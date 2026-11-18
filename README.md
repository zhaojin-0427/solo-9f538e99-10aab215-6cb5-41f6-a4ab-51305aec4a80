# 地震波形片段拼接服务（Waveform Assembly API）

把乱序到达的波形片段按 **台站 + UTC 分钟** 收集、校验、拼装，
仅对 **完整且无冲突** 的分钟生成不可变的发布记录，供震相识别程序取用。

- 框架：FastAPI（自带 `/docs` Swagger UI、`/redoc`）
- 存储：SQLite（WAL 模式，单文件，挂在 `/data` 卷上，重启数据不变）
- 并发：所有写操作走 `BEGIN IMMEDIATE` 事务，上传与发布交错时在同一已提交片段集合上串行执行

## 一、启动

### Docker Compose（推荐）

```bash
docker compose up --build
# API:    http://localhost:8000
# 文档:   http://localhost:8000/docs
# 健康:   http://localhost:8000/health
```

数据库保存在命名卷 `waveform-data`（容器内 `/data/waveform.db`）。

### 本地直接运行

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
WAVEFORM_DB_PATH=./data/waveform.db uvicorn app.main:app --reload
```

## 二、数据模型与规则

片段（fragment）：

| 字段 | 约束 |
| --- | --- |
| `station` | 非空字符串，台站代码 |
| `minute_utc` | 整分钟 UTC 时间（秒必须为 0），如 `2026-11-17T05:06:00Z` |
| `fragment_no` | 片段号，非负整数 |
| `revision` | 修订号，**正整数** |
| `start_sample` | 起始采样位，0–59 |
| `samples` | 非空整数数组；`start_sample + len(samples) ≤ 60`，越界拒绝（422） |

修订与重传规则（同一 `station + minute_utc + fragment_no`）：

- 修订号 **更高** → 接受并覆盖旧版（`status: "superseded"`）
- 修订号 **更低** → 拒绝（409），旧数据不动
- 同修订号、**同内容**（SHA-256 一致）→ 幂等重传，沿用原结果（`status: "unchanged"`），不产生新行
- 同修订号、**异文** → 拒绝（409），旧数据不动

拼装（assembly）：

- 每分钟固定 60 个 1 Hz 采样位
- 缺位在 `gaps` 中逐个列出；覆盖数见 `coverage`
- 重叠位数值不同即为冲突，`conflicts` 给出 **位置、涉及片段号、各自数值**；
  重叠但数值相同不算冲突
- `complete = 60 位全覆盖且无冲突`

发布（publication）：

- 只有完整且无冲突的分钟可发布；缺位或冲突时返回 409 并在 `detail` 中说明，
  **失败不留下任何半份记录**（单事务回滚）
- 发布记录冻结当时的 60 点波形 `wave`、每个片段的修订号与内容（`items`），
  以及片段集合指纹 `fragment_set_hash`
- 同一片段集合重复发布是幂等的，返回同一条记录
- 之后补传/更正只改变当前拼装；该分钟片段集合一旦变化，旧记录自动标 `stale=true`，
  仍可按 ID 读取，波形与修订保持冻结
- 每份记录只来自同一个已提交片段集合（事务快照保证）

撤回（withdrawal）与当前供数（supply）：

- 设备故障等情况下，质控员可按 **发布号 + 原因** 撤回一条 **仍有效**（非 stale）的发布，
  立即停止供数
- 同号同因重试是幂等的，沿用原撤回（同一 `withdrawn_at`，返回 200）；
  同号异因拒绝（409），原原因不动；撤回不存在的发布号返回 404，原因为空返回 422
- 撤回 **不删除数据**：被撤回记录的冻结波形 `wave`、片段来源 `items`、撤回原因
  `withdrawal_reason` 与时间 `withdrawn_at` 仍可通过发布接口读取
- 当前供数接口只返回 **既未过期（stale=false）也未撤回（withdrawn=false）** 的发布；
  被撤回记录不会出现
- 同一片段集合 **不能靠再次发布恢复供数**：对已撤回的同一 `fragment_set_hash`
  再次发布返回 409。只有补传缺失片段或上传更高修订 **真正改变片段集合** 后，
  才能产生新发布并恢复供数
- 旧 SQLite 卷（无撤回字段的早期版本）启动时自动补列，历史发布默认 `withdrawn=false`，
  即默认继续供数

## 三、HTTP 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | 健康检查 |
| POST | `/fragments` | 上传片段（JSON body） |
| GET | `/fragments?station=&minute_utc=` | 查看某分钟当前片段 |
| GET | `/assembly?station=&minute_utc=` | 查看拼装状态：覆盖率、缺口、冲突 |
| POST | `/publications?station=&minute_utc=` | 发布完整无冲突分钟 |
| GET | `/publications?station=&minute_utc=` | 发布记录列表（含已撤回、已过期，可按台站/分钟过滤） |
| GET | `/publications/latest?station=&minute_utc=` | 该分钟最新一条记录 |
| GET | `/publications/{id}` | 按 ID 读取（过期/撤回记录同样可读） |
| POST | `/publications/{id}/withdrawal` | 撤回发布：body `{"reason": "..."}`，同号同因幂等、异因 409 |
| GET | `/supply?station=&minute_utc=` | 当前供数：仅返回未过期且未撤回的有效发布 |

### 撤回与供数示例

```bash
# 发现已发布分钟受设备故障污染，立即撤回
curl -s -X POST localhost:8000/publications/1/withdrawal \
  -H 'content-type: application/json' \
  -d '{"reason":"station sensor fault, correction pending"}'
