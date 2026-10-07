"""端到端测试：地图录入校验、调整预演/提交、版本不可变、各类明确报错。"""

import pytest
from fastapi.testclient import TestClient

from app.main import create_app

# 示例地图：
#   A — B — C
#   |       |
#   D — E — F
# D1 = {A, B, D} 人口 270；D2 = {C, E, F} 人口 250；限额均 [100, 400]
MAP_BODY = {
    "blocks": [
        {"id": "A", "population": 100},
        {"id": "B", "population": 80},
        {"id": "C", "population": 120},
        {"id": "D", "population": 90},
        {"id": "E", "population": 60},
        {"id": "F", "population": 70},
    ],
    "edges": [["A", "B"], ["B", "C"], ["A", "D"], ["D", "E"], ["C", "F"], ["E", "F"]],
    "districts": [
        {"id": "D1", "min_population": 100, "max_population": 400},
        {"id": "D2", "min_population": 100, "max_population": 400},
    ],
    "assignment": {"A": "D1", "B": "D1", "C": "D2", "D": "D1", "E": "D2", "F": "D2"},
}


@pytest.fixture()
def client(tmp_path):
    app = create_app(str(tmp_path / "test.db"))
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def initialized(client):
    resp = client.post("/api/map", json=MAP_BODY)
    assert resp.status_code == 201, resp.json()
    return client


def err(resp, code):
    assert "error" in resp.json(), resp.json()
    assert resp.json()["error"]["code"] == code, resp.json()
    return resp.json()["error"]


# ---- 基础地图录入 ----

def test_init_map_success(initialized):
    resp = initialized.get("/api/map")
    assert resp.status_code == 200
    body = resp.json()
    assert body["latest_version"] == 1
    assert body["version_count"] == 1
    v1 = initialized.get("/api/versions/1").json()
    assert v1["districts"]["D1"]["population"] == 270
    assert v1["districts"]["D2"]["population"] == 250
    cross = {tuple(sorted(e["blocks"])) for e in v1["cross_district_edges"]}
    assert cross == {("B", "C"), ("D", "E")}


def test_init_map_twice_rejected(initialized):
    resp = initialized.post("/api/map", json=MAP_BODY)
    assert resp.status_code == 409
    err(resp, "MAP_ALREADY_INITIALIZED")


def test_duplicate_block(client):
    body = {**MAP_BODY, "blocks": MAP_BODY["blocks"] + [{"id": "A", "population": 1}]}
    assert err(client.post("/api/map", json=body), "DUPLICATE_BLOCK")


def test_negative_population(client):
    body = {**MAP_BODY, "blocks": [{"id": "A", "population": -5}] + MAP_BODY["blocks"][1:]}
    assert err(client.post("/api/map", json=body), "INVALID_POPULATION")


def test_self_loop(client):
    body = {**MAP_BODY, "edges": MAP_BODY["edges"] + [["A", "A"]]}
    assert err(client.post("/api/map", json=body), "SELF_LOOP")


def test_duplicate_edge_both_directions(client):
    for dup in (["A", "B"], ["B", "A"]):
        body = {**MAP_BODY, "edges": MAP_BODY["edges"] + [dup]}
        assert err(client.post("/api/map", json=body), "DUPLICATE_EDGE")


def test_edge_unknown_block(client):
    body = {**MAP_BODY, "edges": MAP_BODY["edges"] + [["A", "ZZ"]]}
    assert err(client.post("/api/map", json=body), "UNKNOWN_BLOCK")


def test_duplicate_district(client):
    body = {**MAP_BODY, "districts": MAP_BODY["districts"] + [
        {"id": "D1", "min_population": 0, "max_population": 10}]}
    assert err(client.post("/api/map", json=body), "DUPLICATE_DISTRICT")


@pytest.mark.parametrize("lo,hi", [(-1, 100), (50, -1), (200, 100)])
def test_invalid_limits(client, lo, hi):
    body = {**MAP_BODY, "districts": [
        {"id": "D1", "min_population": lo, "max_population": hi},
        {"id": "D2", "min_population": 100, "max_population": 400},
    ]}
    assert err(client.post("/api/map", json=body), "INVALID_LIMITS")


