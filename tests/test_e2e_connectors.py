"""Task 6：连接器与效果衡量的全链路端到端验收。

锁住这条链路的核心性质：
- 连接 → 同步 → 诊断，全程无需人工上传任何文件
- L1 与 L0 双轨并行（不是二选一），且 L1 用于校验 L0 自述
- 连接器数据变了，结论必须跟着变
- 无基线时效果页不给任何数字
"""
import io

import pytest
from fastapi.testclient import TestClient

from aiea.api import create_app


@pytest.fixture
def app(tmp_path):
    return TestClient(create_app(root=tmp_path))


def _new(app, name="连接器全链路"):
    r = app.post("/api/clients", json={
        "name": name, "industry": "建材分销", "headcount": 70, "departments": ["客服", "财务"],
    })
    assert r.status_code == 200
    return r.json()["slug"]


def _bind(app, slug, key):
    return app.post(f"/api/clients/{slug}/connectors", json={
        "key": key, "key_id": "readonly-1", "secret": "s3cr3t",
    })


def _sync(app, slug, key):
    return app.post(f"/api/clients/{slug}/connectors/{key}/sync")


# ============================ 纯 L1 链路 ============================
def test_connector_only_chain_needs_no_manual_upload(app):
    """核心：全程不上传任何文件，靠连接器就能出报告。"""
    slug = _new(app)
    assert _bind(app, slug, "ticketing_readonly").status_code == 200
    sync = _sync(app, slug, "ticketing_readonly").json()
    assert sync["row_count"] > 100
    assert sync["evidence_grade"] == "A"

    d = app.post(f"/api/clients/{slug}/diagnose")
    assert d.status_code == 200, d.text
    assert d.json()["scenarios"] > 0
    assert d.json()["quantified"] > 0

    ov = app.get(f"/api/clients/{slug}/overview").json()
    assert ov["scorecard"]["evidence_traceability"] == 1.0
    assert ov["headline"]["deduped_sum"] > 0


def test_all_report_views_render_from_connector_data(app):
    slug = _new(app)
    _bind(app, slug, "ticketing_readonly")
    _sync(app, slug, "ticketing_readonly")
    app.post(f"/api/clients/{slug}/diagnose")
    for view in ("overview", "scenarios", "matrix", "roi", "roadmap",
                 "evidence", "insights", "gaps", "counter-review", "observability"):
        r = app.get(f"/api/clients/{slug}/{view}")
        assert r.status_code == 200, f"{view} -> {r.status_code}"


def test_multiple_connectors_are_parallel_tracks(app):
    """L0 与 L1 并行而非二选一：手工上传 + 两个连接器同时进证据台账。"""
    slug = _new(app)
    manual = "\n".join(
        ["order_no,created_at,amount"]
        + [f"M{i},2026-03-12T10:{i % 55:02d}:00,{100 + i}" for i in range(30)]
    )
    app.post(
        f"/api/clients/{slug}/materials",
        files={"file": ("manual.csv", io.BytesIO(manual.encode("utf-8")), "text/csv")},
    )
    _bind(app, slug, "ticketing_readonly")
    _sync(app, slug, "ticketing_readonly")
    _bind(app, slug, "ecommerce_readonly")
    _sync(app, slug, "ecommerce_readonly")

    app.post(f"/api/clients/{slug}/diagnose")
    origins = {e["origin"] for e in app.get(f"/api/clients/{slug}/evidence").json()["items"]}
    assert any("manual.csv" in o for o in origins), "手工上传的材料应仍在台账里"
    assert any("ticketing_readonly" in o for o in origins)
    assert any("ecommerce_readonly" in o for o in origins)


def test_im_connector_alone_cannot_produce_money(app):
    """IM 只有汇总：即使同步成功，也不得给出任何金额。"""
    slug = _new(app, "仅 IM 客户")
    _bind(app, slug, "im_readonly")
    sync = _sync(app, slug, "im_readonly").json()
    assert sync["evidence_grade"] == "C"

    app.post(f"/api/clients/{slug}/diagnose")
    roi = app.get(f"/api/clients/{slug}/roi").json()
    for item in roi["items"]:
        assert item["amount"] is None
        assert item["tiers"] == []
    assert roi["aggregate"]["deduped_sum"] == 0


def test_erp_connector_caps_roi_at_range(app):
    """ERP 无时间戳 → B 级上限，不得给点估。"""
    slug = _new(app, "ERP 客户")
    _bind(app, slug, "erp_readonly")
    assert _sync(app, slug, "erp_readonly").json()["evidence_grade"] == "B"
    app.post(f"/api/clients/{slug}/diagnose")
    for item in app.get(f"/api/clients/{slug}/roi").json()["items"]:
        assert item["amount"] is None, "B 级不得给点估"


# ============================ 数据变则结论变 ============================
def test_different_tenants_get_different_conclusions(app):
    """连接器按 tenant 返回不同数据，结论必须跟着不同。"""
    a, b = _new(app, "甲连接客户"), _new(app, "乙连接客户")
    for slug in (a, b):
        _bind(app, slug, "ticketing_readonly")
        _sync(app, slug, "ticketing_readonly")
        app.post(f"/api/clients/{slug}/diagnose")

    def total(slug):
        sc = app.get(f"/api/clients/{slug}/scenarios").json()
        return sum(c["monthly_minutes"] for p in sc["parents"] for c in p["children"])

    assert total(a) != total(b), "不同客户的数据不同，工时不该完全相同"


