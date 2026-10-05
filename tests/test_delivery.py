"""离线交付包测试。

覆盖：
- 成功路径：ZIP 内含各版 PNG 与 JSON 清单，清单按请求顺序记录文档 ID、
  版本号、成品 SHA-256、遮盖数及包内文件名；文件名不取自上传文件名；
- 数量范围（2～20）与重复 (doc_id, version) 明确报错；
- 未知文档/未知版本/未到公开时间/已撤回，任一项出现即整包拒绝，
  且错误不泄露该项成品或遮盖详情（其余项的成品也不出现在响应中）；
- 成品缺失/读盘失败/摘要不符 -> 500，不交付部分包；
- 资格确认与撤回串行：撤回先生效则拒绝，打包先确认则允许本次交付完成。

时钟通过 monkeypatch ``app.main._now_dt`` 注入（定时公开用例），避免真实等待。
"""
import hashlib
import io
import json
import os
import sqlite3
import threading
import zipfile
from datetime import datetime, timedelta, timezone

import pytest
from PIL import Image
from fastapi.testclient import TestClient

import app.main as main_mod
from app.config import Settings
from app.main import create_app
from tests.test_api import png_bytes, upload  # noqa: E402,F402  复用构造器与助手

PATH = "/delivery-packages"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    settings = Settings(data_dir=str(tmp_path / "data"))
    clock = {"now": datetime(2026, 10, 5, 12, 0, 0, tzinfo=timezone.utc)}
    monkeypatch.setattr(main_mod, "_now_dt", lambda: clock["now"])
    app = create_app(settings)
    with TestClient(app) as c:
        c.app.state.clock = clock
        yield c


def _advance(client, **delta):
    client.app.state.clock["now"] += timedelta(**delta)


def _iso(dt):
    return dt.isoformat().replace("+00:00", "Z")


def _annotate(client, doc_id, revision=0, rects=None):
    rects = rects if rects is not None else [
        {"x": 1, "y": 1, "w": 5, "h": 5, "reason": "敏感信息"}
    ]
    r = client.put(
        f"/documents/{doc_id}/annotations",
        json={"revision": revision, "rects": rects},
    )
    assert r.status_code == 200, r.text
    return r.json()["revision"]


def _publish(client, doc_id, **kwargs):
    r = client.post(f"/documents/{doc_id}/publish", **kwargs)
    assert r.status_code == 201, r.text
    return r.json()


def _doc_with_version(client, n_rects=1, size=(40, 30), color=(10, 90, 170)):
    """上传 -> 标注（n_rects 个矩形）-> 立即发布 v1，返回 (doc_id, 发布响应)。"""
    doc_id = upload(client, png_bytes(size=size, color=color)).json()["doc_id"]
    rects = [
        {"x": i, "y": i, "w": 3, "h": 3, "reason": f"遮盖-{i}"}
        for i in range(n_rects)
    ]
    if n_rects:
        _annotate(client, doc_id, 0, rects)
    pub = _publish(client, doc_id)
    assert pub["version"] == 1
    return doc_id, pub


def _request_items(pairs):
    return {"items": [{"doc_id": d, "version": v} for d, v in pairs]}


def _deliver(client, pairs):
    return client.post(PATH, json=_request_items(pairs))


def _unzip(response):
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "application/zip"
    assert "delivery-package.zip" in response.headers["content-disposition"]
    return zipfile.ZipFile(io.BytesIO(response.content))


# ---------------- 成功路径：PNG + JSON 清单，按请求顺序 ----------------

def test_delivery_package_success_manifest_order_and_hashes(client):
    a, pub_a = _doc_with_version(client, n_rects=1, color=(200, 30, 30))
    b, pub_b = _doc_with_version(client, n_rects=2, color=(30, 200, 30))

    # 请求顺序与创建顺序相反，清单必须保留请求顺序
    r = _deliver(client, [(b, 1), (a, 1)])
    zf = _unzip(r)

    names = zf.namelist()
    assert names == [
        f"images/{b}-v1.png",
        f"images/{a}-v1.png",
        "manifest.json",
    ]

    # 包内 PNG 与公开图片入口逐字节一致，且为合法 PNG
    for doc_id, pub in ((b, pub_b), (a, pub_a)):
        img_bytes = zf.read(f"images/{doc_id}-v1.png")
        assert img_bytes[:8] == b"\x89PNG\r\n\x1a\n"
        Image.open(io.BytesIO(img_bytes)).verify()
        assert hashlib.sha256(img_bytes).hexdigest() == pub["output_sha256"]
        served = client.get(f"/publications/{doc_id}/1/image").content
        assert img_bytes == served

    manifest = json.loads(zf.read("manifest.json"))
    assert manifest["count"] == 2
    assert manifest["created_at"]
    assert manifest["items"] == [
        {
            "position": 0,
            "doc_id": b,
            "version": 1,
            "output_sha256": pub_b["output_sha256"],
            "redaction_count": 2,
            "filename": f"images/{b}-v1.png",
        },
        {
            "position": 1,
            "doc_id": a,
            "version": 1,
            "output_sha256": pub_a["output_sha256"],
            "redaction_count": 1,
            "filename": f"images/{a}-v1.png",
        },
    ]