# -> 201，记录带 withdrawn=true、withdrawal_reason、withdrawn_at

# 同号同因重试：幂等，沿用原撤回（200，withdrawn_at 不变）
curl -s -X POST localhost:8000/publications/1/withdrawal \
  -H 'content-type: application/json' \
  -d '{"reason":"station sensor fault, correction pending"}'
# 同号异因：409，原撤回原因保留

# 当前供数不再包含它（但 /publications/1 仍可读到波形、片段来源与原因）
curl -s "localhost:8000/supply?station=STA1&minute_utc=$MIN"

# 同一集合再次发布被拒（409）；补传/更高修订真正改变集合后才能重新发布
curl -s -X POST "localhost:8000/publications?station=STA1&minute_utc=$MIN"
```

### 典型流程

```bash
MIN=2026-11-17T05:06:00Z
# 乱序上传两个片段，恰好铺满 60 位
curl -s -X POST localhost:8000/fragments -H 'content-type: application/json' -d '{
  "station":"STA1","minute_utc":"'"$MIN"'","fragment_no":2,"revision":1,
  "start_sample":30,"samples":'"$(/workspace/.venv/bin/python -c 'print(list(range(30,60)))')"'}'
curl -s -X POST localhost:8000/fragments -H 'content-type: application/json' -d '{
  "station":"STA1","minute_utc":"'"$MIN"'","fragment_no":1,"revision":1,
  "start_sample":0,"samples":'"$(/workspace/.venv/bin/python -c 'print(list(range(0,30)))')"'}'

# 检查拼装
curl -s "localhost:8000/assembly?station=STA1&minute_utc=$MIN"
# 发布
curl -s -X POST "localhost:8000/publications?station=STA1&minute_utc=$MIN"
```

## 四、测试与自测

```bash
pip install pytest httpx
python -m pytest tests/ -v          # 37 个用例：校验/修订/拼装/发布/过期/撤回/供数/迁移/重启/并发
python scripts/smoke.py             # 对运行中的服务做 48 项端到端检查
```

并发用例会启动真实 uvicorn 进程，用多个线程同时上传并尝试发布/撤回，
断言中间态操作只可能干净失败（409）或幂等返回，完成态恰好产生一条完整记录，
无半份数据、无重复撤回。

## 五、目录结构

```
app/
  config.py      # 环境变量配置（WAVEFORM_DB_PATH）
  database.py    # SQLite 连接与建表
  models.py      # Pydantic 请求/响应模型
  service.py     # 事务、拼装算法、发布、过期、撤回与供数逻辑
  main.py        # FastAPI 路由
tests/test_api.py
scripts/smoke.py
Dockerfile
docker-compose.yml
requirements.txt
```
