# 档案影像脱敏发布服务

档案员上传 PNG/JPEG 原图，在 **EXIF 旋正后的像素坐标系** 上标注敏感矩形及原因；
发布时把当前标注矩形不可逆涂实为纯黑，生成剥离元数据的独立 PNG，并固化
原图摘要、标注修订号与成品摘要。已发布成品不随后续标注变化而改变；
公开读取只暴露发布版与遮盖信息，永不返回原图。发布（单份或同批次）时可
指定统一的 **UTC 公开时间**：到点前公开版详情与图片一律 404，到点后自动
可读；省略时仍立即公开。已发布版本发现遮盖遗漏时，
可凭文档 ID、版本号与非空原因**永久撤回**：公开读取立即拒绝（优先于公开
时间），发布记录、成品文件与版本号序列全部保留。

技术栈：Python 3.11+ · FastAPI · Pillow · SQLite（WAL）

## 一键启动（Docker Compose）

```bash
docker compose up --build
```

- 服务地址：http://localhost:8000
- 交互式 API 文档（Swagger UI）：http://localhost:8000/docs
- 健康检查：http://localhost:8000/health

数据持久化在名为 `redaction-data` 的卷中（SQLite 库、原图、发布成品）。

## 本地运行

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

运行测试：

```bash
pip install -r requirements-dev.txt
python -m pytest tests/ -q
```

## 配置

| 环境变量        | 默认值   | 说明                                       |
| --------------- | -------- | ------------------------------------------ |
| `DATA_DIR`      | `./data`（容器内 `/data`） | SQLite 库、原图、发布成品的存放根目录 |
| `MAX_UPLOAD_MB` | `25`     | 单个上传文件大小上限（MB）                 |

监听地址/端口通过 uvicorn 参数（`--host`/`--port`）或 Compose 的 `ports` 映射调整。
首次启动时应用自动建目录、建表（初始化无需手工步骤）。

## API 一览

| 方法 | 路径 | 说明 |
| ---- | ---- | ---- |
| POST | `/documents` | 上传原图（multipart 字段名 `file`），返回 `doc_id` 与旋正后宽高 |
| GET  | `/documents/{doc_id}` | 文档元数据与发布列表 |
| GET  | `/documents/{doc_id}/annotations` | 当前标注修订号与矩形 |
| PUT  | `/documents/{doc_id}/annotations` | 整体替换标注，须携带当前修订号 |
| POST | `/documents/{doc_id}/publish` | 按当前标注发布新版本（涂实+去元数据 PNG）；可选 JSON 体 `{"publish_at": "..."}` 指定 UTC 公开时间，省略立即公开 |
| GET  | `/documents/{doc_id}/publications` | 发布版本列表（含公开时间、撤回状态） |
| POST | `/documents/{doc_id}/publications/{version}/withdraw` | 凭非空原因**永久撤回**该版本（重复撤回返回首次记录） |
| GET  | `/documents/{doc_id}/publications/{version}/withdrawal` | 查询该版本的撤回状态、时间与原因 |
| POST | `/publication-batches` | **同批次发布 2～20 份文档**（原子：全成功或整批失败）；可选 `idempotency_key` 防重发、可选 `publish_at` 指定整批统一 UTC 公开时间 |
| GET  | `/publication-batches/{batch_id}` | 按批次标识查询清单（顺序与请求一致，含公开时间、撤回状态） |
| GET  | `/publications/{doc_id}/{version}` | 公开读取：版本元数据+遮盖坐标与原因（未到公开时间 404） |
| GET  | `/publications/{doc_id}/{version}/image` | 公开读取：发布版 PNG（未到公开时间 404） |

## 调用示例（curl）