def test_delivery_package_zero_redactions_counted(client):
    # 无遮盖发布同样可以交付，清单遮盖数为 0
    a, pub_a = _doc_with_version(client, n_rects=0)
    b, pub_b = _doc_with_version(client, n_rects=0)
    zf = _unzip(_deliver(client, [(a, 1), (b, 1)]))
    manifest = json.loads(zf.read("manifest.json"))
    assert [it["redaction_count"] for it in manifest["items"]] == [0, 0]
    # 无遮盖的成品是原图的去元数据版本，摘要与发布记录一致
    assert manifest["items"][0]["output_sha256"] == pub_a["output_sha256"]
    assert manifest["items"][1]["output_sha256"] == pub_b["output_sha256"]


def test_delivery_package_redaction_count_uses_published_revision(client):
    # v1 固化 1 个遮盖；随后改标注（3 个）发布 v2——v1 的遮盖数仍应为 1
    doc_id, pub1 = _doc_with_version(client, n_rects=1)
    rects = [
        {"x": 0, "y": 0, "w": 2, "h": 2, "reason": "a"},
        {"x": 4, "y": 4, "w": 2, "h": 2, "reason": "b"},
        {"x": 8, "y": 8, "w": 2, "h": 2, "reason": "c"},
    ]
    _annotate(client, doc_id, 1, rects)
    pub2 = _publish(client, doc_id)

    other, _ = _doc_with_version(client, n_rects=0)
    zf = _unzip(_deliver(client, [(doc_id, 1), (doc_id, 2), (other, 1)]))
    manifest = json.loads(zf.read("manifest.json"))
    by_key = {(it["doc_id"], it["version"]): it for it in manifest["items"]}
    assert by_key[(doc_id, 1)]["redaction_count"] == 1
    assert by_key[(doc_id, 1)]["output_sha256"] == pub1["output_sha256"]
    assert by_key[(doc_id, 2)]["redaction_count"] == 3
    assert by_key[(doc_id, 2)]["output_sha256"] == pub2["output_sha256"]


def test_delivery_package_filenames_not_derived_from_upload_name(client):
    # 即便上传时携带任意原始文件名，包内文件名仍为 {doc_id}-v{version}.png
    doc_id = upload(client, png_bytes(), "../../evil name (1).jpeg").json()["doc_id"]
    _publish(client, doc_id)
    other, _ = _doc_with_version(client)
    zf = _unzip(_deliver(client, [(doc_id, 1), (other, 1)]))
    for name in zf.namelist():
        assert "evil" not in name and ".." not in name
    assert f"images/{doc_id}-v1.png" in zf.namelist()


def test_delivery_package_size_boundaries(client):
    pairs = []
    for _ in range(20):
        doc_id, _ = _doc_with_version(client)
        pairs.append((doc_id, 1))

    # 20 份：允许
    assert _deliver(client, pairs).status_code == 200
    # 2 份：允许
    assert _deliver(client, pairs[:2]).status_code == 200
    # 21 份：拒绝
    extra, _ = _doc_with_version(client)
    r = _deliver(client, pairs + [(extra, 1)])
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert detail["code"] == "invalid_delivery_size"
    assert detail["count"] == 21


@pytest.mark.parametrize("n", [0, 1])
def test_delivery_package_too_few_items(client, n):
    pairs = []
    for _ in range(n):
        doc_id, _ = _doc_with_version(client)
        pairs.append((doc_id, 1))
    r = _deliver(client, pairs)
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert detail["code"] == "invalid_delivery_size"
    assert detail["count"] == n