def test_assignment_unknown_block_and_district(client):
    body = {**MAP_BODY, "assignment": {**MAP_BODY["assignment"], "ZZ": "D1"}}
    assert err(client.post("/api/map", json=body), "UNKNOWN_BLOCK")
    body = {**MAP_BODY, "assignment": {**MAP_BODY["assignment"], "A": "D9"}}
    assert err(client.post("/api/map", json=body), "UNKNOWN_DISTRICT")


def test_unassigned_block(client):
    assignment = {k: v for k, v in MAP_BODY["assignment"].items() if k != "F"}
    body = {**MAP_BODY, "assignment": assignment}
    e = err(client.post("/api/map", json=body), "UNASSIGNED_BLOCKS")
    assert e["details"]["blocks"] == ["F"]


def test_initial_plan_must_be_valid(client):
    # D2 人口 250 超过上限 200 -> 初始方案不通过
    body = {**MAP_BODY, "districts": [
        {"id": "D1", "min_population": 100, "max_population": 400},
        {"id": "D2", "min_population": 100, "max_population": 200},
    ]}
    resp = client.post("/api/map", json=body)
    e = err(resp, "INITIAL_PLAN_INVALID")
    assert any(i["type"] == "population_above_max" and i["excess"] == 50
               for i in e["details"]["issues"])


# ---- 调整：预演与提交 ----

def test_preview_and_commit_success(initialized):
    move = {"base_version": 1, "transfers": [{"block": "B", "to_district": "D2"}]}
    preview = initialized.post("/api/adjustments/preview", json=move)
    assert preview.status_code == 200
    assert preview.json()["valid"] is True
    assert preview.json()["resulting"]["districts"]["D1"]["population"] == 190
    assert preview.json()["resulting"]["districts"]["D2"]["population"] == 330

    commit = initialized.post("/api/adjustments", json=move)
    assert commit.status_code == 201
    assert commit.json()["version"] == 2
    # 预演结果与同版本提交一致
    assert commit.json()["resulting"] == preview.json()["resulting"]

    v2 = initialized.get("/api/versions/2").json()
    assert v2["assignment"]["B"] == "D2"
    assert v2["parent_id"] == 1
    cross = {tuple(sorted(e["blocks"])) for e in v2["cross_district_edges"]}
    assert cross == {("A", "B"), ("D", "E")}


def test_multi_block_transfer_atomic(initialized):
    # A、B 同时转 D2：D1 只剩 D（人口 90 < 100）且 D2 人口 430 > 400，两个问题同批报出
    move = {"base_version": 1, "transfers": [
        {"block": "A", "to_district": "D2"}, {"block": "B", "to_district": "D2"}]}
    resp = initialized.post("/api/adjustments", json=move)
    assert resp.status_code == 422
    e = err(resp, "ADJUSTMENT_INVALID")
    issues = e["details"]["issues"]
    assert any(i["type"] == "population_below_min" and i["district"] == "D1"
               and i["deficit"] == 10 for i in issues)
    assert any(i["type"] == "population_above_max" and i["district"] == "D2"
               and i["excess"] == 30 for i in issues)
    # 整批不生效：没有产生新版本
    assert initialized.get("/api/map").json()["version_count"] == 1


def test_disconnected_district_reported(initialized):
    # A 转 D2 后：D1 = {B, D} 断开（B-D 不相邻）；A 在 D2 中同样孤立，两个受影响片区都列出
    move = {"base_version": 1, "transfers": [{"block": "A", "to_district": "D2"}]}
    resp = initialized.post("/api/adjustments", json=move)
    assert resp.status_code == 422
    issues = resp.json()["error"]["details"]["issues"]
    disc = {i["district"]: i["disconnected_blocks"] for i in issues if i["type"] == "disconnected"}
    assert disc == {"D1": ["D"], "D2": ["C", "E", "F"]}


def test_empty_district_reported(initialized):
    move = {"base_version": 1, "transfers": [
        {"block": "A", "to_district": "D2"},
        {"block": "B", "to_district": "D2"},
        {"block": "D", "to_district": "D2"},
    ]}
    resp = initialized.post("/api/adjustments", json=move)
    assert resp.status_code == 422
    issues = resp.json()["error"]["details"]["issues"]
    assert any(i["type"] == "empty" and i["district"] == "D1" for i in issues)