```bash
BASE=http://localhost:8000

# 1) 上传原图 -> 记下 doc_id、width/height（EXIF 旋正后的坐标系）
DOC=$(curl -s -F "file=@scan.jpg" $BASE/documents)
DOC_ID=$(echo "$DOC" | python3 -c "import sys,json;print(json.load(sys.stdin)['doc_id'])")

# 2) 标注敏感矩形（x,y 为左上角，w/h 为宽高，坐标基于旋正后图像）
curl -s -X PUT $BASE/documents/$DOC_ID/annotations \
  -H 'Content-Type: application/json' \
  -d '{"revision":0,"rects":[{"x":120,"y":80,"w":200,"h":40,"reason":"含身份证号"}]}'
# -> {"doc_id":"...","revision":1,...}

# 2b) 携带过期修订号会被拒绝：HTTP 409 {"detail":{"code":"stale_revision","current_revision":1,...}}

# 3) 发布 -> 固化 annotation_revision、original_sha256、output_sha256
curl -s -X POST $BASE/documents/$DOC_ID/publish
# -> {"version":1,"annotation_revision":1,"output_sha256":"...","publish_at":null,...}

# 3b) 也可指定统一的 UTC 公开时间（晚于当前时刻、不超过 30 天）：
#     发布当下即固化成品并占用版本号，但到点前公开读取返回 404
curl -s -X POST $BASE/documents/$DOC_ID/publish \
  -H 'Content-Type: application/json' \
  -d '{"publish_at":"2026-10-06T08:00:00Z"}'
# -> {"version":2,...,"publish_at":"2026-10-06T08:00:00Z"}

# 4) 公开读取（不含原图）
curl -s $BASE/publications/$DOC_ID/1          # 版本元数据 + 遮盖坐标/原因
curl -s -OJ $BASE/publications/$DOC_ID/1/image # 发布版 PNG

# 5) 发现遮盖遗漏：凭文档 ID + 版本号 + 非空原因永久撤回 v1
curl -s -X POST $BASE/documents/$DOC_ID/publications/1/withdraw \
  -H 'Content-Type: application/json' \
  -d '{"reason":"遮盖遗漏：右下角工号未遮盖"}'
# -> HTTP 201 {"withdrawn":true,"withdrawn_at":"...","withdrawal_reason":"...",...}
# 重复撤回 -> HTTP 200 返回首次记录，原因不被改写

# 6) 撤回后公开读取返回 410；详情/列表仍保留记录并标识撤回状态
curl -s $BASE/publications/$DOC_ID/1           # 410 publication_withdrawn
curl -s $BASE/documents/$DOC_ID/publications   # v1 条目 withdrawn=true
```

完整可运行脚本：`examples/demo.sh`（单份流程）、`examples/demo_batch.sh`
（批次发布流程）、`examples/demo_idempotency.sh`（批次幂等键流程）、
`examples/demo_scheduled_publication.sh`（定时公开流程）与
`examples/demo_withdrawal.sh`（版本撤回流程）；
先启动服务，再执行 `bash examples/demo_scheduled_publication.sh`。

## 定时公开（统一 UTC 公开时间 `publish_at`）

单份发布与同批次发布都可以指定**统一的 UTC 公开时间**（RFC 3339 / ISO 8601
日期时间，**必须带时区偏移**，推荐尾部 `Z` 表示 UTC，如
`2026-10-06T08:00:00Z`；带偏移的时间会被换算并规范化为 UTC）：

- **窗口（仅首次提交校验）**：公开时间必须**严格晚于当前时刻**，且距当前
  **不超过 30 天**；过去时间、等于当前时刻、超过 30 天、无法解析或不带
  时区偏移均返回 `400 invalid_publish_time`。省略请求体、省略 `publish_at`
  或显式传 `null` 时行为与之前完全一致：**立即公开**（响应 `publish_at`
  为 `null`）。
- **发布当下即固化**：定时发布与普通发布走同一条路径——在发布事务内渲染
  涂实成品、写盘并占用**正常版本号**（`MAX(version)+1`），版本不可变；
  后续标注变化不影响该版本，后续发布继续续号。
- **到点前对外 404**：公开时间到达前，
  `GET /publications/{doc}/{version}` 与 `.../image` 一律返回
  `404 publication_not_yet_public`——与"版本不存在"同形态，**不返回遮盖
  坐标/原因、摘要，也不返回成品文件**。时间到达后**无需再次操作**即可读取。
