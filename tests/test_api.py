"""端到端 API 测试：每个用例使用独立临时 SQLite 库。"""
import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    return TestClient(app)


def base_payload(**overrides):
    payload = {
        "map_id": "city",
        "blocks": [
            {"id": "A", "population": 100},
            {"id": "B", "population": 200},
            {"id": "C", "population": 150},
            {"id": "D", "population": 120},
            {"id": "E", "population": 80},
            {"id": "F", "population": 90},
        ],
        "edges": [["A", "B"], ["B", "C"], ["A", "C"],
                  ["D", "E"], ["E", "F"], ["D", "F"],
                  ["C", "D"]],
        "districts": [
            {"id": "D1", "min_population": 200, "max_population": 600},
            {"id": "D2", "min_population": 200, "max_population": 600},
        ],
        "assignment": {"A": "D1", "B": "D1", "C": "D1",
                       "D": "D2", "E": "D2", "F": "D2"},
    }
    payload.update(overrides)
    return payload


def create_map(client, **overrides):
    return client.post("/maps", json=base_payload(**overrides))


# ------------------------------------------------------------ 建图

def test_create_map_ok(client):
    r = create_map(client)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["version"] == 1
    assert body["district_populations"] == {"D1": 450, "D2": 290}
    # 初始跨片区边只有 C-D
    assert body["cross_district_edges"] == [
        {"block_a": "C", "block_b": "D", "district_a": "D1", "district_b": "D2"}
    ]


def test_create_map_twice_conflict(client):
    assert create_map(client).status_code == 201
    r = create_map(client)
    assert r.status_code == 409
    assert r.json()["detail"]["error"] == "map_exists"


def test_duplicate_blocks(client):
    p = base_payload()
    p["blocks"].append({"id": "A", "population": 5})
    r = client.post("/maps", json=p)
    assert r.status_code == 422
    assert r.json()["detail"]["error"] == "duplicate_blocks"
    assert r.json()["detail"]["blocks"] == ["A"]


def test_duplicate_districts(client):
    p = base_payload()
    p["districts"].append({"id": "D1", "min_population": 0, "max_population": 10})
    r = client.post("/maps", json=p)
    assert r.status_code == 422
    assert r.json()["detail"]["error"] == "duplicate_districts"


def test_self_loop_edge(client):
    r = create_map(client, edges=[["A", "A"]])
    assert r.status_code == 422
    assert r.json()["detail"]["error"] == "self_loop_edges"
    assert r.json()["detail"]["blocks"] == ["A"]


def test_duplicate_edge_undirected(client):
    r = create_map(client, edges=[["A", "B"], ["B", "A"], ["B", "C"]])
    assert r.status_code == 422
    assert r.json()["detail"]["error"] == "duplicate_edges"
    assert r.json()["detail"]["edges"] == [["A", "B"]]


def test_unknown_block_in_edge(client):
    r = create_map(client, edges=[["A", "ZZ"]])
    assert r.status_code == 422
    assert r.json()["detail"]["error"] == "unknown_blocks"
    assert r.json()["detail"]["blocks"] == ["ZZ"]


def test_unknown_block_in_assignment(client):
    p = base_payload()
    p["assignment"]["ZZ"] = "D1"
    r = client.post("/maps", json=p)
    assert r.status_code == 422
    assert r.json()["detail"]["error"] == "unknown_blocks"


def test_unassigned_block(client):
    p = base_payload()
    del p["assignment"]["F"]
    r = client.post("/maps", json=p)
    assert r.status_code == 422
    assert r.json()["detail"]["error"] == "unassigned_blocks"
    assert r.json()["detail"]["blocks"] == ["F"]


def test_unknown_district_in_assignment(client):
    p = base_payload()
    p["assignment"]["A"] = "DX"
    r = client.post("/maps", json=p)
    assert r.status_code == 422
    assert r.json()["detail"]["error"] == "unknown_districts"


def test_negative_population(client):
    p = base_payload()
    p["blocks"][0]["population"] = -1
    r = client.post("/maps", json=p)
    assert r.status_code == 422  # pydantic 校验


def test_invalid_limits(client):
    p = base_payload()
    p["districts"][0] = {"id": "D1", "min_population": 500, "max_population": 100}
    r = client.post("/maps", json=p)
    assert r.status_code == 422  # max < min