def test_preview_is_readonly_and_matches_commit(initialized):
    move = {"base_version": 1, "transfers": [{"block": "A", "to_district": "D2"}]}
    preview = initialized.post("/api/adjustments/preview", json=move)
    assert preview.status_code == 200
    assert preview.json()["valid"] is False
    # 预演不产生新版本
    assert initialized.get("/api/map").json()["version_count"] == 1
    # 同版本提交报同样的问题
    commit = initialized.post("/api/adjustments", json=move)
    assert commit.status_code == 422
    assert commit.json()["error"]["details"]["issues"] == preview.json()["issues"]


def test_stale_base_version_conflict(initialized):
    move1 = {"base_version": 1, "transfers": [{"block": "B", "to_district": "D2"}]}
    assert initialized.post("/api/adjustments", json=move1).status_code == 201
    # 再基于旧版本 1 提交 -> 409
    move2 = {"base_version": 1, "transfers": [{"block": "F", "to_district": "D1"}]}
    resp = initialized.post("/api/adjustments", json=move2)
    assert resp.status_code == 409
    e = err(resp, "VERSION_CONFLICT")
    assert e["details"]["latest_version"] == 2
    # 但旧版本可以只读预演
    preview = initialized.post("/api/adjustments/preview", json=move2)
    assert preview.status_code == 200


def test_old_version_immutable(initialized):
    before = initialized.get("/api/versions/1").json()
    move = {"base_version": 1, "transfers": [{"block": "B", "to_district": "D2"}]}
    assert initialized.post("/api/adjustments", json=move).status_code == 201
    after = initialized.get("/api/versions/1").json()
    assert before == after
    assert after["assignment"]["B"] == "D1"


def test_transfer_validation_errors(initialized):
    base = {"base_version": 1}
    resp = initialized.post("/api/adjustments", json={**base, "transfers": [
        {"block": "A", "to_district": "D2"}, {"block": "A", "to_district": "D1"}]})
    assert resp.status_code == 400
    err(resp, "DUPLICATE_TRANSFER")

    resp = initialized.post("/api/adjustments", json={**base, "transfers": [
        {"block": "ZZ", "to_district": "D2"}]})
    assert resp.status_code == 400
    err(resp, "UNKNOWN_BLOCK")

    resp = initialized.post("/api/adjustments", json={**base, "transfers": [
        {"block": "A", "to_district": "D9"}]})
    assert resp.status_code == 400
    err(resp, "UNKNOWN_DISTRICT")

    resp = initialized.post("/api/adjustments", json={**base, "transfers": []})
    assert resp.status_code == 422
    err(resp, "VALIDATION_ERROR")


def test_version_not_found(initialized):
    assert err(initialized.get("/api/versions/99"), "VERSION_NOT_FOUND")
    move = {"base_version": 99, "transfers": [{"block": "A", "to_district": "D2"}]}
    assert err(initialized.post("/api/adjustments/preview", json=move), "VERSION_NOT_FOUND")


def test_map_not_initialized(client):
    assert err(client.get("/api/map"), "MAP_NOT_INITIALIZED")
    move = {"base_version": 1, "transfers": [{"block": "A", "to_district": "D2"}]}
    assert err(client.post("/api/adjustments", json=move), "MAP_NOT_INITIALIZED")


def test_versions_listed(initialized):
    initialized.post("/api/adjustments", json={
        "base_version": 1, "transfers": [{"block": "B", "to_district": "D2"}]})
    initialized.post("/api/adjustments", json={
        "base_version": 2, "transfers": [{"block": "E", "to_district": "D1"}]})
    versions = initialized.get("/api/versions").json()["versions"]
    assert [v["id"] for v in versions] == [1, 2, 3]
    assert versions[2]["parent_id"] == 2
    assert versions[2]["transfers"] == [{"block": "E", "to_district": "D1"}]
    v3 = initialized.get("/api/versions/3").json()
    assert v3["districts"]["D1"]["population"] == 250
    assert v3["districts"]["D2"]["population"] == 270