def test_sync_twice_does_not_double_count_into_one_material(app):
    slug = _new(app)
    _bind(app, slug, "ticketing_readonly")
    first = _sync(app, slug, "ticketing_readonly").json()
    second = _sync(app, slug, "ticketing_readonly").json()
    assert first["stored_as"] != second["stored_as"], "两次同步应各自落盘，便于追溯"
    mats = app.get(f"/api/clients/{slug}/materials").json()["items"]
    assert len([m for m in mats if m.get("stored_as")]) >= 2


# ============================ 安全 ============================
def test_secret_never_appears_in_any_response(app):
    slug = _new(app)
    app.post(f"/api/clients/{slug}/connectors", json={
        "key": "crm_readonly", "key_id": "kid", "secret": "NEVER-SHOW-ME",
    })
    _sync(app, slug, "crm_readonly")
    for path in ("connectors", "materials", "effect"):
        assert "NEVER-SHOW-ME" not in app.get(f"/api/clients/{slug}/{path}").text
    assert "NEVER-SHOW-ME" not in app.get("/api/connectors").text


def test_write_verbs_have_no_route_at_all(app):
    """连接器没有写入路由——不是"拒绝写"，而是根本不存在这条路。"""
    slug = _new(app)
    _bind(app, slug, "ticketing_readonly")
    for method in ("put", "patch", "delete"):
        r = getattr(app, method)(f"/api/clients/{slug}/connectors/ticketing_readonly/sync")
        assert r.status_code in (404, 405)


# ============================ 效果衡量链路 ============================
def test_full_measurement_chain(app):
    """连接器拉基线 → 诊断 → 改造后复测 → 出效果结论。"""
    slug = _new(app, "效果全链路")
    _bind(app, slug, "ticketing_readonly")
    _sync(app, slug, "ticketing_readonly")
    app.post(f"/api/clients/{slug}/diagnose")

    cards = app.get(f"/api/clients/{slug}/scenarios").json()["parents"][0]["children"]
    card_id = cards[0]["card_id"]

    b = app.post(f"/api/clients/{slug}/baselines", json={
        "card_id": card_id, "metric": "该环节处理时长",
        "value": 5.0, "sample_size": 240, "source": "工单只读 API（改造前）",
    })
    assert b.status_code == 200

    m = app.post(f"/api/clients/{slug}/measurements", json={
        "card_id": card_id, "metric": "该环节处理时长",
        "value": 2.5, "sample_size": 250, "source": "工单只读 API（改造后）",
    })
    assert m.status_code == 200
    body = m.json()
    assert body["direction"] == "改善"
    assert body["improvement_pct"] == pytest.approx(50.0, abs=0.1)
    assert body["low_confidence"] is False

    summary = app.get(f"/api/clients/{slug}/effect").json()
    assert summary["improved_count"] == 1
    row = summary["measurements"][0]
    assert row["baseline_value"] == 5.0
    assert row["measured_value"] == 2.5


def test_effect_without_baseline_gives_no_number(app):
    slug = _new(app, "无基线客户")
    r = app.post(f"/api/clients/{slug}/measurements", json={
        "card_id": "s-01", "metric": "该环节处理时长",
        "value": 2.0, "sample_size": 100, "source": "后测",
    })
    assert r.status_code == 422
    assert "基线" in r.json()["detail"]
    summary = app.get(f"/api/clients/{slug}/effect").json()
    assert summary["measurements"] == []
    assert summary["improved_count"] == 0


def test_regression_is_surfaced_not_hidden(app):
    slug = _new(app, "退步客户")
    app.post(f"/api/clients/{slug}/baselines", json={
        "card_id": "s-01", "metric": "该环节处理时长",
        "value": 3.0, "sample_size": 100, "source": "基线",
    })
    m = app.post(f"/api/clients/{slug}/measurements", json={
        "card_id": "s-01", "metric": "该环节处理时长",
        "value": 4.2, "sample_size": 100, "source": "后测",
    }).json()
    assert m["direction"] == "退步"
    assert m["improvement_pct"] < 0
    assert "排查" in m["note"]
    assert app.get(f"/api/clients/{slug}/effect").json()["regressed_count"] == 1


def test_baseline_versioning_keeps_history(app):
    slug = _new(app, "基线版本客户")
    for v, val in ((1, 30.0), (2, 28.0)):
        r = app.post(f"/api/clients/{slug}/baselines", json={
            "card_id": "s-01", "metric": "该环节处理时长",
            "value": val, "sample_size": 40, "source": f"第 {v} 次",
        })
        assert r.json()["version"] == v
    # 后测应对齐最新版本
    m = app.post(f"/api/clients/{slug}/measurements", json={
        "card_id": "s-01", "metric": "该环节处理时长",
        "value": 14.0, "sample_size": 40, "source": "后测",
    }).json()
    assert m["baseline"]["value"] == 28.0
