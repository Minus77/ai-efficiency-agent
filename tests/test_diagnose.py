"""Task 4：诊断编排 —— 把 S0–S5 跑在真实上传材料上。

关键约束：产出的 report 必须与 seed 版**同一 schema**，否则前端 9 个视图要写两套。
"""
import json

import pytest

from aiea.clients import ClientRegistry
from aiea.diagnose import DiagnosisNotReady, run_diagnosis
from aiea.intake import save_material
from aiea.seed import run_seed_diagnosis

TICKETS = "\n".join(
    ["ticket_no,created_at,first_response_at,category"]
    + [
        f"WD{1000 + i},2026-03-{12 + i // 18:02d}T09:{(i * 3) % 55:02d}:00,"
        f"2026-03-{12 + i // 18:02d}T09:{(i * 3 + 3) % 58:02d}:00,"
        + ("送货时间" if i % 3 else "开票问题")
        for i in range(36)
    ]
)
RECON = "\n".join(
    ["edited_at,action"]
    + [f"2026-03-0{1 + i // 20}T{9 + (i % 20) // 8:02d}:{(i * 4) % 59:02d}:00,手工比对填写" for i in range(40)]
)
NOTES = "# 纪要\n\n财务提出月初对账压力大，经常加班。决定先看看有没有工具能帮忙。"


@pytest.fixture
def client_with_materials(tmp_path):
    reg = ClientRegistry(root=tmp_path)
    c = reg.create(name="河东建材", industry="建材分销", headcount=70, departments=["客服", "财务"])
    for fn, body in (("tickets.csv", TICKETS), ("recon.csv", RECON), ("notes.md", NOTES)):
        save_material(root=tmp_path, slug=c.slug, filename=fn, content=body.encode("utf-8"), evidence_role="R1")
    return tmp_path, c.slug


def test_no_materials_raises_ready_error(tmp_path):
    reg = ClientRegistry(root=tmp_path)
    c = reg.create(name="空客户", industry="零售", headcount=50, departments=["客服"])
    with pytest.raises(DiagnosisNotReady) as exc:
        run_diagnosis(tenant=c.slug, root=tmp_path)
    assert "材料" in str(exc.value)


def test_unknown_client_raises(tmp_path):
    with pytest.raises(DiagnosisNotReady):
        run_diagnosis(tenant="nope", root=tmp_path)


def test_report_schema_matches_seed_version(client_with_materials, tmp_path_factory):
    root, slug = client_with_materials
    got = run_diagnosis(tenant=slug, root=root)
    seed = run_seed_diagnosis(root=tmp_path_factory.mktemp("seed"))
    missing = set(seed.keys()) - set(got.keys())
    assert not missing, f"缺少字段会让前端视图报错：{missing}"


def test_cards_are_derived_from_uploaded_materials(client_with_materials):
    root, slug = client_with_materials
    r = run_diagnosis(tenant=slug, root=root)
    assert r["cards"], "有材料就应该推出场景"
    for c in r["cards"]:
        assert c["evidence_refs"]
    ids = {e["evidence_id"] for e in r["evidence"]}
    for c in r["cards"]:
        assert set(c["evidence_refs"]) <= ids


def test_traceability_is_one_hundred_percent(client_with_materials):
    root, slug = client_with_materials
    r = run_diagnosis(tenant=slug, root=root)
    assert r["scorecard"]["evidence_traceability"] == 1.0


def test_grade_c_cards_carry_no_money(client_with_materials):
    root, slug = client_with_materials
    r = run_diagnosis(tenant=slug, root=root)
    for card in r["cards"]:
        if card["evidence_grade"] == "C":
            roi = r["roi"][card["card_id"]]
            assert roi["amount"] is None
            assert roi["tiers"] == []
            assert roi["implementation_cost_low"] is None