- **响应携带公开时间**：单份发布响应在原字段后追加 `publish_at`；批次发布
  与批次查询在顶层返回 `publish_at`，批次各条目、文档详情、发布列表的发布
  条目也在原字段之后追加 `publish_at`（原字段及顺序不变；立即公开为
  `null`）。
- **永久撤回优先**：对未到公开时间的版本撤回后，在到点前后都返回
  `410 publication_withdrawn`，**时间到达也不会恢复**；未撤回版本到点后
  正常可读。
- **失败不留痕迹**：公开时间无效（或批次中任一文档失败）时不插入
  `publications`/批次记录、不落盘成品（落盘的也会清理）、不占用版本号与
  幂等键，可修正后用原参数/原键重试。

```bash
BASE=http://localhost:8000
# 单份：12 小时后公开
curl -s -X POST $BASE/documents/$DOC_ID/publish \
  -H 'Content-Type: application/json' \
  -d '{"publish_at":"2026-10-06T01:00:00Z"}'
# -> 201 {"version":1,...,"created_at":"...","publish_at":"2026-10-06T01:00:00Z"}
# 到点前：
#   GET /publications/$DOC_ID/1        -> 404 publication_not_yet_public
#   GET /publications/$DOC_ID/1/image  -> 404 publication_not_yet_public
# 到点后同样的请求自动变为 200，无需任何额外操作。

# 整批统一公开时间（顶层 publish_at 对批次内每份文档生效）
curl -s -X POST $BASE/publication-batches \
  -H 'Content-Type: application/json' \
  -d '{
    "publish_at": "2026-10-06T01:00:00Z",
    "idempotency_key": "req-20261005-01",
    "documents": [
      {"doc_id": "DOC_ID_1", "expected_revision": 1},
      {"doc_id": "DOC_ID_2", "expected_revision": 1}
    ]
  }'
# -> 201 {"batch_id":"...","count":2,"documents":[...],"publish_at":"2026-10-06T01:00:00Z"}
```

端到端示例（含到点前后变化、无效时间、幂等重放、撤回优先）：
`bash examples/demo_scheduled_publication.sh`。

## 同批次发布（2～20 份，整批原子）

档案员把一批**已标注**影像作为同一批次公开。请求携带 2～20 个**不同**文档 ID
及各自**预期标注修订号**：仅当全部文档都存在、且预期修订号仍与服务端当前修订号
匹配时，才在同一写事务内为每份文档按当前标注生成去元数据、纯黑涂实的独立发布版。
任一文档无效、修订过期、原图无法读取或成品写入失败，**整批失败**：不留下任何可
公开的部分版本，不占用任何版本号，也不生成批次标识。批次内渲染与落盘与同时发生
的标注修改、单份发布、其他批次发布互斥串行；已发布版本仍不可变，单份发布与公开
读取的响应及行为保持兼容（批次产物与单份产物共用同一张 `publications` 表、
同一个 `/publications/...` 公开入口）。

```bash
BASE=http://localhost:8000
curl -sS -X POST $BASE/publication-batches \
  -H 'Content-Type: application/json' \
  -d '{
    "documents": [
      {"doc_id": "DOC_ID_1", "expected_revision": 1},
      {"doc_id": "DOC_ID_2", "expected_revision": 1},
      {"doc_id": "DOC_ID_3", "expected_revision": 0}
    ]
  }'
```

成功 `201`：返回批次标识、创建时间，以及按**请求顺序**排列的各文档结果
（版本、固化的标注修订号、原图/成品摘要、遮盖数量）：

```json
{
  "batch_id": "7bc8e9008700469fb1c896990b34e620",
  "created_at": "2026-10-04T19:18:21+00:00",
  "count": 3,
  "documents": [
    {"position": 0, "doc_id": "DOC_ID_1", "version": 1,
     "annotation_revision": 1, "original_sha256": "…",
     "output_sha256": "…", "redaction_count": 1, "created_at": "…"},
    {"position": 1, "doc_id": "DOC_ID_2", "version": 1, "…": "…"},
    {"position": 2, "doc_id": "DOC_ID_3", "version": 1, "…": "…"}
  ],
  "publish_at": null
}
```

