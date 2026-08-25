"""Task 12：只读交付物 API + 反馈写入（§12.3 报告分层、§7 物理分区）。"""
import pytest
from fastapi.testclient import TestClient

from aiea.api import create_app


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    app = create_app(root=tmp_path_factory.mktemp("api-ws"))
    return TestClient(app)


def test_overview_returns_client_and_assumptions(client):
    r = client.get("/api/overview")
    assert r.status_code == 200
    body = r.json()
    assert body["client"]["short_name"] == "明辉家居建材"
    assert len(body["assumptions"]) >= 4
    assert body["delivery_form"] == "完整诊断"
    assert body["scorecard"]["evidence_traceability"] == 1.0


def test_scenarios_endpoint_nests_children_under_parents(client):
    body = client.get("/api/scenarios").json()
    assert len(body["parents"]) == 3
    total_children = sum(len(p["children"]) for p in body["parents"])
    assert total_children == 8
    for p in body["parents"]:
        for child in p["children"]:
            assert child["evidence_refs"]


def test_matrix_endpoint_keeps_do_not_quadrant(client):
    body = client.get("/api/matrix").json()
    assert any(i["quadrant"] == "不做" for i in body["items"])
    assert body["axes"]["benefit"] and body["axes"]["difficulty"]


def test_matrix_exposes_axis_thresholds_for_plotting(client):
    """前端要画真散点象限图，必须拿到与服务端一致的分界线，不能自己猜。"""
    body = client.get("/api/matrix").json()
    t = body["thresholds"]
    assert t["difficulty"] == 3.0
    assert t["benefit"] > 0
    # 阈值必须能把 items 正确分到象限，否则前端画的点会和文字结论矛盾
    for i in body["items"]:
        high_benefit = i["benefit"] >= t["benefit"]
        high_difficulty = i["difficulty"] >= t["difficulty"]
        expected = {
            (True, False): "先做", (True, True): "规划",
            (False, False): "顺手做", (False, True): "不做",
        }[(high_benefit, high_difficulty)]
        assert i["quadrant"] == expected, i


def test_roi_endpoint_never_exposes_amount_for_grade_c(client):
    body = client.get("/api/roi").json()
    for item in body["items"]:
        if item["evidence_grade"] == "C":
            assert item["amount"] is None
            assert item["tiers"] == []
    assert body["aggregate"]["delta"] > 0


def test_insights_endpoint_carries_no_money_fields(client):
    body = client.get("/api/insights").json()
    assert body["title"] == "基于经验的判断（无数据支撑）"
    blob = str(body)
    for token in ("amount", "monthly_saving", "¥", "payback"):
        assert token not in blob
    for item in body["items"]:
        assert item["verification_suggestion"]


def test_evidence_ledger_exposes_grade_reason_and_conflicts(client):
    body = client.get("/api/evidence").json()
    assert len(body["items"]) >= 6
    for e in body["items"]:
        assert e["grade_reason"]
    assert any(e["conflict"] for e in body["items"])


def test_roadmap_endpoint_has_exit_conditions(client):
    body = client.get("/api/roadmap").json()
    assert len(body["batches"]) == 3
    for b in body["batches"]:
        assert b["exit_condition"]


def test_observability_endpoint_is_natural_language(client):
    body = client.get("/api/observability").json()
    assert "昨天跑了" in body["daily_brief"]
    for jargon in ("insufficient_data", "no_grounding", "span"):
        assert jargon not in body["daily_brief"]
    assert body["security"]["injection_attempts_detected"] >= 1


def test_gaps_endpoint_states_impact(client):
    body = client.get("/api/gaps").json()
    assert body["items"]
    for g in body["items"]:
        assert g["impact"]


def test_feedback_post_requires_role(client):
    bad = client.post("/api/feedback", json={"card_id": "s-01", "role": "", "direction": "偏高"})
    assert bad.status_code == 422
    ok = client.post(
        "/api/feedback",
        json={"card_id": "s-01", "role": "客服专员", "direction": "偏低", "reason": "旺季更多"},
    )
    assert ok.status_code == 200
    assert ok.json()["feedback_id"]


def test_feedback_rejects_satisfaction_style_direction(client):
    r = client.post("/api/feedback", json={"card_id": "s-01", "role": "老板", "direction": "很满意"})
    assert r.status_code == 422


def test_frontend_is_served(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "明辉" in r.text or "提效" in r.text


def test_insights_endpoint_exposes_provenance(client):
    """经验判断必须标明来源。

    这一区按设计"无数据支撑"，那么它唯一的可信度线索就是来源：
    顾问定稿的判断与模型现场生成的判断，读者该给的信任完全不同。
    反评审页早就有「模型现场生成 / 定稿内容」标记，这里此前是缺的。
    """
    body = client.get("/api/insights").json()
    assert body["generated_by"], "缺少整体来源标记"
    for item in body["items"]:
        assert item["source"] in ("curated", "llm", "fallback"), item.get("source")
        assert item["source_label"], "每条都要有可直接显示的来源文案"

    # 未启用模型时不得声称是模型生成——那是在夸大自动化程度
    assert body["generated_by"] == "定稿内容"
    assert all(i["source"] == "curated" for i in body["items"])