def test_matrix_thresholds_are_consistent_with_quadrants(client_with_materials):
    root, slug = client_with_materials
    r = run_diagnosis(tenant=slug, root=root)
    t = r["matrix_thresholds"]
    for i in r["matrix"]:
        hb = i["benefit"] >= t["benefit"]
        hd = i["difficulty"] >= t["difficulty"]
        expect = {(True, False): "先做", (True, True): "规划", (False, False): "顺手做", (False, True): "不做"}[(hb, hd)]
        assert i["quadrant"] == expect


def test_report_is_persisted_for_resume(client_with_materials):
    root, slug = client_with_materials
    run_diagnosis(tenant=slug, root=root)
    saved = json.loads((root / slug / "REPORT.json").read_text(encoding="utf-8"))
    assert saved["cards"]
    assert (root / slug / "FINDINGS.md").exists()


def test_client_status_becomes_diagnosed(client_with_materials):
    root, slug = client_with_materials
    run_diagnosis(tenant=slug, root=root)
    reg = ClientRegistry(root=root)
    assert reg.get(slug).status == "diagnosed"
    assert reg.get(slug).has_report is True


def test_assumptions_and_disclaimer_present(client_with_materials):
    root, slug = client_with_materials
    r = run_diagnosis(tenant=slug, root=root)
    assert len(r["assumptions"]) >= 4
    assert any("非投资承诺" in a for a in r["assumptions"])


def test_gaps_are_reported_for_notes_only_material(client_with_materials):
    root, slug = client_with_materials
    r = run_diagnosis(tenant=slug, root=root)
    assert r["gaps"]
    for g in r["gaps"]:
        assert g["impact"]


def test_client_profile_flows_into_report(client_with_materials):
    root, slug = client_with_materials
    r = run_diagnosis(tenant=slug, root=root)
    assert r["client"]["short_name"] == "河东建材"
    assert r["client"]["headcount"] == 70
    assert r["scope"]["as_of"]


def test_changing_materials_changes_the_report(tmp_path):
    """核心断言：同样的代码、不同的材料 → 不同的结论。"""
    reg = ClientRegistry(root=tmp_path)
    a = reg.create(name="甲厂", industry="零售", headcount=50, departments=["客服"])
    b = reg.create(name="乙厂", industry="零售", headcount=50, departments=["客服"])
    save_material(root=tmp_path, slug=a.slug, filename="t.csv", content=TICKETS.encode("utf-8"))
    few = "\n".join(
        ["ticket_no,created_at,first_response_at,category"]
        + [f"WD{i},2026-03-12T09:{i:02d}:00,2026-03-12T09:{i + 2:02d}:00,送货时间" for i in range(10)]
    )
    save_material(root=tmp_path, slug=b.slug, filename="t.csv", content=few.encode("utf-8"))

    ra = run_diagnosis(tenant=a.slug, root=tmp_path)
    rb = run_diagnosis(tenant=b.slug, root=tmp_path)
    ta = sum(c["monthly_minutes"] for c in ra["cards"])
    tb = sum(c["monthly_minutes"] for c in rb["cards"])
    assert ta != tb


def test_tenant_isolation_reports_do_not_leak(tmp_path):
    reg = ClientRegistry(root=tmp_path)
    a = reg.create(name="A公司", industry="零售", headcount=50, departments=["客服"])
    b = reg.create(name="B公司", industry="制造", headcount=60, departments=["财务"])
    save_material(root=tmp_path, slug=a.slug, filename="t.csv", content=TICKETS.encode("utf-8"))
    save_material(root=tmp_path, slug=b.slug, filename="r.csv", content=RECON.encode("utf-8"))
    ra = run_diagnosis(tenant=a.slug, root=tmp_path)
    rb = run_diagnosis(tenant=b.slug, root=tmp_path)
    assert ra["client"]["short_name"] != rb["client"]["short_name"]
    a_files = {e["origin"] for e in ra["evidence"]}
    assert not any("r.csv" in o for o in a_files)


def test_security_counters_present(client_with_materials):
    root, slug = client_with_materials
    r = run_diagnosis(tenant=slug, root=root)
    assert "injection_escaped" in r["security"]
    assert r["security"]["injection_escaped"] == 0
