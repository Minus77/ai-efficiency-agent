"""Task 5：连接器与效果衡量的 API（§4、§19.4、§13.3 凭据边界）。"""
import io

import pytest
from fastapi.testclient import TestClient

from aiea.api import create_app


@pytest.fixture
def app(tmp_path):
    return TestClient(create_app(root=tmp_path))


def _new(app, name="连接器客户"):
    r = app.post("/api/clients", json={
        "name": name, "industry": "建材分销", "headcount": 70, "departments": ["客服"],
    })
    assert r.status_code == 200
    return r.json()["slug"]


# ---------------------------- 可选连接器目录 ----------------------------
def test_catalog_lists_production_connectors(app):
    r = app.get("/api/connectors")
    assert r.status_code == 200
    items = r.json()["items"]
    keys = {i["key"] for i in items}
    for k in ("ticketing_readonly", "crm_readonly", "im_readonly", "erp_readonly", "ecommerce_readonly"):
        assert k in keys


def test_catalog_declares_capabilities_and_limits(app):
    for item in app.get("/api/connectors").json()["items"]:
        assert item["metrics"]
        assert item["known_limits"]
        assert item["max_evidence_grade"] in ("A", "B", "C")
        assert item["auth_hint"]


def test_catalog_hides_test_only_connectors(app):
    keys = {i["key"] for i in app.get("/api/connectors").json()["items"]}
    assert "demo_injection" not in keys, "测试专用连接器不应出现在客户可见目录"


# ---------------------------- 绑定 ----------------------------
def test_bind_connector_never_echoes_secret(app):
    slug = _new(app)
    r = app.post(f"/api/clients/{slug}/connectors", json={
        "key": "ticketing_readonly", "key_id": "kid-1", "secret": "TOP-SECRET-9",
    })
    assert r.status_code == 200
    assert "TOP-SECRET-9" not in r.text
    assert r.json()["credential"]["secret_present"] is True

    listed = app.get(f"/api/clients/{slug}/connectors")
    assert "TOP-SECRET-9" not in listed.text


def test_bind_unknown_connector_400(app):
    slug = _new(app)
    r = app.post(f"/api/clients/{slug}/connectors", json={
        "key": "nope", "key_id": "k", "secret": "s",
    })
    assert r.status_code == 400


def test_bind_requires_existing_client(app):
    r = app.post("/api/clients/does-not-exist/connectors", json={
        "key": "ticketing_readonly", "key_id": "k", "secret": "s",
    })
    assert r.status_code == 404


def test_bindings_are_per_tenant(app):
    a, b = _new(app, "甲客户"), _new(app, "乙客户")
    app.post(f"/api/clients/{a}/connectors", json={
        "key": "crm_readonly", "key_id": "k", "secret": "s",
    })
    assert len(app.get(f"/api/clients/{a}/connectors").json()["items"]) == 1
    assert len(app.get(f"/api/clients/{b}/connectors").json()["items"]) == 0


# ---------------------------- 同步 ----------------------------
def test_sync_returns_row_count_and_grade(app):
    slug = _new(app)
    app.post(f"/api/clients/{slug}/connectors", json={
        "key": "ticketing_readonly", "key_id": "k", "secret": "s",
    })
    r = app.post(f"/api/clients/{slug}/connectors/ticketing_readonly/sync")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["row_count"] > 100
    assert body["evidence_grade"] == "A"
    assert body["source_kind"] == "L1"

    # 同步产生的材料应出现在材料列表里，与 L0 上传并列
    mats = app.get(f"/api/clients/{slug}/materials").json()["items"]
    assert any(m.get("stored_as", "").startswith("ticketing_readonly") for m in mats)


def test_sync_unbound_returns_409(app):
    slug = _new(app)
    r = app.post(f"/api/clients/{slug}/connectors/ticketing_readonly/sync")
    assert r.status_code == 409
    assert "未绑定" in r.json()["detail"]


def test_synced_data_feeds_diagnosis(app):
    """核心：L1 同步的数据可直接支撑诊断，无需人工上传。"""
    slug = _new(app)
    app.post(f"/api/clients/{slug}/connectors", json={
        "key": "ticketing_readonly", "key_id": "k", "secret": "s",
    })
    app.post(f"/api/clients/{slug}/connectors/ticketing_readonly/sync")

    d = app.post(f"/api/clients/{slug}/diagnose")
    assert d.status_code == 200, d.text
    assert d.json()["scenarios"] > 0
    ov = app.get(f"/api/clients/{slug}/overview").json()
    assert ov["scorecard"]["evidence_traceability"] == 1.0
    assert ov["headline"]["deduped_sum"] > 0


def test_cross_tenant_sync_is_404(app):
    slug = _new(app)
    app.post(f"/api/clients/{slug}/connectors", json={
        "key": "ticketing_readonly", "key_id": "k", "secret": "s",
    })
    r = app.post("/api/clients/other-tenant/connectors/ticketing_readonly/sync")
    assert r.status_code == 404


# ---------------------------- 效果衡量 ----------------------------
def test_effect_page_without_baseline_says_it_cannot_measure(app):
    slug = _new(app)
    r = app.get(f"/api/clients/{slug}/effect")
    assert r.status_code == 200
    body = r.json()
    assert body["baselines"] == []
    assert body["measurements"] == []
    assert "基线" in body["rule"]


def test_capture_baseline_then_measure(app):
    slug = _new(app)
    b = app.post(f"/api/clients/{slug}/baselines", json={
        "card_id": "s-01", "metric": "该环节处理时长",
        "value": 30.0, "sample_size": 40, "source": "工单只读 API（改造前）",
    })
    assert b.status_code == 200
    m = app.post(f"/api/clients/{slug}/measurements", json={
        "card_id": "s-01", "metric": "该环节处理时长",
        "value": 18.0, "sample_size": 44, "source": "工单只读 API（改造后）",
    })
    assert m.status_code == 200
    assert m.json()["improvement_pct"] == pytest.approx(40.0, abs=0.1)
    assert m.json()["direction"] == "改善"

    summary = app.get(f"/api/clients/{slug}/effect").json()
    assert summary["improved_count"] == 1


def test_measure_without_baseline_returns_422_with_actionable_hint(app):
    slug = _new(app)
    r = app.post(f"/api/clients/{slug}/measurements", json={
        "card_id": "s-09", "metric": "该环节处理时长",
        "value": 18.0, "sample_size": 40, "source": "后测",
    })
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert "基线" in detail


def test_business_outcome_metric_is_rejected(app):
    slug = _new(app)
    r = app.post(f"/api/clients/{slug}/baselines", json={
        "card_id": "s-01", "metric": "营收", "value": 120000.0,
        "sample_size": 30, "source": "ERP",
    })
    assert r.status_code == 422
    assert "过程指标" in r.json()["detail"] or "经营结果" in r.json()["detail"]


def test_effect_is_per_tenant(app):
    a, b = _new(app, "效果甲"), _new(app, "效果乙")
    app.post(f"/api/clients/{a}/baselines", json={
        "card_id": "s-01", "metric": "该环节处理时长", "value": 30.0,
        "sample_size": 40, "source": "基线",
    })
    assert len(app.get(f"/api/clients/{a}/effect").json()["baselines"]) == 1
    assert len(app.get(f"/api/clients/{b}/effect").json()["baselines"]) == 0
