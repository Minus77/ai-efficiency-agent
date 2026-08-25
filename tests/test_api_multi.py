"""Task 5：多客户 API —— 建档、上传、诊断、按租户取交付物。"""
import io
import json

import pytest
from fastapi.testclient import TestClient

from aiea.api import create_app

TICKETS = "\n".join(
    ["ticket_no,created_at,first_response_at,category"]
    + [
        f"WD{1000 + i},2026-03-{12 + i // 18:02d}T09:{(i * 3) % 55:02d}:00,"
        f"2026-03-{12 + i // 18:02d}T09:{(i * 3 + 3) % 58:02d}:00,"
        + ("送货时间" if i % 3 else "开票问题")
        for i in range(36)
    ]
)
SUMMARY = "月份,咨询总量,平均时长\n2026-03,612,3.2"


@pytest.fixture
def client(tmp_path):
    return TestClient(create_app(root=tmp_path))


def _upload(client, slug, filename, body, role="R1"):
    return client.post(
        f"/api/clients/{slug}/materials",
        files={"file": (filename, io.BytesIO(body.encode("utf-8")), "text/csv")},
        data={"evidence_role": role},
    )


# ---------------------------- 客户 CRUD ----------------------------
def test_list_clients_starts_with_preset(client):
    r = client.get("/api/clients")
    assert r.status_code == 200
    items = r.json()["items"]
    assert any(c["is_preset"] for c in items), "预置演示客户应可见"


def test_create_client_returns_slug(client):
    r = client.post("/api/clients", json={
        "name": "新客户建材", "industry": "建材分销", "headcount": 70, "departments": ["客服", "财务"],
    })
    assert r.status_code == 200
    body = r.json()
    assert body["slug"]
    assert body["status"] == "draft"


def test_create_rejects_empty_name(client):
    r = client.post("/api/clients", json={"name": "  ", "industry": "零售", "headcount": 50})
    assert r.status_code == 422


def test_out_of_scope_headcount_is_flagged_not_blocked(client):
    r = client.post("/api/clients", json={"name": "小微企业", "industry": "零售", "headcount": 8})
    assert r.status_code == 200
    assert r.json()["out_of_scope"] is True
    assert "范围外" in r.json()["scope_note"]


def test_get_unknown_client_404(client):
    assert client.get("/api/clients/nope").status_code == 404


def test_traversal_slug_is_404_not_500(client):
    assert client.get("/api/clients/..%2F..%2Fetc").status_code in (404, 400)


def test_delete_client(client):
    slug = client.post("/api/clients", json={"name": "待删客户", "industry": "零售", "headcount": 40}).json()["slug"]
    assert client.delete(f"/api/clients/{slug}").status_code == 200
    assert client.get(f"/api/clients/{slug}").status_code == 404


def test_preset_client_cannot_be_deleted(client):
    presets = [c for c in client.get("/api/clients").json()["items"] if c["is_preset"]]
    r = client.delete(f"/api/clients/{presets[0]['slug']}")
    assert r.status_code == 400


# ---------------------------- 材料上传 ----------------------------
def test_upload_material_returns_probe_result(client):
    slug = client.post("/api/clients", json={"name": "上传测试", "industry": "零售", "headcount": 60}).json()["slug"]
    r = _upload(client, slug, "tickets.csv", TICKETS)
    assert r.status_code == 200
    body = r.json()
    assert body["accepted"] is True
    assert body["reachable_grade"] == "A"
    assert body["delivery_form"] == "完整诊断"
    assert body["row_count"] == 36


def test_upload_summary_only_caps_at_light_delivery(client):
    slug = client.post("/api/clients", json={"name": "汇总测试", "industry": "零售", "headcount": 60}).json()["slug"]
    body = _upload(client, slug, "summary.csv", SUMMARY).json()
    assert body["reachable_grade"] == "C"
    assert body["delivery_form"] == "轻量咨询"


def test_upload_rejects_disallowed_type(client):
    slug = client.post("/api/clients", json={"name": "类型测试", "industry": "零售", "headcount": 60}).json()["slug"]
    r = client.post(
        f"/api/clients/{slug}/materials",
        files={"file": ("bad.exe", io.BytesIO(b"MZ"), "application/octet-stream")},
    )
    assert r.status_code == 200
    assert r.json()["accepted"] is False


