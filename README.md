# 城市普查调查片区调整 API

供城市普查人员调整调查片区的 HTTP API（Python / FastAPI + SQLite）。

- **基础地图一次录入**：街区编号、非负人口、双向相邻边、初始片区归属、各片区人口上下限；每个街区恰好归属一个片区，初始方案须通过整体校验。
- **批量调整**：一次调整可同时转移多个街区，需提交基准版本号；按转移完成后的整体归属校验**每个片区非空、片区内相邻图连通、人口在限额内**；任一失败则列出受影响片区及断开街区 / 人口差额，**整批不生效**。
- **只读预演**：同一调整可以 `dry_run` 预演，校验口径与提交完全一致，不产生新版本。
- **不可变版本**：成功提交产生新的方案版本；可查询任一历史版本的归属、各片区人口、跨片区相邻边，旧版本永不漂移。
- **明确报错**：并发旧版（409）、自环 / 重复相邻边、未知 / 重复街区、无效限额等均返回结构化错误。

## 一键启动（Docker Compose）

```bash
docker compose up --build
```

启动后：

- 服务地址：**http://localhost:8000**
- 交互式接口文档（Swagger UI）：**http://localhost:8000/docs**
- 健康检查：`GET http://localhost:8000/health`

数据通过命名卷 `census-data` 持久化在本地（容器内路径 `/data/census.db`），
容器删除重建后数据不丢。停止服务：`docker compose down`（加 `-v` 才会清除数据）。

也可以只用 Docker：

```bash
docker build -t census-district-api .
docker run -d -p 8000:8000 -v census-data:/data --name census-district-api census-district-api
```

## 本地开发启动

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

运行测试：

```bash
pip install -r requirements-dev.txt
pytest tests/ -q
```

## 配置

| 环境变量  | 默认值                          | 说明                       |
| --------- | ------------------------------- | -------------------------- |
| `DB_PATH` | 容器内 `/data/census.db`；本地 `./data/census.db` | SQLite 数据库文件位置 |

端口通过 uvicorn 启动参数 / Compose `ports` 映射配置，默认 **8000**。
首次启动自动建表初始化存储，无需手工迁移。

## API 一览

| 方法 | 路径 | 说明 |
| ---- | ---- | ---- |
| `POST` | `/maps` | 一次性录入基础地图与初始方案（成功即版本 1） |
| `GET`  | `/maps` | 列出全部地图 |
| `GET`  | `/maps/{map_id}` | 地图静态信息与当前版本号 |
| `GET`  | `/maps/{map_id}/versions` | 版本历史列表 |
| `GET`  | `/maps/{map_id}/versions/{version}` | 查询任一版本：归属、各片区人口、跨片区相邻边 |
| `POST` | `/maps/{map_id}/adjustments` | 提交（或 `dry_run` 预演）一次批量调整 |
| `GET`  | `/health` | 健康检查 |

错误响应统一为 `{"detail": {"error": <代码>, "message": <说明>, ...}}`。

主要状态码：`201` 建图 / 提交成功，`200` 查询 / 预演，
`404` 地图或版本不存在，`409` 地图已存在 / 基准版本过期（`stale_base_version`），
`422` 输入或方案校验失败（`duplicate_blocks`、`self_loop_edges`、`duplicate_edges`、
`unknown_blocks`、`unassigned_blocks`、`unknown_districts`、`duplicate_move_blocks`、
`plan_validation_failed` 等）。

## 调用示例

### 1. 录入基础地图（初始方案即版本 1）

```bash
curl -s -X POST http://localhost:8000/maps \
  -H 'Content-Type: application/json' -d '{
    "map_id": "city",
    "blocks": [
      {"id": "A", "population": 100}, {"id": "B", "population": 200},
      {"id": "C", "population": 150}, {"id": "D", "population": 120},
      {"id": "E", "population": 80},  {"id": "F", "population": 90}
    ],
    "edges": [["A","B"],["B","C"],["A","C"],["D","E"],["E","F"],["D","F"],["C","D"]],
    "districts": [
      {"id": "D1", "min_population": 200, "max_population": 600},
      {"id": "D2", "min_population": 200, "max_population": 600}
    ],
    "assignment": {"A":"D1","B":"D1","C":"D1","D":"D2","E":"D2","F":"D2"}
  }'
```

响应 `201`：版本 1 的归属、各片区人口（D1=450，D2=290）、跨片区相邻边（仅 C-D）。

### 2. 只读预演：把街区 C 从 D1 划入 D2

```bash
curl -s -X POST http://localhost:8000/maps/city/adjustments \
  -H 'Content-Type: application/json' -d '{
    "base_version": 1,
    "dry_run": true,
    "moves": [{"block_id": "C", "to_district": "D2"}]
  }'
```

响应 `200`：`{"dry_run": true, "committed": false, "would_be_version": 2, ...}`，
归属 / 人口 / 跨片区边与同版本提交完全一致，但不落库。

### 3. 正式提交同一调整（产生不可变版本 2）

```bash
curl -s -X POST http://localhost:8000/maps/city/adjustments \
  -H 'Content-Type: application/json' -d '{
    "base_version": 1,
    "moves": [{"block_id": "C", "to_district": "D2"}]
  }'
```

响应 `201`：`committed: true`，`version: 2`，人口变为 D1=300、D2=440，
跨片区边变为 A-C、B-C。

### 4. 校验失败：列出受影响片区与明细，整批不生效

```bash
curl -s -X POST http://localhost:8000/maps/city/adjustments \
  -H 'Content-Type: application/json' -d '{
    "base_version": 2,
    "moves": [{"block_id": "A", "to_district": "D2"},
              {"block_id": "B", "to_district": "D2"}]
  }'
```

响应 `422`（当前版本号不变）：

```json
{
  "detail": {
    "error": "plan_validation_failed",
    "message": "调整后方案未通过校验，整批转移未生效",
    "issues": [
      {"district": "D1", "type": "empty_district",
       "message": "片区 D1 不包含任何街区（片区必须非空）"},
      {"district": "D2", "type": "population_out_of_range",
       "population": 740, "min_population": 200, "max_population": 600,
       "difference": 140,
       "message": "片区 D2 人口 740 超出限额 [200, 600]，差额 +140"}
    ]
  }
}
```

断开示例（`type: "disconnected"`）会给出 `components`（各连通分量）与
`disconnected_blocks`（断开街区列表）。

### 5. 并发 / 旧版本提交

版本 2 产生后再以 `base_version: 1` 提交（或预演）：

```json
HTTP 409
{"detail": {"error": "stale_base_version",
            "message": "基准版本 1 已过期，当前最新版本为 2；请基于最新版本重新提交",
            "base_version": 1, "current_version": 2}}
```

并发以同一基准版本提交时，数据库事务保证只有一笔成功，其余收到 409。

### 6. 查询历史版本（旧版不漂移）

```bash
curl -s http://localhost:8000/maps/city/versions/1
```

始终返回版本 1 的归属（C 仍属 D1）、人口（D1=450，D2=290）与当时的跨片区边（C-D）。

## 项目结构

```
app/
  main.py        # FastAPI 路由与请求校验
  models.py      # Pydantic 请求模型
  validation.py  # 方案校验（非空 / 连通 / 人口限额）与派生数据，提交与预演共用
  store.py       # SQLite 持久化：版本快照只插不改，事务保证并发一致
tests/test_api.py
Dockerfile
docker-compose.yml
requirements.txt / requirements-dev.txt
```
