# 城市普查片区调整 API

供城市普查人员调整调查片区的 HTTP 服务。基础地图（街区、人口、相邻边、初始片区归属、各片区人口上下限）一次录入后，普查人员可基于任一最新版本提交多街区整批转移；服务按转移完成后的整体归属校验每个片区**非空、内部连通、人口在限额内**，任一失败则整批不生效并列出受影响片区及断开街区或人口差额。支持只读预演、不可变版本快照与历史版本查询。

## 快速开始

### Docker Compose（一键启动）

```bash
docker compose up --build
```

服务地址：**http://localhost:8000**（交互式接口文档：http://localhost:8000/docs）

数据通过命名卷 `census-data` 持久化在容器 `/data/app.db`（SQLite），重启不丢失。

### 本地运行

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

### 运行测试

```bash
pip install -r requirements-dev.txt
pytest tests/ -q
```

## 配置

| 环境变量       | 默认值          | 说明                                   |
| -------------- | --------------- | -------------------------------------- |
| `APP_DB_PATH`  | `data/app.db`   | SQLite 数据文件路径（容器内 `/data/app.db`） |

端口通过 `docker-compose.yml` 的 `ports` 或 uvicorn `--port` 配置，默认 **8000**。

## 数据模型与规则

- 街区：编号唯一、人口非负，只能归属一个片区；
- 相邻边：双向、无自环、无重复（`(A,B)` 与 `(B,A)` 视为同一条）；
- 片区：编号唯一，限额须满足 `0 <= min_population <= max_population`；
- 初始方案录入时即须通过「非空 + 连通 + 人口限额」校验；
- 一次调整可包含多个街区转移，基于 `base_version` 提交：
  - 预演 `POST /api/adjustments/preview`：只读，可基于任意历史版本，结果与同版本提交一致；
  - 提交 `POST /api/adjustments`：`base_version` 必须是最新版本，否则 `409 VERSION_CONFLICT`（并发旧版）；
  - 校验任一失败返回 `422 ADJUSTMENT_INVALID`，`details.issues` 列出每个受影响片区的断开街区或人口差额，整批不生效；
  - 成功则生成新的不可变版本，历史版本随时可查、绝不漂移。

## API 一览

| 方法 | 路径                        | 说明                       |
| ---- | --------------------------- | -------------------------- |
| POST | `/api/map`                  | 一次录入基础地图与初始方案 |
| GET  | `/api/map`                  | 查看地图与最新版本号       |
| POST | `/api/adjustments/preview`  | 只读预演一次调整           |
| POST | `/api/adjustments`          | 提交调整，生成新版本       |
| GET  | `/api/versions`             | 版本列表                   |
| GET  | `/api/versions/{id}`        | 任一版本的归属、人口、跨片区相邻边 |
| GET  | `/api/health`               | 健康检查                   |

## 调用示例

示例地图：`A—B—C`、`A—D`、`D—E`、`C—F`、`E—F`；D1={A,B,D}，D2={C,E,F}，限额均 [100, 400]。

### 1. 录入基础地图

```bash
curl -s -X POST http://localhost:8000/api/map -H 'Content-Type: application/json' -d '{
  "blocks": [
    {"id": "A", "population": 100}, {"id": "B", "population": 80},
    {"id": "C", "population": 120}, {"id": "D", "population": 90},
    {"id": "E", "population": 60},  {"id": "F", "population": 70}
  ],
  "edges": [["A","B"],["B","C"],["A","D"],["D","E"],["C","F"],["E","F"]],
  "districts": [
    {"id": "D1", "min_population": 100, "max_population": 400},
    {"id": "D2", "min_population": 100, "max_population": 400}
  ],
  "assignment": {"A":"D1","B":"D1","C":"D2","D":"D1","E":"D2","F":"D2"}
}'
```

响应 `201`：`{"version": 1, "blocks": 6, "edges": 6, "districts": ["D1","D2"], "resulting": {...}}`

### 2. 预演一次调整（B 转入 D2）

```bash
curl -s -X POST http://localhost:8000/api/adjustments/preview -H 'Content-Type: application/json' -d '{
  "base_version": 1,
  "transfers": [{"block": "B", "to_district": "D2"}]
}'
```

响应 `200`：`{"valid": true, "base_version": 1, "issues": [], "resulting": {...}}`，不产生新版本。

### 3. 提交同一调整

```bash
curl -s -X POST http://localhost:8000/api/adjustments -H 'Content-Type: application/json' -d '{
  "base_version": 1,
  "transfers": [{"block": "B", "to_district": "D2"}]
}'
```