def test_upload_to_unknown_client_404(client):
    r = _upload(client, "nope", "t.csv", TICKETS)
    assert r.status_code == 404


def test_list_materials(client):
    slug = client.post("/api/clients", json={"name": "列材料", "industry": "零售", "headcount": 60}).json()["slug"]
    _upload(client, slug, "tickets.csv", TICKETS)
    r = client.get(f"/api/clients/{slug}/materials")
    assert r.status_code == 200
    assert len(r.json()["items"]) == 1
    assert r.json()["items"][0]["evidence_role"] == "R1"


# ---------------------------- 诊断 ----------------------------
def test_diagnose_without_materials_returns_409(client):
    slug = client.post("/api/clients", json={"name": "无材料", "industry": "零售", "headcount": 60}).json()["slug"]
    r = client.post(f"/api/clients/{slug}/diagnose")
    assert r.status_code == 409
    assert "材料" in r.json()["detail"]


def test_report_before_diagnosis_returns_409(client):
    slug = client.post("/api/clients", json={"name": "未诊断", "industry": "零售", "headcount": 60}).json()["slug"]
    r = client.get(f"/api/clients/{slug}/overview")
    assert r.status_code == 409


def test_full_flow_create_upload_diagnose_report(client):
    slug = client.post("/api/clients", json={
        "name": "全链路客户", "industry": "建材分销", "headcount": 70, "departments": ["客服"],
    }).json()["slug"]
    assert _upload(client, slug, "tickets.csv", TICKETS).json()["accepted"] is True

    d = client.post(f"/api/clients/{slug}/diagnose")
    assert d.status_code == 200, d.text
    assert d.json()["scenarios"] > 0

    ov = client.get(f"/api/clients/{slug}/overview")
    assert ov.status_code == 200
    assert ov.json()["client"]["short_name"] == "全链路客户"
    assert ov.json()["scorecard"]["evidence_traceability"] == 1.0

    for view in ("scenarios", "matrix", "roi", "roadmap", "evidence", "insights", "gaps", "counter-review", "observability"):
        r = client.get(f"/api/clients/{slug}/{view}")
        assert r.status_code == 200, f"{view} -> {r.status_code}"


def test_report_is_per_tenant_no_leak(client):
    a = client.post("/api/clients", json={"name": "甲客户", "industry": "零售", "headcount": 50}).json()["slug"]
    b = client.post("/api/clients", json={"name": "乙客户", "industry": "制造", "headcount": 60}).json()["slug"]
    _upload(client, a, "tickets.csv", TICKETS)
    _upload(client, b, "tickets.csv", TICKETS)
    client.post(f"/api/clients/{a}/diagnose")
    client.post(f"/api/clients/{b}/diagnose")
    assert client.get(f"/api/clients/{a}/overview").json()["client"]["short_name"] == "甲客户"
    assert client.get(f"/api/clients/{b}/overview").json()["client"]["short_name"] == "乙客户"


def test_feedback_is_scoped_to_client(client):
    slug = client.post("/api/clients", json={"name": "反馈客户", "industry": "零售", "headcount": 50}).json()["slug"]
    _upload(client, slug, "tickets.csv", TICKETS)
    client.post(f"/api/clients/{slug}/diagnose")
    r = client.post(f"/api/clients/{slug}/feedback", json={
        "card_id": "s-01", "role": "客服组长", "direction": "偏低", "reason": "旺季更多",
    })
    assert r.status_code == 200
    assert client.post(f"/api/clients/{slug}/feedback", json={
        "card_id": "s-01", "role": "", "direction": "偏高",
    }).status_code == 422


# ---------------------------- 预置客户仍可用 ----------------------------
def test_preset_client_report_still_works(client):
    presets = [c for c in client.get("/api/clients").json()["items"] if c["is_preset"]]
    slug = presets[0]["slug"]
    r = client.get(f"/api/clients/{slug}/overview")
    assert r.status_code == 200
    assert r.json()["scorecard"]["evidence_traceability"] == 1.0


def test_legacy_endpoints_still_serve_preset(client):
    """旧的无租户路径继续可用，指向预置客户——避免破坏已有链接。"""
    r = client.get("/api/overview")
    assert r.status_code == 200
    assert r.json()["client"]["short_name"] == "明辉家居建材"