def test_negative_limit(client):
    p = base_payload()
    p["districts"][0] = {"id": "D1", "min_population": -5, "max_population": 100}
    r = client.post("/maps", json=p)
    assert r.status_code == 422


def test_initial_plan_must_pass_validation(client):
    # D1 人口 450，但上限设为 400 -> 初始方案不通过
    r = create_map(client, districts=[
        {"id": "D1", "min_population": 300, "max_population": 400},
        {"id": "D2", "min_population": 200, "max_population": 500},
    ])
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["error"] == "plan_validation_failed"
    issue = detail["issues"][0]
    assert issue["district"] == "D1"
    assert issue["type"] == "population_out_of_range"
    assert issue["population"] == 450
    assert issue["difference"] == 50


def test_initial_plan_disconnected(client):
    # 去掉 B-C 边后 D1 = {A,B,C} 中 C 孤立
    r = create_map(client, edges=[["A", "B"], ["D", "E"], ["E", "F"], ["C", "D"]])
    assert r.status_code == 422
    issues = r.json()["detail"]["issues"]
    disc = [i for i in issues if i["type"] == "disconnected"]
    assert disc and disc[0]["district"] == "D1"
    assert disc[0]["disconnected_blocks"] == ["C"]


# ------------------------------------------------------------ 调整：成功 / 预演

def test_preview_matches_commit_and_does_not_persist(client):
    create_map(client)
    adj = {"base_version": 1, "moves": [{"block_id": "C", "to_district": "D2"}]}

    preview = client.post("/maps/city/adjustments", json={**adj, "dry_run": True})
    assert preview.status_code == 200
    pv = preview.json()
    assert pv["dry_run"] is True and pv["committed"] is False
    assert pv["would_be_version"] == 2

    # 预演不产生新版本
    assert client.get("/maps/city").json()["current_version"] == 1

    commit = client.post("/maps/city/adjustments", json=adj)
    assert commit.status_code == 201
    cm = commit.json()
    assert cm["committed"] is True and cm["version"] == 2

    # 预演结果与同版本提交一致
    for key in ("assignment", "district_populations", "cross_district_edges"):
        assert pv[key] == cm[key]
    assert cm["district_populations"] == {"D1": 300, "D2": 440}
    assert {tuple(sorted((e["block_a"], e["block_b"])))
            for e in cm["cross_district_edges"]} == {("A", "C"), ("B", "C")}


def test_multi_block_atomic_move(client):
    create_map(client)
    # 同时把 B、C 移入 D2：D1={A} 人口 100 低于下限 200 -> 整批失败
    r = client.post("/maps/city/adjustments", json={
        "base_version": 1,
        "moves": [{"block_id": "B", "to_district": "D2"},
                  {"block_id": "C", "to_district": "D2"}],
    })
    assert r.status_code == 422
    assert client.get("/maps/city").json()["current_version"] == 1


# ------------------------------------------------------------ 调整：失败明细

def test_disconnected_failure_lists_blocks(client):
    # 链式图（无 A-C 边）：B 移走后 D1={A,C} 断开；同时 B 在 D2 中孤立
    create_map(client, edges=[["A", "B"], ["B", "C"], ["D", "E"], ["E", "F"], ["C", "D"]])
    r = client.post("/maps/city/adjustments", json={
        "base_version": 1, "moves": [{"block_id": "B", "to_district": "D2"}]})
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["error"] == "plan_validation_failed"
    by_district = {i["district"]: i for i in detail["issues"]}
    assert by_district["D1"]["type"] == "disconnected"
    assert by_district["D1"]["disconnected_blocks"] == ["C"]
    assert by_district["D2"]["type"] == "disconnected"
    assert by_district["D2"]["disconnected_blocks"] == ["B"]
    # 整批不生效
    assert client.get("/maps/city").json()["current_version"] == 1


def test_population_failure_lists_difference(client):
    create_map(client)
    # E、F 移入 D1：D2={D} 人口 120，低于下限 200，差额 -80
    r = client.post("/maps/city/adjustments", json={
        "base_version": 1,
        "moves": [{"block_id": "E", "to_district": "D1"},
                  {"block_id": "F", "to_district": "D1"}],
    })
    assert r.status_code == 422
    issues = r.json()["detail"]["issues"]
    pop = {i["district"]: i for i in issues if i["type"] == "population_out_of_range"}
    # D1={A,B,C,E,F} 人口 620，超出上限 600，差额 +20
    assert pop["D1"]["population"] == 620
    assert pop["D1"]["difference"] == 20
    # D2={D} 人口 120，低于下限 200，差额 -80
    assert pop["D2"]["population"] == 120
    assert pop["D2"]["difference"] == -80


