"""HTTP API：上传、标注（乐观锁修订）、发布（事务内涂实）、撤回、公开读取。"""
from __future__ import annotations

import hashlib
import json
import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import Body, FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response

from .config import Settings
from .db import init_db, read_tx, write_tx
from .imaging import (
    SUPPORTED_FORMATS,
    ImageDecodeError,
    UnsupportedFormatError,
    load_oriented,
    render_publication,
)
from .models import (
    AnnotationsPut,
    BatchPublishRequest,
    PublishRequest,
    WithdrawRequest,
)

MEDIA_TYPES = {"PNG": "image/png", "JPEG": "image/jpeg"}

# 定时公开的最大提前量：首次提交时公开时间不得晚于当前时刻 30 天
MAX_PUBLISH_DELAY = timedelta(days=30)


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _now_dt() -> datetime:
    """当前时刻（UTC，aware）。单独成函数便于测试注入固定时钟。"""
    return datetime.now(timezone.utc)


def _stored_publish_at_dt(value: str) -> datetime:
    """解析库内规范化存储的公开时间（UTC ISO 8601，尾部 Z）。"""
    return datetime.fromisoformat(value)


def _parse_publish_at(raw: str) -> tuple[datetime, str]:
    """解析调用方指定的公开时间。

    仅接受带时区偏移的 RFC 3339/ISO 8601 字符串（尾部 ``Z`` 等同 +00:00），
    统一换算为 UTC；返回 (aware UTC datetime, 规范化时间字符串)。
    无法解析或为朴素时间（无偏移）均属无效时间，调用方据此返回 400。
    """
    text = raw.strip()
    if not text:
        _error(
            400,
            "invalid_publish_time",
            "publish_at must be an RFC 3339 / ISO 8601 date-time with a"
            " timezone offset (UTC recommended), e.g."
            " 2026-10-06T08:00:00Z; use null or omit the field to publish"
            " immediately",
        )
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        _error(
            400,
            "invalid_publish_time",
            "publish_at must be an RFC 3339 / ISO 8601 date-time with a"
            " timezone offset (UTC recommended), e.g."
            " 2026-10-06T08:00:00Z",
        )
    if dt.tzinfo is None or dt.utcoffset() is None:
        _error(
            400,
            "invalid_publish_time",
            "publish_at must carry a timezone offset; a naive local time is"
            " ambiguous, use e.g. 2026-10-06T08:00:00Z",
        )
    dt_utc = dt.astimezone(timezone.utc)
    # 规范化为统一的 UTC 文本（尾部 Z）；Python 3.11 的 fromisoformat 可直接读回
    normalized = dt_utc.isoformat().replace("+00:00", "Z")
    return dt_utc, normalized