请求顶层可加 `"publish_at": "2026-10-06T08:00:00Z"` 为整批指定统一的
UTC 公开时间（规则见上一节）；响应与批次查询顶层的 `publish_at` 即该时间
（立即公开为 `null`），批次内每份文档到点前均 404、到点后自动可读。

按批次标识查询（清单顺序与原请求一致）：

```bash
curl -sS $BASE/publication-batches/7bc8e9008700469fb1c896990b34e620
```

每份文档的版本号仍在该文档维度内按 `MAX(version)+1` 连续分配，与单份发布
共用序列（先发过 v1 的文档，在成功批次里拿到 v2）。失败响应在统一错误体外
附带可定位信息：`position`（请求中的下标，从 0 起）、`doc_id`，修订过期时
还附 `current_revision`/`supplied_revision`：

```bash
# HTTP 409
# {"detail":{"code":"stale_revision","message":"…",
#   "position":1,"doc_id":"DOC_ID_2",
#   "current_revision":2,"supplied_revision":1}}
```

## 批次幂等键（可选，防重发）

批量发布时网络中断可能使成功响应丢失，调用方无法判断批次是否已生效。
`POST /publication-batches` 接受可选的 **`idempotency_key`**（1～128 字符，
调用方保证**全局唯一**）；键与**按请求顺序排列**的 `(doc_id, expected_revision)`
列表及整批统一的 **`publish_at`**（含"立即公开"的 `null`）绑定：

- **首次成功（201）**：键与完整成功结果在同一事务内持久化。
- **相同键 + 相同内容重试（200）**：直接返回首次持久化的原批次标识、各文档
  版本与摘要（响应体与首次逐字节一致）——即使标注随后已变化、**即使首次
  指定的公开时间此刻已经过去**，也**不再校验时间窗口**、不再校验当前修订号、
  不重新渲染、不产生第二批版本。
- **相同键 + 不同内容（409 `idempotency_key_conflict`）**：明确冲突，
  文档集、修订号、顺序或 **`publish_at`**（定时 vs 立即、不同时间）任一
  不同都算；原批次不受任何影响，冲突请求自身也不留任何痕迹。
- **失败不占键**：任一校验失败（数量/重复/文档缺失/修订过期/
  **`publish_at` 不在允许窗口内或格式无效**）、原图读取失败
  或成品落盘失败都不会占用键，也不产生部分版本；修正后可用**原键**重试。
- **并发重试最多发布一次**：键的检查、发布、登记在同一写事务内串行化，
  并发相同键请求中恰好一个首次成功（201），其余为重放（200），批次标识一致。
- 未带键的请求行为与之前完全一致：每次调用独立发布。

```bash
BASE=http://localhost:8000
KEY="req-$(date +%s)"   # 调用方生成全局唯一键

# 首次发布（201）
curl -sS -X POST $BASE/publication-batches \
  -H 'Content-Type: application/json' \
  -d "{
    \"idempotency_key\": \"$KEY\",
    \"documents\": [
      {\"doc_id\": \"DOC_ID_1\", \"expected_revision\": 1},
      {\"doc_id\": \"DOC_ID_2\", \"expected_revision\": 1}
    ]
  }"

# 网络中断后原样重发同一请求 -> HTTP 200，返回首次的批次标识/版本/摘要，
# 不生成第二批版本
curl -sS -X POST $BASE/publication-batches \
  -H 'Content-Type: application/json' \
  -d "{
    \"idempotency_key\": \"$KEY\",
    \"documents\": [
      {\"doc_id\": \"DOC_ID_1\", \"expected_revision\": 1},
      {\"doc_id\": \"DOC_ID_2\", \"expected_revision\": 1}
    ]
  }"

# 相同键但内容不同（文档集、修订号、顺序或 publish_at 任一变化）
# -> HTTP 409 {"detail":{"code":"idempotency_key_conflict","message":"…"}}
```

