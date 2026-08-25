"""术语表与分级标准的 API：界面要能查词、能展开分级标准。"""
import pytest
from fastapi.testclient import TestClient

from aiea.api import create_app


@pytest.fixture
def app(tmp_path):
    return TestClient(create_app(root=tmp_path))


def test_glossary_endpoint_returns_all_terms(app):
    r = app.get("/api/glossary")
    assert r.status_code == 200
    body = r.json()
    assert len(body["terms"]) >= 25
    for t in body["terms"]:
        assert t["label"] and t["plain"] and t["why"]


def test_glossary_includes_all_scales(app):
    body = app.get("/api/glossary").json()
    assert [g["grade"] for g in body["grade_scale"]] == ["A", "B", "C"]
    assert len(body["work_form_scale"]) == 3
    assert len(body["difficulty_scale"]["dimensions"]) == 7
    assert len(body["delivery_scale"]) == 3


def test_grade_scale_states_output_limits(app):
    scale = {g["grade"]: g for g in app.get("/api/glossary").json()["grade_scale"]}
    assert "点估" in scale["A"]["output"]
    assert "不给" in scale["C"]["output"]
    for g in scale.values():
        assert g["criteria"] and g["example"]


def test_single_term_lookup(app):
    r = app.get("/api/glossary/折现")
    assert r.status_code == 200
    assert r.json()["label"]
    assert r.json()["plain"]


def test_unknown_term_404(app):
    assert app.get("/api/glossary/不存在的词").status_code == 404


def test_internal_terms_are_not_exposed(app):
    """内部实现词不该出现在用户术语表里。"""
    body = app.get("/api/glossary").json()
    blob = str(body)
    for internal in ("insufficient_data", "no_grounding", "taskcard_upsert", "metric_probe"):
        assert internal not in blob, f"{internal} 泄漏到用户术语表"
    for internal in ("insufficient_data", "no_grounding"):
        assert app.get(f"/api/glossary/{internal}").status_code == 404


def test_difficulty_weights_match_rubric(app):
    from aiea.feasibility import DIMENSIONS

    dims = app.get("/api/glossary").json()["difficulty_scale"]["dimensions"]
    assert {d["name"] for d in dims} == set(DIMENSIONS)
    for d in dims:
        assert abs(d["weight"] - DIMENSIONS[d["name"]]) < 1e-9


def test_glossary_exposes_grouped_terms(app):
    """参考页按主题分块渲染，分组必须随接口一起下发。"""
    body = app.get("/api/glossary").json()
    assert len(body["groups"]) >= 6
    listed = [t["key"] for g in body["groups"] for t in g["terms"]]
    assert len(listed) == len(body["terms"]), "分组里的词数与术语总数不一致，有词漏出参考页"
    for g in body["groups"]:
        assert g["group"] and g["intro"] and g["terms"]


def test_glossary_exposes_tier_and_severity_scales(app):
    """ROI 三档与反评审严重度也是分级，判定标准同样要随接口下发。"""
    body = app.get("/api/glossary").json()

    tiers = body["tier_scale"]
    assert [t["tier"] for t in tiers] == ["保守", "中性", "乐观"]
    for t in tiers:
        assert t["criteria"] and t["why"]

    sev = body["severity_scale"]
    assert [s["level"] for s in sev] == ["高", "中", "低"]
    for s in sev:
        assert s["criteria"] and s["action"], "严重度必须同时说明判定标准与应对动作"