def _check_publish_window(dt_utc: datetime) -> None:
    """首次提交的窗口校验：必须晚于当前时刻，且不超过 30 天。

    幂等键重放路径不调用本函数——首次结果原样返回，即使公开时间已过也不
    重新校验。
    """
    now = _now_dt()
    if dt_utc <= now:
        _error(
            400,
            "invalid_publish_time",
            "publish_at must be later than the current time",
        )
    if dt_utc - now > MAX_PUBLISH_DELAY:
        _error(
            400,
            "invalid_publish_time",
            "publish_at must be no more than 30 days in the future",
        )


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _batch_request_hash(items: Any, publish_at: str | None) -> str:
    """幂等键绑定的内容摘要：按请求顺序排列的 (doc_id, expected_revision)
    列表，加上整批统一的公开时间（含"立即公开"的 null）。"""
    canonical = json.dumps(
        {
            "documents": [
                {"doc_id": it.doc_id, "expected_revision": it.expected_revision}
                for it in items
            ],
            "publish_at": publish_at,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _error(status: int, code: str, message: str, **extra: Any) -> None:
    detail = {"code": code, "message": message}
    detail.update(extra)
    raise HTTPException(status_code=status, detail=detail)


def _rect_out(row: Any) -> dict:
    return {
        "x": row["x"],
        "y": row["y"],
        "w": row["w"],
        "h": row["h"],
        "reason": row["reason"],
    }


def _pub_entry(row: Any, keys: list[str]) -> dict:
    """发布记录条目：原字段按序输出，末尾依次追加公开时间与撤回状态字段。

    查询须以列别名带出 p_at / w_at / w_reason（见各列表端点）。
    """
    out = {k: row[k] for k in keys}
    out["publish_at"] = row["p_at"]
    out["withdrawn"] = row["w_at"] is not None
    out["withdrawn_at"] = row["w_at"]
    out["withdrawal_reason"] = row["w_reason"]
    return out


# 列表查询共用的撤回状态联表片段
_WITHDRAWAL_JOIN = (
    " LEFT JOIN publication_withdrawals w"
    " ON w.doc_id = {t}.doc_id AND w.version = {t}.version"
)
_WITHDRAWAL_COLS = ", w.withdrawn_at AS w_at, w.reason AS w_reason"


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        os.makedirs(settings.originals_dir, exist_ok=True)
        os.makedirs(settings.publications_dir, exist_ok=True)
        init_db(settings.db_path)
        yield

    app = FastAPI(
        title="档案影像脱敏发布服务",
        version="1.0.0",
        lifespan=lifespan,
    )
    app.state.settings = settings

    def _get_doc(conn: Any, doc_id: str) -> Any:
        row = conn.execute(
            "SELECT * FROM documents WHERE id = ?", (doc_id,)
        ).fetchone()
        if row is None:
            _error(404, "document_not_found", f"document '{doc_id}' does not exist")
        return row

    def _get_publication(conn: Any, doc_id: str, version: int) -> Any:
        """取发布记录；未知文档/版本按既有的不存在错误返回。"""
        pub = conn.execute(
            "SELECT * FROM publications WHERE doc_id = ? AND version = ?",
            (doc_id, version),
        ).fetchone()
        if pub is None:
            exists = conn.execute(
                "SELECT 1 FROM documents WHERE id = ?", (doc_id,)
            ).fetchone()
            if exists is None:
                _error(
                    404, "document_not_found", f"document '{doc_id}' does not exist"
                )
            _error(
                404,
                "unknown_version",
                f"document '{doc_id}' has no publication version {version}",
            )
        return pub

    def _ensure_not_withdrawn(conn: Any, doc_id: str, version: int) -> None:
        """已撤回版本对公开面不可见（410），不返回成品与遮盖详情。

        撤回只登记记录、不删除成品文件：生效前已开始的下载因此能够
        完成，只有新发起的公开读取被拒绝。
        """
        row = conn.execute(
            "SELECT 1 FROM publication_withdrawals"
            " WHERE doc_id = ? AND version = ?",
            (doc_id, version),
        ).fetchone()
        if row is not None:
            _error(
                410,
                "publication_withdrawn",
                f"document '{doc_id}' publication version {version} has been"
                " withdrawn and is no longer publicly available",
            )

    def _ensure_publish_time_reached(pub: Any) -> None:
        """未到公开时间的版本对公开面表现为 404（与未知版本一致）。

        既不返回成品，也不返回遮盖坐标/原因、摘要等任何版本信息；
        公开时间到达后无需任何额外操作即可读取。调用前必须先完成
        撤回检查——永久撤回优先于公开时间，撤回版本一律 410。
        """
        publish_at = pub["publish_at"]
        if publish_at is not None and _stored_publish_at_dt(
            publish_at
        ) > _now_dt():
            _error(
                404,
                "publication_not_yet_public",
                f"publication is scheduled to become public at {publish_at}",
            )

    # ---------------- 基本 ----------------

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    # ---------------- 档案上传 ----------------

    @app.post("/documents", status_code=201)
    async def upload_document(file: UploadFile = File(...)) -> dict:
        data = await file.read()
        if not data:
            _error(400, "empty_upload", "uploaded file is empty")
        if len(data) > settings.max_upload_bytes:
            _error(
                413,
                "payload_too_large",
                f"file is {len(data)} bytes, limit is {settings.max_upload_bytes}",
            )
        try:
            oriented, fmt = load_oriented(data)
        except ImageDecodeError as exc:
            _error(400, "image_decode_failed", f"cannot decode image: {exc}")
        except UnsupportedFormatError as exc:
            _error(
                415,
                "unsupported_media_type",
                f"only PNG and JPEG are supported, got format: {exc}",
            )

        doc_id = uuid.uuid4().hex
        digest = _sha256_bytes(data)
        original_path = os.path.abspath(
            os.path.join(settings.originals_dir, doc_id + SUPPORTED_FORMATS[fmt])
        )
        with open(original_path, "wb") as f:
            f.write(data)

        width, height = oriented.size
        now = _utcnow()
        try:
            with write_tx(settings.db_path) as conn:
                conn.execute(
                    "INSERT INTO documents (id, filename, media_type, original_path,"
                    " original_sha256, width, height, annotation_revision, created_at)"
                    " VALUES (?,?,?,?,?,?,?,0,?)",
                    (
                        doc_id,
                        file.filename or "",
                        MEDIA_TYPES[fmt],
                        original_path,
                        digest,
                        width,
                        height,
                        now,
                    ),
                )
        except Exception:
            if os.path.exists(original_path):
                os.unlink(original_path)
            raise
        return {
            "doc_id": doc_id,
            "filename": file.filename or "",
            "media_type": MEDIA_TYPES[fmt],
            "width": width,
            "height": height,
            "original_sha256": digest,
            "annotation_revision": 0,
            "created_at": now,
        }

    @app.get("/documents/{doc_id}")
    def get_document(doc_id: str) -> dict:
        with read_tx(settings.db_path) as conn:
            doc = _get_doc(conn, doc_id)
            pubs = conn.execute(
                "SELECT p.version, p.annotation_revision, p.output_sha256,"
                " p.created_at, p.publish_at AS p_at" + _WITHDRAWAL_COLS +
                " FROM publications p" + _WITHDRAWAL_JOIN.format(t="p") +
                " WHERE p.doc_id = ? ORDER BY p.version",
                (doc_id,),
            ).fetchall()
        return {
            "doc_id": doc["id"],
            "filename": doc["filename"],
            "media_type": doc["media_type"],
            "width": doc["width"],
            "height": doc["height"],
            "original_sha256": doc["original_sha256"],
            "annotation_revision": doc["annotation_revision"],
            "created_at": doc["created_at"],
            "publications": [
                _pub_entry(
                    p, ["version", "annotation_revision", "output_sha256",
                        "created_at"]
                )
                for p in pubs
            ],
        }

    # ---------------- 标注（修订号乐观锁） ----------------

    @app.get("/documents/{doc_id}/annotations")
    def get_annotations(doc_id: str) -> dict:
        with read_tx(settings.db_path) as conn:
            doc = _get_doc(conn, doc_id)
            rects = conn.execute(
                "SELECT x, y, w, h, reason FROM annotation_rects"
                " WHERE doc_id = ? AND revision = ? ORDER BY idx",
                (doc_id, doc["annotation_revision"]),
            ).fetchall()
        return {
            "doc_id": doc_id,
            "revision": doc["annotation_revision"],
            "rects": [_rect_out(r) for r in rects],
        }

    @app.put("/documents/{doc_id}/annotations")
    def put_annotations(doc_id: str, body: AnnotationsPut) -> dict:
        with write_tx(settings.db_path) as conn:
            doc = _get_doc(conn, doc_id)
            current = doc["annotation_revision"]
            if body.revision != current:
                _error(
                    409,
                    "stale_revision",
                    f"annotation revision mismatch: expected {current},"
                    f" got {body.revision}; re-fetch and retry",
                    current_revision=current,
                )
            width, height = doc["width"], doc["height"]
            for i, r in enumerate(body.rects):
                if r.w <= 0 or r.h <= 0:
                    _error(
                        400,
                        "empty_rectangle",
                        f"rect #{i} has non-positive size (w={r.w}, h={r.h})",
                    )
                if r.x < 0 or r.y < 0 or r.x + r.w > width or r.y + r.h > height:
                    _error(
                        400,
                        "rectangle_out_of_bounds",
                        f"rect #{i} (x={r.x}, y={r.y}, w={r.w}, h={r.h}) exceeds"
                        f" image bounds {width}x{height}",
                    )
                if not r.reason.strip():
                    _error(
                        400,
                        "reason_required",
                        f"rect #{i} must carry a non-empty reason",
                    )
            new_revision = current + 1
            for idx, r in enumerate(body.rects):
                conn.execute(
                    "INSERT INTO annotation_rects"
                    " (doc_id, revision, idx, x, y, w, h, reason)"
                    " VALUES (?,?,?,?,?,?,?,?)",
                    (
                        doc_id,
                        new_revision,
                        idx,
                        r.x,
                        r.y,
                        r.w,
                        r.h,
                        r.reason.strip(),
                    ),
                )
            conn.execute(
                "UPDATE documents SET annotation_revision = ? WHERE id = ?",
                (new_revision, doc_id),
            )
        return {
            "doc_id": doc_id,
            "revision": new_revision,
            "rects": [r.model_dump() for r in body.rects],
        }

    # ---------------- 发布 ----------------

    @app.post("/documents/{doc_id}/publish", status_code=201)
    def publish_document(
        doc_id: str, body: PublishRequest | None = Body(default=None)
    ) -> dict:
        # 公开时间在进入事务前解析（格式无效即 400，无任何副作用）；
        # "晚于当前且不超过 30 天"的窗口校验在写事务内完成。
        publish_at: str | None = None
        publish_dt: datetime | None = None
        if body is not None and body.publish_at is not None:
            publish_dt, publish_at = _parse_publish_at(body.publish_at)

        output_path: str | None = None
        try:
            # 整个"窗口校验+读修订+读矩形+渲染+落盘+写记录"在一个写事务内，
            # 与标注修改互斥，保证成品与记录的修订号一致；定时版本同样在发布
            # 当下固化成品并占用正常版本号，只是公开时间到达前对外 404。
            with write_tx(settings.db_path) as conn:
                doc = _get_doc(conn, doc_id)
                if publish_dt is not None:
                    _check_publish_window(publish_dt)
                revision = doc["annotation_revision"]
                rects = conn.execute(
                    "SELECT x, y, w, h, reason FROM annotation_rects"
                    " WHERE doc_id = ? AND revision = ? ORDER BY idx",
                    (doc_id, revision),
                ).fetchall()
                version = conn.execute(
                    "SELECT COALESCE(MAX(version), 0) + 1 AS v"
                    " FROM publications WHERE doc_id = ?",
                    (doc_id,),
                ).fetchone()["v"]

                try:
                    with open(doc["original_path"], "rb") as f:
                        original_bytes = f.read()
                    oriented, _ = load_oriented(original_bytes)
                except Exception as exc:
                    _error(
                        500,
                        "original_unavailable",
                        f"stored original cannot be decoded: {exc}",
                    )

                image = render_publication(oriented, rects)
                doc_pub_dir = os.path.join(settings.publications_dir, doc_id)
                os.makedirs(doc_pub_dir, exist_ok=True)
                output_path = os.path.abspath(
                    os.path.join(doc_pub_dir, f"v{version}.png")
                )
                image.save(output_path, format="PNG")  # 无元数据的独立 PNG
                output_sha256 = _sha256_file(output_path)

                now = _utcnow()
                conn.execute(
                    "INSERT INTO publications (doc_id, version, annotation_revision,"
                    " original_sha256, output_sha256, output_path, created_at,"
                    " publish_at) VALUES (?,?,?,?,?,?,?,?)",
                    (
                        doc_id,
                        version,
                        revision,
                        doc["original_sha256"],
                        output_sha256,
                        output_path,
                        now,
                        publish_at,
                    ),
                )
        except Exception:
            # 事务回滚时清理可能已落盘的半成品文件
            if output_path and os.path.exists(output_path):
                try:
                    os.unlink(output_path)
                except OSError:
                    pass
            raise
        return {
            "doc_id": doc_id,
            "version": version,
            "annotation_revision": revision,
            "original_sha256": doc["original_sha256"],
            "output_sha256": output_sha256,
            "redaction_count": len(rects),
            "created_at": now,
            "publish_at": publish_at,  # null=立即公开；否则为统一 UTC 公开时间
        }

    @app.get("/documents/{doc_id}/publications")
    def list_publications(doc_id: str) -> dict:
        with read_tx(settings.db_path) as conn:
            _get_doc(conn, doc_id)
            pubs = conn.execute(
                "SELECT p.version, p.annotation_revision, p.original_sha256,"
                " p.output_sha256, p.created_at, p.publish_at AS p_at"
                + _WITHDRAWAL_COLS +
                " FROM publications p" + _WITHDRAWAL_JOIN.format(t="p") +
                " WHERE p.doc_id = ? ORDER BY p.version",
                (doc_id,),
            ).fetchall()
        return {
            "doc_id": doc_id,
            "publications": [
                _pub_entry(
                    p, ["version", "annotation_revision", "original_sha256",
                        "output_sha256", "created_at"]
                )
                for p in pubs
            ],
        }

    # ---------------- 批次发布（整批原子） ----------------

    @app.post("/publication-batches", status_code=201)
    def publish_batch(body: BatchPublishRequest) -> Any:
        items = body.documents
        key = body.idempotency_key
        # 整批统一的公开时间：先解析（格式无效直接 400，不进入事务、
        # 不占版本/批次/幂等键），规范化值同时纳入幂等内容摘要。
        publish_at: str | None = None
        publish_dt: datetime | None = None
        if body.publish_at is not None:
            publish_dt, publish_at = _parse_publish_at(body.publish_at)
        request_hash = (
            _batch_request_hash(items, publish_at) if key is not None else None
        )

        batch_id = uuid.uuid4().hex
        now = _utcnow()
        written_paths: list[str] = []
        # 整批校验 + 渲染 + 落盘 + 记录全部在同一个 BEGIN IMMEDIATE 事务中，
        # 与标注修改、单份发布、其他批次发布互斥串行；
        # 任一项失败即回滚全部 INSERT，并清理已落盘文件，不留下可公开的
        # 部分版本，MAX(version)+1 也不会被失败批次推进（记录从未提交）。
        # 幂等键的"查重 -> 发布 -> 登记"也在该事务内：并发重试被写锁串行化，
        # 最多发布一次；任何失败回滚时键记录一并撤销，键不被占用，可原键重试。
        try:
            with write_tx(settings.db_path) as conn:
                # 第零阶段：幂等键检查，先于一切业务校验——
                # 相同键+相同内容的重试直接返回首次成功结果，
                # 即使标注随后变化，也不再校验当前修订号、不重新渲染。
                if key is not None:
                    prior = conn.execute(
                        "SELECT request_hash, response FROM batch_idempotency"
                        " WHERE key = ?",
                        (key,),
                    ).fetchone()
                    if prior is not None:
                        if prior["request_hash"] == request_hash:
                            # 重放：原样返回持久化的首次成功响应（HTTP 200）
                            return Response(
                                content=prior["response"],
                                media_type="application/json",
                                status_code=200,
                            )
                        # 相同键+不同内容：明确冲突，原批次不受影响
                        _error(
                            409,
                            "idempotency_key_conflict",
                            f"idempotency key '{key}' was already used with"
                            " different batch content; the original batch is"
                            " unchanged",
                        )

                # 语义校验：公开时间窗口、数量与重复（失败即回滚，不占用
                # 幂等键、不涉及版本号）。窗口校验只在首次请求进行——
                # 重放已在上方直接返回，即使公开时间已过也不重新校验。
                if publish_dt is not None:
                    _check_publish_window(publish_dt)
                if not 2 <= len(items) <= 20:
                    _error(
                        400,
                        "invalid_batch_size",
                        f"batch must contain between 2 and 20 documents,"
                        f" got {len(items)}",
                        count=len(items),
                    )
                seen: set[str] = set()
                for pos, item in enumerate(items):
                    if item.doc_id in seen:
                        _error(
                            400,
                            "duplicate_document_in_batch",
                            f"document '{item.doc_id}' appears more than once"
                            " in the batch",
                            position=pos,
                            doc_id=item.doc_id,
                        )
                    seen.add(item.doc_id)

                # 第一阶段：按请求顺序逐份校验（存在性 + 修订号匹配），
                # 先定位失败项，再渲染任何成品。
                prepared: list[tuple[Any, int, Any]] = []
                for pos, item in enumerate(items):
                    doc = conn.execute(
                        "SELECT * FROM documents WHERE id = ?", (item.doc_id,)
                    ).fetchone()
                    if doc is None:
                        _error(
                            404,
                            "document_not_found",
                            f"document '{item.doc_id}' does not exist",
                            position=pos,
                            doc_id=item.doc_id,
                        )
                    revision = doc["annotation_revision"]
                    if item.expected_revision != revision:
                        _error(
                            409,
                            "stale_revision",
                            f"annotation revision mismatch for '{item.doc_id}':"
                            f" expected {revision}, got {item.expected_revision};"
                            " re-fetch and retry",
                            position=pos,
                            doc_id=item.doc_id,
                            current_revision=revision,
                            supplied_revision=item.expected_revision,
                        )
                    rects = conn.execute(
                        "SELECT x, y, w, h, reason FROM annotation_rects"
                        " WHERE doc_id = ? AND revision = ? ORDER BY idx",
                        (doc["id"], revision),
                    ).fetchall()
                    version = conn.execute(
                        "SELECT COALESCE(MAX(version), 0) + 1 AS v"
                        " FROM publications WHERE doc_id = ?",
                        (doc["id"],),
                    ).fetchone()["v"]
                    prepared.append((doc, version, revision, rects))

                # 第二阶段：全部校验通过后逐份渲染、落盘、登记
                result_items: list[dict] = []
                for pos, (doc, version, revision, rects) in enumerate(prepared):
                    try:
                        with open(doc["original_path"], "rb") as f:
                            original_bytes = f.read()
                        oriented, _ = load_oriented(original_bytes)
                    except Exception as exc:
                        _error(
                            500,
                            "original_unavailable",
                            f"stored original for '{doc['id']}' cannot be decoded:"
                            f" {exc}",
                            position=pos,
                            doc_id=doc["id"],
                        )

                    image = render_publication(oriented, rects)
                    doc_pub_dir = os.path.join(settings.publications_dir, doc["id"])
                    output_path = os.path.abspath(
                        os.path.join(doc_pub_dir, f"v{version}.png")
                    )
                    try:
                        os.makedirs(doc_pub_dir, exist_ok=True)
                        image.save(output_path, format="PNG")  # 无元数据独立 PNG
                        output_sha256 = _sha256_file(output_path)
                    except Exception as exc:
                        # 成品写入失败：清理可能已部分写入的文件
                        if os.path.exists(output_path):
                            try:
                                os.unlink(output_path)
                            except OSError:
                                pass
                        _error(
                            500,
                            "artifact_write_failed",
                            f"failed to write publication artifact for"
                            f" '{doc['id']}' version {version}: {exc}",
                            position=pos,
                            doc_id=doc["id"],
                            version=version,
                        )
                    written_paths.append(output_path)

                    conn.execute(
                        "INSERT INTO publications (doc_id, version, annotation_revision,"
                        " original_sha256, output_sha256, output_path, created_at,"
                        " batch_id, publish_at) VALUES (?,?,?,?,?,?,?,?,?)",
                        (
                            doc["id"],
                            version,
                            revision,
                            doc["original_sha256"],
                            output_sha256,
                            output_path,
                            now,
                            batch_id,
                            publish_at,
                        ),
                    )
                    result_items.append(
                        {
                            "position": pos,
                            "doc_id": doc["id"],
                            "version": version,
                            "annotation_revision": revision,
                            "original_sha256": doc["original_sha256"],
                            "output_sha256": output_sha256,
                            "redaction_count": len(rects),
                            "created_at": now,
                        }
                    )

                conn.execute(
                    "INSERT INTO publication_batches (id, created_at, publish_at)"
                    " VALUES (?,?,?)",
                    (batch_id, now, publish_at),
                )
                for out in result_items:
                    conn.execute(
                        "INSERT INTO batch_publications (batch_id, position, doc_id,"
                        " version, annotation_revision, original_sha256, output_sha256)"
                        " VALUES (?,?,?,?,?,?,?)",
                        (
                            batch_id,
                            out["position"],
                            out["doc_id"],
                            out["version"],
                            out["annotation_revision"],
                            out["original_sha256"],
                            out["output_sha256"],
                        ),
                    )

                response_body = {
                    "batch_id": batch_id,
                    "created_at": now,
                    "count": len(result_items),
                    "documents": result_items,  # 顺序与请求一致
                    "publish_at": publish_at,   # null=立即公开；否则为整批统一 UTC 公开时间
                }
                if key is not None:
                    # 首次成功：持久化键与完整成功结果，同事务提交
                    conn.execute(
                        "INSERT INTO batch_idempotency (key, request_hash, batch_id,"
                        " response, created_at) VALUES (?,?,?,?,?)",
                        (
                            key,
                            request_hash,
                            batch_id,
                            json.dumps(response_body, ensure_ascii=False),
                            now,
                        ),
                    )
        except Exception:
            # 事务回滚（不留下任何批次/版本/幂等键记录），并清理已落盘半成品
            for path in written_paths:
                if os.path.exists(path):
                    try:
                        os.unlink(path)
                    except OSError:
                        pass
            raise
        return response_body

    @app.get("/publication-batches/{batch_id}")
    def get_publication_batch(batch_id: str) -> dict:
        with read_tx(settings.db_path) as conn:
            batch = conn.execute(
                "SELECT id, created_at, publish_at FROM publication_batches"
                " WHERE id = ?",
                (batch_id,),
            ).fetchone()
            if batch is None:
                _error(
                    404,
                    "batch_not_found",
                    f"publication batch '{batch_id}' does not exist",
                )
            rows = conn.execute(
                "SELECT bp.position, bp.doc_id, bp.version, bp.annotation_revision,"
                " bp.original_sha256, bp.output_sha256, p.publish_at AS p_at"
                + _WITHDRAWAL_COLS +
                " FROM batch_publications bp"
                " JOIN publications p ON p.doc_id = bp.doc_id AND p.version = bp.version"
                + _WITHDRAWAL_JOIN.format(t="bp") +
                " WHERE bp.batch_id = ? ORDER BY bp.position",
                (batch_id,),
            ).fetchall()
        return {
            "batch_id": batch["id"],
            "created_at": batch["created_at"],
            "count": len(rows),
            "documents": [
                _pub_entry(
                    r, ["position", "doc_id", "version", "annotation_revision",
                        "original_sha256", "output_sha256"]
                )
                for r in rows
            ],  # 顺序与原请求一致
            "publish_at": batch["publish_at"],
        }

    # ---------------- 版本撤回（永久，幂等） ----------------

    def _withdrawal_out(doc_id: str, version: int, row: Any) -> dict:
        return {
            "doc_id": doc_id,
            "version": version,
            "withdrawn": row is not None,
            "withdrawn_at": row["withdrawn_at"] if row is not None else None,
            "withdrawal_reason": row["reason"] if row is not None else None,
        }

    @app.post(
        "/documents/{doc_id}/publications/{version}/withdraw",
        status_code=201,
    )
    def withdraw_publication(
        doc_id: str, version: int, body: WithdrawRequest, response: Response
    ) -> dict:
        # 存在性校验、原因校验、"查重 -> 插入"在同一个 BEGIN IMMEDIATE
        # 写事务内完成：与发布、标注修改、其他撤回互斥串行，并发重复撤回
        # 恰好一个创建记录（201），其余返回首次记录（200）；任何失败整体
        # 回滚，不留下部分撤回记录。撤回只插入 publication_withdrawals 行，
        # 不删除发布记录与成品文件，版本号也不会被后续发布复用。
        with write_tx(settings.db_path) as conn:
            _get_publication(conn, doc_id, version)  # 未知文档/版本 -> 404
            reason = body.reason.strip()
            if not reason:
                _error(
                    400,
                    "reason_required",
                    "withdrawal reason must be non-empty",
                )
            existing = conn.execute(
                "SELECT reason, withdrawn_at FROM publication_withdrawals"
                " WHERE doc_id = ? AND version = ?",
                (doc_id, version),
            ).fetchone()
            if existing is not None:
                # 重复撤回：返回首次记录，不改写原因与时间
                response.status_code = 200
                return _withdrawal_out(doc_id, version, existing)
            now = _utcnow()
            conn.execute(
                "INSERT INTO publication_withdrawals"
                " (doc_id, version, reason, withdrawn_at) VALUES (?,?,?,?)",
                (doc_id, version, reason, now),
            )
        return _withdrawal_out(
            doc_id, version, {"reason": reason, "withdrawn_at": now}
        )

    @app.get("/documents/{doc_id}/publications/{version}/withdrawal")
    def get_withdrawal(doc_id: str, version: int) -> dict:
        with read_tx(settings.db_path) as conn:
            _get_publication(conn, doc_id, version)  # 未知文档/版本 -> 404
            row = conn.execute(
                "SELECT reason, withdrawn_at FROM publication_withdrawals"
                " WHERE doc_id = ? AND version = ?",
                (doc_id, version),
            ).fetchone()
        return _withdrawal_out(doc_id, version, row)

    # ---------------- 公开读取（只暴露发布版，不暴露原图） ----------------

    @app.get("/publications/{doc_id}/{version}")
    def get_publication(doc_id: str, version: int) -> dict:
        with read_tx(settings.db_path) as conn:
            pub = _get_publication(conn, doc_id, version)
            # 永久撤回优先于公开时间：已撤回版本一律 410
            _ensure_not_withdrawn(conn, doc_id, version)
            # 未到公开时间：404，不返回遮盖信息、摘要等任何版本内容
            _ensure_publish_time_reached(pub)
            rects = conn.execute(
                "SELECT x, y, w, h, reason FROM annotation_rects"
                " WHERE doc_id = ? AND revision = ? ORDER BY idx",
                (doc_id, pub["annotation_revision"]),
            ).fetchall()
        return {
            "doc_id": doc_id,
            "version": pub["version"],
            "annotation_revision": pub["annotation_revision"],
            "original_sha256": pub["original_sha256"],
            "output_sha256": pub["output_sha256"],
            "created_at": pub["created_at"],
            "redactions": [_rect_out(r) for r in rects],
            "publish_at": pub["publish_at"],
        }

    @app.get("/publications/{doc_id}/{version}/image")
    def get_publication_image(doc_id: str, version: int) -> FileResponse:
        with read_tx(settings.db_path) as conn:
            pub = _get_publication(conn, doc_id, version)
            _ensure_not_withdrawn(conn, doc_id, version)
            _ensure_publish_time_reached(pub)
        path = pub["output_path"]
        if not os.path.exists(path):
            _error(
                500,
                "artifact_missing",
                f"published artifact for version {version} is missing on disk",
            )
        return FileResponse(
            path,
            media_type="image/png",
            filename=f"{doc_id}-v{version}.png",
        )

    return app


app = create_app()