端到端示例：`bash examples/demo_idempotency.sh`。

## 版本撤回（永久，幂等）

已发布版本发现遮盖遗漏时，档案员凭**文档 ID + 版本号 + 非空原因**永久撤回：

```bash
BASE=http://localhost:8000
curl -sS -X POST $BASE/documents/$DOC_ID/publications/1/withdraw \
  -H 'Content-Type: application/json' \
  -d '{"reason":"遮盖遗漏：右下角工号未遮盖"}'
# 首次撤回 HTTP 201：
# {"doc_id":"…","version":1,"withdrawn":true,
#  "withdrawn_at":"2026-10-05T…+00:00","withdrawal_reason":"遮盖遗漏：…"}
```

- **永久且保留记录**：撤回只登记撤回记录，不删除 `publications` 行、成品
  文件或批次记录，版本号不复用——后续单份/批次发布继续 `MAX(version)+1`。
- **公开面立即生效（优先于公开时间）**：撤回后新发起的
  `GET /publications/{doc}/{version}` 与 `.../image` 返回
  **410 `publication_withdrawn`**，不返回成品与遮盖详情；即使该版本带有
  尚未到达的公开时间，时间到达也**不会恢复访问**。成品文件保留在磁盘上，
  撤回生效前已开始的下载允许完成。
- **幂等**：重复撤回返回首次记录（HTTP 200），撤回时间与原因不被改写；
  并发撤回与发布在同一写事务内串行化，恰好一个请求创建记录，
  失败不留部分撤回记录。
- **可查询**：`GET /documents/{doc_id}/publications/{version}/withdrawal`
  返回该版本的撤回状态、时间与原因；文档详情、发布列表、批次查询的每个
  版本条目均在原字段之后追加 `publish_at` 及
  `withdrawn`/`withdrawn_at`/`withdrawal_reason`
  （原字段及顺序不变；未撤回为 `false`/`null`/`null`，立即公开的
  `publish_at` 为 `null`）。
- **错误**：未知文档 404 `document_not_found`、未知版本 404
  `unknown_version`、空白原因 400 `reason_required`。
- **批次幂等键重放**仍原样返回首次保存的响应（HTTP 200），
  但不能恢复已撤回版本的公开访问。

端到端示例：`bash examples/demo_withdrawal.sh`。


## 错误响应

统一格式：`{"detail": {"code": "...", "message": "..."}}`

| HTTP | code | 触发场景 |
| ---- | ---- | -------- |
| 400 | `empty_upload` | 上传空文件 |
| 400 | `image_decode_failed` | 字节无法解码为图像（含截断/损坏） |
| 400 | `empty_rectangle` | 矩形宽或高 ≤ 0 |
| 400 | `rectangle_out_of_bounds` | 矩形越出旋正后图像边界 |
| 400 | `reason_required` | 矩形未填写原因；撤回原因为空白 |
| 400 | `invalid_publish_time` | `publish_at` 无法解析、不带时区偏移、不晚于当前时刻或晚于当前超过 30 天 |
| 404 | `document_not_found` | 文档不存在（批次场景附 `position`/`doc_id`） |
| 404 | `unknown_version` | 发布版本不存在 |
| 404 | `publication_not_yet_public` | 版本存在但公开时间尚未到达（详情与图片一致，不泄露任何版本内容） |
| 404 | `batch_not_found` | 批次标识不存在 |
| 410 | `publication_withdrawn` | 公开读取已撤回的版本（不返回成品与遮盖详情） |
| 400 | `invalid_batch_size` | 批次文档数不在 2～20（附 `count`） |
| 400 | `duplicate_document_in_batch` | 批次内文档 ID 重复（附 `position`/`doc_id`） |
| 409 | `stale_revision` | 标注修订号过期（响应附 `current_revision`；批次场景另附 `position`/`doc_id`/`supplied_revision`） |
| 409 | `idempotency_key_conflict` | 幂等键已用于不同内容的批次请求（原批次不受影响） |
| 500 | `original_unavailable` | 存储的原图无法读取/解码（批次场景附 `position`/`doc_id`） |
| 500 | `artifact_write_failed` | 发布成品写入磁盘失败（附 `position`/`doc_id`/`version`） |
| 413 | `payload_too_large` | 超过上传大小上限 |
| 415 | `unsupported_media_type` | 非 PNG/JPEG（如 GIF） |