def test_delivery_package_duplicate_pair_rejected(client):
    a, _ = _doc_with_version(client)
    b, _ = _doc_with_version(client)
    # (a,1) 完全重复；同一文档的不同版本不算重复
    r = _deliver(client, [(a, 1), (b, 1), (a, 1)])
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert detail["code"] == "duplicate_delivery_item"
    assert detail["position"] == 2
    assert detail["doc_id"] == a
    assert detail["version"] == 1


def test_delivery_package_same_doc_distinct_versions_allowed(client):
    doc_id, _ = _doc_with_version(client, n_rects=1)
    _annotate(client, doc_id, 1, [
        {"x": 1, "y": 1, "w": 5, "h": 5, "reason": "敏感信息"},
        {"x": 9, "y": 9, "w": 4, "h": 4, "reason": "工号"},
    ])
    _publish(client, doc_id)
    other, _ = _doc_with_version(client)
    zf = _unzip(_deliver(client, [(doc_id, 2), (doc_id, 1), (other, 1)]))
    manifest = json.loads(zf.read("manifest.json"))
    assert [it["version"] for it in manifest["items"]] == [2, 1, 1]
    assert [it["doc_id"] for it in manifest["items"]] == [doc_id, doc_id, other]
    assert zf.namelist().count(f"images/{doc_id}-v1.png") == 1
    assert zf.namelist().count(f"images/{doc_id}-v2.png") == 1


# ---------------- 资格：未知版本/未到公开时间/已撤回 => 整包拒绝 ----------------

def test_delivery_package_unknown_document_rejected_without_leakage(client):
    a, _ = _doc_with_version(client)
    r = _deliver(client, [("deadbeef" * 4, 1), (a, 1)])
    assert r.status_code == 404
    detail = r.json()["detail"]
    assert detail["code"] == "document_not_found"
    assert detail["position"] == 0
    # 错误不携带成品摘要/遮盖详情；响应也不是 ZIP
    assert "output_sha256" not in detail
    assert "redaction" not in detail
    assert r.headers["content-type"] != "application/zip"


def test_delivery_package_unknown_version_rejected(client):
    doc_id, _ = _doc_with_version(client)
    other, _ = _doc_with_version(client)
    r = _deliver(client, [(doc_id, 99), (other, 1)])
    assert r.status_code == 404
    detail = r.json()["detail"]
    assert detail["code"] == "unknown_version"
    assert detail["position"] == 0
    assert detail["doc_id"] == doc_id
    assert detail["version"] == 99


def test_delivery_package_nonpositive_version_unknown(client):
    doc_id, _ = _doc_with_version(client)
    other, _ = _doc_with_version(client)
    r = _deliver(client, [(doc_id, 0), (other, 1)])
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "unknown_version"


def test_delivery_package_not_yet_public_rejected(client):
    doc_id = upload(client, png_bytes()).json()["doc_id"]
    _annotate(client, doc_id, 0)
    target = client.app.state.clock["now"] + timedelta(hours=2)
    _publish(client, doc_id, json={"publish_at": _iso(target)})
    other, _ = _doc_with_version(client)

    # 到点前：整包 404，且不暴露该版本的成品/遮盖（即使它是第二项）
    r = _deliver(client, [(other, 1), (doc_id, 1)])
    assert r.status_code == 404
    detail = r.json()["detail"]
    assert detail["code"] == "publication_not_yet_public"
    assert detail["position"] == 1
    assert "output_sha256" not in detail and "redaction" not in detail

    # 到点后：同请求自动允许
    _advance(client, hours=2, seconds=1)
    zf = _unzip(_deliver(client, [(other, 1), (doc_id, 1)]))
    manifest = json.loads(zf.read("manifest.json"))
    assert manifest["count"] == 2


def test_delivery_package_withdrawn_rejected(client):
    doc_id, _ = _doc_with_version(client, n_rects=2)
    other, _ = _doc_with_version(client)
    r = client.post(
        f"/documents/{doc_id}/publications/1/withdraw",
        json={"reason": "遮盖遗漏"},
    )
    assert r.status_code == 201

    r = _deliver(client, [(other, 1), (doc_id, 1)])
    assert r.status_code == 410
    detail = r.json()["detail"]
    assert detail["code"] == "publication_withdrawn"
    assert detail["position"] == 1
    assert "output_sha256" not in detail and "redaction" not in detail