def test_empty_district_failure(client):
    create_map(client)
    r = client.post("/maps/city/adjustments", json={
        "base_version": 1,
        "moves": [{"block_id": "D", "to_district": "D1"},
                  {"block_id": "E", "to_district": "D1"},
                  {"block_id": "F", "to_district": "D1"}],
    })
    assert r.status_code == 422
    types = {i["type"] for i in r.json()["detail"]["issues"]}
    assert "empty_district" in types


def test_move_validation_errors(client):
    create_map(client)
    # 未知街区
    r = client.post("/maps/city/adjustments", json={
        "base_version": 1, "moves": [{"block_id": "ZZ", "to_district": "D2"}]})
    assert r.status_code == 422
    assert r.json()["detail"]["error"] == "unknown_blocks"
    # 重复街区
    r = client.post("/maps/city/adjustments", json={
        "base_version": 1,
        "moves": [{"block_id": "A", "to_district": "D2"},
                  {"block_id": "A", "to_district": "D1"}]})
    assert r.status_code == 422
    assert r.json()["detail"]["error"] == "duplicate_move_blocks"
    # 未知目标片区
    r = client.post("/maps/city/adjustments", json={
        "base_version": 1, "moves": [{"block_id": "A", "to_district": "DX"}]})
    assert r.status_code == 422
    assert r.json()["detail"]["error"] == "unknown_districts"


# ------------------------------------------------------------ 版本与并发

def test_stale_base_version_conflict(client):
    create_map(client)
    adj = {"base_version": 1, "moves": [{"block_id": "C", "to_district": "D2"}]}
    assert client.post("/maps/city/adjustments", json=adj).status_code == 201

    # 旧版本再提交 -> 409（预演同样 409，口径一致）
    for dry in (False, True):
        r = client.post("/maps/city/adjustments", json={**adj, "dry_run": dry})
        assert r.status_code == 409
        detail = r.json()["detail"]
        assert detail["error"] == "stale_base_version"
        assert detail["current_version"] == 2


def test_concurrent_same_base_only_one_wins(client):
    create_map(client)
    adj = {"base_version": 1, "moves": [{"block_id": "C", "to_district": "D2"}]}
    r1 = client.post("/maps/city/adjustments", json=adj)
    r2 = client.post("/maps/city/adjustments", json=adj)
    assert sorted([r1.status_code, r2.status_code]) == [201, 409]
    assert client.get("/maps/city").json()["current_version"] == 2


def test_old_version_snapshot_does_not_drift(client):
    create_map(client)
    v1_before = client.get("/maps/city/versions/1").json()

    client.post("/maps/city/adjustments", json={
        "base_version": 1, "moves": [{"block_id": "C", "to_district": "D2"}]})
    client.post("/maps/city/adjustments", json={
        "base_version": 2, "moves": [{"block_id": "A", "to_district": "D2"}]})

    v1_after = client.get("/maps/city/versions/1").json()
    assert v1_after == v1_before  # 旧版本完全不变
    assert v1_after["assignment"]["C"] == "D1"
    assert v1_after["district_populations"] == {"D1": 450, "D2": 290}

    v3 = client.get("/maps/city/versions/3").json()
    assert v3["assignment"]["A"] == "D2"
    assert v3["assignment"]["F"] == "D2"
    assert v3["district_populations"] == {"D1": 200, "D2": 540}
    versions = client.get("/maps/city/versions").json()["versions"]
    assert [v["version"] for v in versions] == [1, 2, 3]


def test_unknown_map_and_version(client):
    assert client.get("/maps/nope").status_code == 404
    assert client.get("/maps/nope/versions/1").status_code == 404
    create_map(client)
    r = client.get("/maps/city/versions/99")
    assert r.status_code == 404
    assert r.json()["detail"]["error"] == "version_not_found"
    r = client.post("/maps/city/adjustments", json={
        "base_version": 99, "moves": [{"block_id": "C", "to_district": "D2"}]})
    assert r.status_code == 404