## 一致性设计

- **坐标系**：上传时以 `ImageOps.exif_transpose` 旋正，标注、涂实、发布全部在旋正后
  的像素坐标上进行，与查看器所见方向一致。
- **乐观锁**：`PUT /documents/{id}/annotations` 要求 `revision` 等于服务端当前修订号，
  在 `BEGIN IMMEDIATE` 事务内 check-and-set，过期即 409，不会丢失他人修改。
- **发布事务**：读取当前修订号与矩形、渲染涂实、写盘、插入发布记录在同一写事务中完成；
  与标注修改互斥串行，因此成品文件与记录必然对应同一修订。
- **批次原子性**：`POST /publication-batches` 在**一个** `BEGIN IMMEDIATE` 事务内
  按请求顺序先完成全部"存在性 + 修订号"校验，全部通过后才逐份渲染落盘并登记
  `publications`/`publication_batches`/`batch_publications`；与标注修改、单份发布、
  其他批次发布互斥。任一文档不存在、修订号过期、原图不可读、成品写盘失败，事务整体
  回滚并清理已落盘文件——既不产生批次标识，也没有任何 `publications` 行，因此
  `MAX(version)+1` 不会被失败批次推进（不占版本号、不留部分版本）。成功批次的每份
  版本与单份发布共用 `publications` 序列；批次清单以 `position` 固化请求次序。
- **批次幂等**：携带 `idempotency_key` 时，键的"检查 → 发布 → 登记"与其余批次
  写入同在上述写事务内完成。首次成功即将键、请求内容摘要（有序文档 ID + 预期
  修订号 + 规范化后的 `publish_at` 的 SHA-256）与完整成功响应体落入
  `batch_idempotency` 表；重试命中同键同内容时直接返回持久化响应（HTTP 200，
  不再校验时间窗口/修订号、不重新渲染），命中同键不同内容（含不同公开时间）
  时 409 冲突且原批次不变；任何失败回滚会一并撤销键记录，键不被占用；
  并发同键请求被写锁串行化，最多发布一次。
- **不可变性**：标注矩形按 `(doc_id, revision)` 追加存储、历史修订永不改写；
  发布版 PNG 落盘后不改动，后续标注变化不影响已发布成品（批次产物同样如此）。
- **版本撤回**：撤回的"校验 → 查重 → 登记"在同一个 `BEGIN IMMEDIATE`
  写事务内完成，与标注修改、单份/批次发布、其他撤回互斥串行；撤回记录按
  `(doc_id, version)` 主键追加、永不改写——重复撤回（含并发）命中主键直接
  返回首次记录，恰好一个请求创建记录，失败整体回滚不留部分记录。撤回只插入
  `publication_withdrawals` 行：`publications` 记录、成品文件与版本号序列全部
  保留（后续发布继续 `MAX(version)+1`）；撤回生效后新发起的公开读取返回 410，
  生效前已开始的下载因成品文件保留而允许完成。
- **脱敏**：矩形区域以纯黑不可逆涂实；发布图重建为干净像素容器后另存为 PNG，
  不携带 EXIF/ICC/文本等任何原图元数据；无标注时也生成去元数据的发布版。