def test_delivery_package_withdrawn_beats_schedule_reaches_time(client):
    # 撤回优先于公开时间：即使把时钟拨到公开时间之后，仍整包 410
    doc_id = upload(client, png_bytes()).json()["doc_id"]
    _annotate(client, doc_id, 0)
    target = client.app.state.clock["now"] + timedelta(hours=1)
    _publish(client, doc_id, json={"publish_at": _iso(target)})
    assert client.post(
        f"/documents/{doc_id}/publications/1/withdraw",
        json={"reason": "撤回定时版本"},
    ).status_code == 201
    other, _ = _doc_with_version(client)

    _advance(client, hours=3)
    r = _deliver(client, [(other, 1), (doc_id, 1)])
    assert r.status_code == 410
    assert r.json()["detail"]["code"] == "publication_withdrawn"


def test_delivery_package_no_partial_package_on_any_failure(client):
    # 第一项合格、第二项已撤回：响应中不得出现第一项的成品字节
    a, pub_a = _doc_with_version(client)
    b, _ = _doc_with_version(client)
    client.post(
        f"/documents/{b}/publications/1/withdraw", json={"reason": "x"}
    )
    r = _deliver(client, [(a, 1), (b, 1)])
    assert r.status_code == 410
    # 响应体是 JSON 错误而非 ZIP；第一项成品内容也不出现在错误响应中
    assert r.headers["content-type"].startswith("application/json")
    assert pub_a["output_sha256"] not in r.text
    with pytest.raises(zipfile.BadZipFile):
        zipfile.ZipFile(io.BytesIO(r.content)).getinfo("x")


# ---------------- 成品缺失/读盘失败/摘要不符：不交付部分包 ----------------

def _artifact_path(client, doc_id, version=1):
    settings = client.app.state.settings
    return os.path.join(settings.publications_dir, doc_id, f"v{version}.png")


def test_delivery_package_artifact_missing_rejected(client):
    a, _ = _doc_with_version(client)
    b, _ = _doc_with_version(client)
    os.unlink(_artifact_path(client, b))

    r = _deliver(client, [(a, 1), (b, 1)])
    assert r.status_code == 500
    detail = r.json()["detail"]
    assert detail["code"] == "artifact_missing"
    assert detail["position"] == 1
    assert r.headers["content-type"].startswith("application/json")


def test_delivery_package_artifact_unreadable_rejected(client, monkeypatch):
    import builtins

    a, _ = _doc_with_version(client)
    b, _ = _doc_with_version(client)
    target_path = os.path.abspath(_artifact_path(client, b))

    real_open = builtins.open

    def flaky_open(path, *args, **kwargs):
        if os.path.abspath(str(path)) == target_path:
            raise OSError("simulated disk failure")
        return real_open(path, *args, **kwargs)

    # 交付端点内以 open(...) 内建调用读盘（main 模块未单独 import open）
    monkeypatch.setattr(builtins, "open", flaky_open)
    r = _deliver(client, [(a, 1), (b, 1)])
    assert r.status_code == 500
    assert r.json()["detail"]["code"] == "artifact_read_failed"


def test_delivery_package_artifact_hash_mismatch_rejected(client):
    a, _ = _doc_with_version(client)
    b, pub_b = _doc_with_version(client)
    # 磁盘成品被破坏（库内 output_sha256 仍记录原值）
    with open(_artifact_path(client, b), "wb") as f:
        f.write(b"not the published png bytes")

    r = _deliver(client, [(a, 1), (b, 1)])
    assert r.status_code == 500
    detail = r.json()["detail"]
    assert detail["code"] == "artifact_corrupted"
    assert detail["position"] == 1
    # 记录的真实摘要不应出现在错误详情中
    assert pub_b["output_sha256"] not in r.text


# ---------------- 资格确认与撤回串行 ----------------

def test_withdraw_effective_before_qualification_rejects(client):
    """撤回先生效：打包请求在资格确认处看到撤回记录，整包 410。"""
    a, _ = _doc_with_version(client)
    b, _ = _doc_with_version(client)
    wr = client.post(
        f"/documents/{b}/publications/1/withdraw", json={"reason": "先于打包撤回"}
    )
    assert wr.status_code == 201

    r = _deliver(client, [(a, 1), (b, 1)])
    assert r.status_code == 410
    detail = r.json()["detail"]
    assert detail["code"] == "publication_withdrawn"
    assert detail["position"] == 1


