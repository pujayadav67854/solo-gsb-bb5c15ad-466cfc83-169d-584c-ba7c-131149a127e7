#!/usr/bin/env bash
# 离线交付包端到端示例：
#   上传 3 份原图 -> 标注遮盖 -> 各自发布 v1 -> 按请求顺序申请离线交付包
#   -> 下载 ZIP（images/ 下各版 PNG + manifest.json 清单）
#   -> 用 Python 标准库校验：清单顺序、成品 SHA-256、遮盖数、包内文件名
#   -> 演示整包拒绝：撤回其中一份后重新申请 -> 410 publication_withdrawn
#   -> 数量越界 / 重复项 / 未知版本 明确报错（均不返回包）
# 依赖：curl、python3（仅用于解析 JSON、生成示例图、解 ZIP 校验）
set -euo pipefail

BASE="${BASE:-http://localhost:8000}"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

mkdoc() {  # $1=文件名 $2=R $3=G $4=B -> 输出 doc_id
  python3 - "$TMP/$1" "$2" "$3" "$4" <<'EOF'
import sys
from PIL import Image
path, r, g, b = sys.argv[1], *map(int, sys.argv[2:])
Image.new("RGB", (96, 72), (r, g, b)).save(path)
EOF
  curl -sS -F "file=@$TMP/$1" "$BASE/documents" \
    | python3 -c "import sys,json;print(json.load(sys.stdin)['doc_id'])"
}

publish_with_rects() {  # $1=doc_id $2=JSON 矩形数组（可为 []）
  local doc="$1" rects="$2" rev
  if [ "$rects" != "[]" ]; then
    rev=$(curl -sS "$BASE/documents/$doc/annotations" \
      | python3 -c "import sys,json;print(json.load(sys.stdin)['revision'])")
    curl -sS -X PUT "$BASE/documents/$doc/annotations" \
      -H 'Content-Type: application/json' \
      -d "{\"revision\":$rev,\"rects\":$rects}" >/dev/null
  fi
  curl -sS -X POST "$BASE/documents/$doc/publish" >/dev/null
}

echo "== 1. 上传 3 份文档并发布（v1 遮盖数分别为 1、2、0）=="
D1=$(mkdoc d1.png 120 60 200)
D2=$(mkdoc d2.png 20 180 90)
D3=$(mkdoc "my upload (3).jpg" 200 120 20)
publish_with_rects "$D1" '[{"x":8,"y":8,"w":30,"h":12,"reason":"含身份证号"}]'
publish_with_rects "$D2" '[{"x":4,"y":4,"w":10,"h":10,"reason":"姓名"},{"x":40,"y":40,"w":20,"h":20,"reason":"工号"}]'
publish_with_rects "$D3" '[]'
echo "D1=$D1"
echo "D2=$D2"
echo "D3=$D3"

echo
echo "== 2. 申请离线交付包：2～20 个互不重复的 (doc_id, version)，按此顺序入清单 =="
# 注意顺序 D2 -> D1 -> D3；包内文件名由服务端按 doc_id+version 生成，
# 与上传文件名（如 "my upload (3).jpg"）无关。
curl -sS -X POST "$BASE/delivery-packages" \
  -H 'Content-Type: application/json' \
  -d "{\"items\":[
    {\"doc_id\":\"$D2\",\"version\":1},
    {\"doc_id\":\"$D1\",\"version\":1},
    {\"doc_id\":\"$D3\",\"version\":1}
  ]}" -o "$TMP/package.zip"
echo "已保存 $TMP/package.zip（HTTP 200 application/zip）"

echo
echo "== 3. 解包并校验清单与成品摘要 =="
D1="$D1" D2="$D2" D3="$D3" TMP="$TMP" python3 - <<'EOF'
import hashlib, json, os, zipfile

zf = zipfile.ZipFile(os.environ["TMP"] + "/package.zip")
names = zf.namelist()
print("包内条目：")
for n in names:
    print(" ", n)
assert names[-1] == "manifest.json" and all(n.startswith("images/") for n in names[:-1])

manifest = json.loads(zf.read("manifest.json"))
expected_order = [os.environ["D2"], os.environ["D1"], os.environ["D3"]]
assert [it["doc_id"] for it in manifest["items"]] == expected_order
assert manifest["count"] == 3
assert [it["redaction_count"] for it in manifest["items"]] == [2, 1, 0]
for it in manifest["items"]:
    data = zf.read(it["filename"])
    assert data[:8] == b"\x89PNG\r\n\x1a\n", "每个成品都是 PNG"
    assert hashlib.sha256(data).hexdigest() == it["output_sha256"], "成品 SHA-256 与清单一致"
    assert it["filename"] == f"images/{it['doc_id']}-v{it['version']}.png"
print("清单（按请求顺序）：")
print(json.dumps(manifest, ensure_ascii=False, indent=2))
print("校验通过：顺序、SHA-256、遮盖数、PNG、包内文件名规则全部符合。")
EOF

echo
echo "== 4. 任一项已撤回 -> 整包拒绝（410），不返回 ZIP、不泄露成品/遮盖详情 =="
curl -sS -X POST "$BASE/documents/$D1/publications/1/withdraw" \
  -H 'Content-Type: application/json' -d '{"reason":"遮盖遗漏：页脚工号未遮盖"}' >/dev/null
curl -sS -w "\nHTTP %{http_code} %{content_type}\n" -X POST "$BASE/delivery-packages" \
  -H 'Content-Type: application/json' \
  -d "{\"items\":[
    {\"doc_id\":\"$D2\",\"version\":1},
    {\"doc_id\":\"$D1\",\"version\":1},
    {\"doc_id\":\"$D3\",\"version\":1}
  ]}"

echo
echo "== 5. 其他明确错误 =="
echo "-- 数量越界（仅 1 项）：400 invalid_delivery_size --"
curl -sS -w "\nHTTP %{http_code}\n" -X POST "$BASE/delivery-packages" \
  -H 'Content-Type: application/json' \
  -d "{\"items\":[{\"doc_id\":\"$D2\",\"version\":1}]}"
echo "-- 重复项（同一 doc_id+version 出现两次；同文档不同版本不算重复）：400 duplicate_delivery_item --"
curl -sS -w "\nHTTP %{http_code}\n" -X POST "$BASE/delivery-packages" \
  -H 'Content-Type: application/json' \
  -d "{\"items\":[
    {\"doc_id\":\"$D2\",\"version\":1},
    {\"doc_id\":\"$D3\",\"version\":1},
    {\"doc_id\":\"$D2\",\"version\":1}
  ]}"
echo "-- 未知版本：404 unknown_version（附 position/doc_id/version） --"
curl -sS -w "\nHTTP %{http_code}\n" -X POST "$BASE/delivery-packages" \
  -H 'Content-Type: application/json' \
  -d "{\"items\":[{\"doc_id\":\"$D2\",\"version\":1},{\"doc_id\":\"$D3\",\"version\":99}]}"

echo
echo "== 6. 去掉已撤回项后，其余两份仍可正常交付（撤回不影响其他版本）=="
curl -sS -X POST "$BASE/delivery-packages" \
  -H 'Content-Type: application/json' \
  -d "{\"items\":[{\"doc_id\":\"$D2\",\"version\":1},{\"doc_id\":\"$D3\",\"version\":1}]}" \
  -o "$TMP/package-ok.zip" -w "HTTP %{http_code}, %{size_download} bytes -> package-ok.zip\n"