- **定时公开**：`publish_at` 在解析为 UTC 后随发布记录（批次还随
  `publication_batches`）持久化；成品在发布当下就已渲染落盘并占用正常版本号，
  "未到点"只是公开读取面的判定（`publish_at > 当前 UTC 时间` →
  404 `publication_not_yet_public`），因此到点无需任何补操作、无后台任务。
  该判定在撤回检查之后：已撤回版本恒为 410，时间到达不恢复。公开时间的
  窗口校验（晚于当前、≤30 天）只在首次请求的写事务内进行，幂等重放直接
  返回首次结果、不重新校验。
- **公开面**：`/publications/...` 只返回发布版成品、遮盖坐标与原因及摘要值，
  原图没有任何对外读取入口；未到公开时间的版本返回 404（不返回成品、遮盖
  详情与摘要），已撤回版本返回 410。

## 目录结构

```
app/
  main.py     # FastAPI 路由与事务编排
  db.py       # SQLite 模式与事务助手（WAL + BEGIN IMMEDIATE）
  imaging.py  # 解码、EXIF 旋正、涂实、去元数据渲染
  models.py   # 请求体模型（含批次请求）
  config.py   # 环境变量配置
tests/test_api.py    # 16 个用例：含 EXIF、乐观锁、错误码、不可变性、并发一致性
tests/test_batch.py  # 12 个用例：批次成功/顺序、数量与重复、文档缺失/修订过期/
                     #   原图不可读/写盘失败的整批回滚、版本交错、不可变性、并发
tests/test_idempotency.py  # 10 个用例：幂等重放、内容绑定（含公开时间）、冲突、
                           #   各类失败不占键、并发同键最多发布一次、无键请求兼容
tests/test_withdrawal.py   # 10 个用例：撤回/查询、公开读取 410、幂等重复、
                           #   列表标识、版本号不复用、批次重放不恢复、并发一致性
tests/test_scheduled_publication.py  # 22 个用例：单份/批次定时发布、到点前后
                                     #   404→200、不可变、无效时间不留痕、时区
                                     #   规范化、幂等内容纳入公开时间、撤回优先
examples/demo.sh             # 单份端到端 curl 示例
examples/demo_batch.sh       # 批次发布端到端 curl 示例
examples/demo_idempotency.sh # 批次幂等键端到端 curl 示例
examples/demo_scheduled_publication.sh  # 定时公开端到端 curl 示例
examples/demo_withdrawal.sh  # 版本撤回端到端 curl 示例
Dockerfile / docker-compose.yml
```

## 本轮变动（定时公开 publish_at）

- 单份发布 `POST /documents/{doc_id}/publish` 新增**可选 JSON 请求体**
  `{"publish_at": "RFC3339 时间"}`（省略请求体的既有调用方式不变）；
  批次发布 `POST /publication-batches` 顶层新增可选 `publish_at`，对批次内
  每份文档统一生效。时间必须带时区偏移（推荐 UTC 尾部 `Z`，其他偏移换算为
  UTC 并规范化）；首次提交时必须严格晚于当前时刻且不超过 30 天，否则
  `400 invalid_publish_time`；省略或 `null` 仍立即公开。
- 定时版本在发布当下即生成不可变成品、写盘并占用**正常版本号**
  （与立即公开共用 `MAX(version)+1` 序列与公开入口）；公开时间到达前，
  `/publications/{doc}/{version}` 与 `.../image` 一律返回
  `404 publication_not_yet_public`，不返回遮盖信息、摘要或成品文件；
  到点后无需再次操作即可读取（无后台任务，读取时按当前 UTC 时间判定）。
- 单份发布响应、批次发布/批次查询顶层及各发布条目（文档详情、发布列表、
  批次清单）均在原字段之后追加 `publish_at`（立即公开为 `null`），原字段
  及顺序保持不变。
- 永久撤回优先于公开时间：对未到点版本撤回后恒为 `410
  publication_withdrawn`，时间到达也不恢复；撤回记录、成品与版本号照旧保留。
- 批次幂等键的内容绑定纳入 `publish_at`：同键同内容（含相同公开时间）
  重放即使时间已过也原样返回首次结果（200），不再校验时间窗口；同键不同
  公开时间（含一边定时、一边立即）报 `409 idempotency_key_conflict`。