响应 `201`：`{"valid": true, "version": 2, "base_version": 1, "resulting": {...}}`，`resulting` 与预演一致。

### 4. 查询任一版本（归属、人口、跨片区相邻边）

```bash
curl -s http://localhost:8000/api/versions/2
```

```json
{
  "id": 2, "parent_id": 1,
  "transfers": [{"block": "B", "to_district": "D2"}],
  "assignment": {"A":"D1","B":"D2","C":"D2","D":"D1","E":"D2","F":"D2"},
  "districts": {
    "D1": {"blocks": ["A","D"], "population": 190, "min_population": 100, "max_population": 400},
    "D2": {"blocks": ["B","C","E","F"], "population": 330, "min_population": 100, "max_population": 400}
  },
  "cross_district_edges": [
    {"blocks": ["A","B"], "districts": ["D1","D2"]},
    {"blocks": ["D","E"], "districts": ["D1","D2"]}
  ]
}
```

历史版本（如 `/api/versions/1`）返回原始快照，不随后续提交改变。

### 5. 失败示例：整批不生效并列出原因

```bash
curl -s -X POST http://localhost:8000/api/adjustments -H 'Content-Type: application/json' -d '{
  "base_version": 2,
  "transfers": [{"block": "A", "to_district": "D2"}, {"block": "D", "to_district": "D2"}]
}'
```

响应 `422`（D1 被抽空、D2 人口超限，且不产生新版本）：

```json
{
  "error": {
    "code": "ADJUSTMENT_INVALID",
    "message": "调整后方案未通过校验，整批未生效",
    "details": {"issues": [
      {"district": "D1", "type": "empty", "message": "片区 D1 调整后为空"},
      {"district": "D2", "type": "population_above_max", "population": 520,
       "max_population": 400, "excess": 120, "message": "片区 D2 人口 520 超过上限 400，差额 120"}
    ]}
  }
}
```

断开示例（A 单独转入 D2，D1 中 B、D 失去连接）：

```json
{"district": "D1", "type": "disconnected", "disconnected_blocks": ["D"],
 "message": "片区 D1 内街区不连通，断开街区: D"}
```

### 6. 并发旧版提交

基于已过期版本提交时响应 `409`：

```json
{"error": {"code": "VERSION_CONFLICT",
  "message": "基准版本 1 不是最新版本 2，可能存在并发提交，请基于最新版本重新调整",
  "details": {"base_version": 1, "latest_version": 2}}}
```

## 错误码

统一错误格式：`{"error": {"code", "message", "details"}}`

| HTTP | code                    | 含义                                       |
| ---- | ----------------------- | ------------------------------------------ |
| 400  | `EMPTY_BLOCKS` / `EMPTY_DISTRICTS` | 街区/片区列表为空                |
| 400  | `DUPLICATE_BLOCK`       | 街区重复录入                               |
| 400  | `INVALID_POPULATION`    | 人口为负数                                 |
| 400  | `SELF_LOOP`             | 相邻边是自环                               |
| 400  | `DUPLICATE_EDGE`        | 相邻边重复（含反向重复）                   |
| 400  | `DUPLICATE_DISTRICT`    | 片区重复录入                               |
| 400  | `INVALID_LIMITS`        | 限额无效（负数或上限小于下限）             |
| 400  | `UNKNOWN_BLOCK`         | 引用了未知街区                             |
| 400  | `UNKNOWN_DISTRICT`      | 引用了未知片区                             |
| 400  | `UNASSIGNED_BLOCKS`     | 存在未分配片区的街区                       |
| 400  | `INITIAL_PLAN_INVALID`  | 初始方案未通过校验（details.issues 列明）  |
| 400  | `DUPLICATE_TRANSFER`    | 同一街区在一批调整中被重复转移             |
| 404  | `VERSION_NOT_FOUND`     | 版本不存在                                 |
| 409  | `MAP_ALREADY_INITIALIZED` | 基础地图重复录入                         |
| 409  | `MAP_NOT_INITIALIZED`   | 基础地图尚未录入                           |
| 409  | `VERSION_CONFLICT`      | 基准版本非最新（并发旧版）                 |
| 422  | `ADJUSTMENT_INVALID`    | 调整未通过校验，整批未生效                 |
| 422  | `VALIDATION_ERROR`      | 请求体格式错误                             |

## 项目结构

```
app/
  main.py    # FastAPI 路由与错误处理（create_app 工厂）
  core.py    # 非空/连通/人口限额校验、跨片区边计算（纯函数）
  models.py  # 请求体模型
  store.py   # SQLite 持久化：地图 + 不可变版本快照
tests/test_api.py
Dockerfile / docker-compose.yml
```