def test_package_qualified_before_withdraw_completes(client):
    """打包先确认：撤回在写锁上排队，本次交付允许完成；提交后撤回生效。"""
    import time

    a, _ = _doc_with_version(client)
    b, _ = _doc_with_version(client)
    result = {}

    def withdraw():
        r = client.post(
            f"/documents/{b}/publications/1/withdraw", json={"reason": "排队撤回"}
        )
        result["withdraw"] = r.status_code

    def hook(_qualified):
        # 此刻打包事务持有写锁且资格全部确认；发起并发撤回——它的
        # BEGIN IMMEDIATE 必然阻塞等待，直到打包事务提交（撤回先不生效）。
        t = threading.Thread(target=withdraw)
        t.start()
        time.sleep(0.5)  # 留足时间让撤回进入写锁等待队列
        # 不能在此 join：会与"撤回等待本事务结束"形成死锁
        result["started"] = True

    client.app.state.package_assembly_hook = hook
    try:
        r = _deliver(client, [(a, 1), (b, 1)])
    finally:
        client.app.state.package_assembly_hook = None

    # 本次交付已完整完成：含两份 PNG 与清单（撤回未能在资格确认前生效）
    zf = _unzip(r)
    manifest = json.loads(zf.read("manifest.json"))
    assert manifest["count"] == 2
    assert f"images/{b}-v1.png" in zf.namelist()
    assert f"images/{a}-v1.png" in zf.namelist()
    # 打包事务提交后撤回应运生效（恰好一次首次撤回 201）
    assert result.get("started") is True
    # 等待排队撤回结束（此时已能拿到写锁）
    deadline = time.time() + 10
    while "withdraw" not in result and time.time() < deadline:
        time.sleep(0.05)
    assert result["withdraw"] == 201

    # 撤回已生效：之后的新交付与公开读取均被拒绝（已完成的交付不受追溯影响）
    r2 = _deliver(client, [(a, 1), (b, 1)])
    assert r2.status_code == 410
    assert client.get(f"/publications/{b}/1").status_code == 410


def test_concurrent_package_and_withdraw_serialized_one_outcome(client):
    """压力竞赛：多线程同时交付/撤回，结果只能是两种串行结局之一，
    且绝不会出现"交付了一个在资格确认时已撤回的版本"。"""
    doc_id, _ = _doc_with_version(client, n_rects=1)
    other, _ = _doc_with_version(client)
    barrier = threading.Barrier(6)
    outcomes = []
    lock = threading.Lock()

    def packager():
        barrier.wait()
        r = _deliver(client, [(other, 1), (doc_id, 1)])
        with lock:
            if r.status_code == 200:
                zf = zipfile.ZipFile(io.BytesIO(r.content))
                manifest = json.loads(zf.read("manifest.json"))
                outcomes.append(("ok", manifest["count"]))
            else:
                outcomes.append((r.status_code, r.json()["detail"]["code"]))

    def withdrawer():
        barrier.wait()
        r = client.post(
            f"/documents/{doc_id}/publications/1/withdraw", json={"reason": "并发撤回"}
        )
        with lock:
            outcomes.append(("withdraw", r.status_code))

    threads = [threading.Thread(target=packager) for _ in range(4)]
    threads.append(threading.Thread(target=withdrawer))
    threads.append(threading.Thread(target=withdrawer))
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    packages = [o for o in outcomes if o[0] == "ok"]
    rejected = [o for o in outcomes if o[1] == "publication_withdrawn"]
    # 撤回恰好一次首次创建（201），另一次幂等（200）
    withdraws = [o for o in outcomes if o[0] == "withdraw"]
    assert sorted(s for _, s in withdraws) == [200, 201]
    # 交付：要么全部成功（撤回尚未生效），要么全部 410（撤回先生效）；
    # 不会有 500/部分包，成功的包都恰好含 2 份成品
    assert len(packages) + len(rejected) == 4
    assert all(o[1] == 2 for o in packages)


# ---------------- 既有发布/公开读取契约保持兼容 ----------------

def test_delivery_does_not_mutate_publications(client):
    a, pub_a = _doc_with_version(client)
    b, pub_b = _doc_with_version(client)
    _deliver(client, [(a, 1), (b, 1)])

    settings = client.app.state.settings
    conn = sqlite3.connect(settings.db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT doc_id, version, output_sha256 FROM publications ORDER BY doc_id"
    ).fetchall()
    conn.close()
    digests = {r["output_sha256"] for r in rows}
    assert digests == {pub_a["output_sha256"], pub_b["output_sha256"]}
    # 公开读取照常
    assert client.get(f"/publications/{a}/1").status_code == 200
    assert client.get(f"/publications/{b}/1/image").status_code == 200