- 无效时间与批次任一文档失败一样整批回滚：不留版本、批次、成品文件，
  也不占用幂等键，可修正后用原参数/原键重试。
- 存储：`publications`、`publication_batches` 新增可空列 `publish_at`，
  旧数据卷启动时自动补列（`ALTER TABLE` 轻量迁移）；旧数据该列为 NULL，
  等同立即公开。
- 上传、标注、撤回、公开读取及未携带 `publish_at` 的既有路由、请求与响应
  字段均未改动，行为完全兼容。

## 历史变动（版本撤回）

- 新增 `POST /documents/{doc_id}/publications/{version}/withdraw`：凭非空原因
  永久撤回已发布版本（首次 201；重复撤回 200 返回首次记录，不改写原因与时间）；
  新增 `GET /documents/{doc_id}/publications/{version}/withdrawal` 查询该版本的
  撤回状态、时间与原因。未知文档/版本沿用 404 `document_not_found`/
  `unknown_version`，空白原因 400 `reason_required`。
- 新增表 `publication_withdrawals`（`(doc_id, version)` 主键、原因、撤回时间）；
  旧数据卷启动时自动建表（`CREATE TABLE IF NOT EXISTS` 轻量迁移）。
- 撤回生效后，新发起的公开元数据与图片读取返回 410 `publication_withdrawn`，
  不返回成品与遮盖详情；发布记录、成品文件与版本号序列保留，后续单份/批次
  发布继续原序列；生效前已开始的下载允许完成。
- 文档详情、发布列表、批次查询的版本条目末尾追加
  `withdrawn`/`withdrawn_at`/`withdrawal_reason`，原字段及顺序不变。
- 批次幂等键重放仍返回首次保存的响应，但不能恢复撤回版本的公开访问；
  并发撤回与发布在同一写事务内串行化，失败不留部分撤回记录。
- 上传、标注、单份/批次发布、公开读取的既有路由、请求与响应字段均未改动，
  行为完全兼容。

## 历史变动（批次发布幂等键）

- `POST /publication-batches` 新增可选字段 `idempotency_key`（1～128 字符，
  调用方保证全局唯一），与按请求顺序排列的 `(doc_id, expected_revision)` 绑定。
- 首次成功（201）持久化键与完整成功结果；相同键+相同内容重试返回 HTTP 200
  及首次的原批次标识、各文档版本与摘要，不再校验当前修订号、不重新渲染；
  相同键+不同内容返回 409 `idempotency_key_conflict`，原批次不受影响。
- 新增表 `batch_idempotency`（键、请求内容摘要、批次标识、持久化响应体）；
  旧数据卷启动时自动建表（`CREATE TABLE IF NOT EXISTS` 轻量迁移）。
- 任一校验、原图读取或成品落盘失败均不占用键、不产生部分版本，可用原键重试；
  并发同键重试在同一写事务内串行化，最多发布一次。
- 未带键的批次请求、单份发布与公开读取的路由、请求与响应字段均未改动，
  行为完全兼容；带键批次产物同样共用版本序列与公开入口。

## 历史变动（同批次发布）

- 新增 `POST /publication-batches`（2～20 份不同文档、各带预期修订号，整批原子）
  与 `GET /publication-batches/{batch_id}`（按批次标识查询，顺序与请求一致）。
- 新增表 `publication_batches`、`batch_publications`，并在 `publications` 增加
  可空列 `batch_id`；旧数据卷启动时自动补列（`ALTER TABLE` 轻量迁移）。
- 新增错误码：`invalid_batch_size`、`duplicate_document_in_batch`、
  `batch_not_found`、`original_unavailable`、`artifact_write_failed`；批次场景的
  `document_not_found`/`stale_revision` 附 `position`、`doc_id` 等定位字段。
- 既有单份发布、公开读取的路由、请求与响应字段均未改动，行为完全兼容；
  批次产物与单份产物共用版本序列与公开入口，已发布版本依旧不可变。
