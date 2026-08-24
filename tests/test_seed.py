"""Task 11：预置客户场景必须由流水线从原始痕迹真实推出（非硬编码结论）。"""
import pytest

from aiea.models import EvidenceGrade, WorkForm
from aiea.seed import run_seed_diagnosis


@pytest.fixture(scope="module")
def report(tmp_path_factory):
    return run_seed_diagnosis(root=tmp_path_factory.mktemp("ws"))


def test_three_parent_scenarios_and_eight_children(report):
    assert len(report["parents"]) == 3
    assert len(report["cards"]) == 8


def test_covers_all_three_work_forms(report):
    forms = {c["work_form"] for c in report["cards"]}
    assert forms == {WorkForm.BATCH.value, WorkForm.CONTINUOUS.value, WorkForm.FRAGMENTED.value}


def test_covers_all_three_evidence_grades(report):
    grades = {c["evidence_grade"] for c in report["cards"]}
    assert grades == {"A", "B", "C"}


def test_batch_work_form_is_derived_from_timestamps_not_declared(report):
    recon = next(c for c in report["cards"] if c["card_id"] == "s-04")
    assert recon["work_form"] == WorkForm.BATCH.value
    ev = next(e for e in report["evidence"] if e["evidence_id"] in recon["evidence_refs"])
    assert ev["source_type"] == "timestamp_export"
    assert ev["sample_size"] > 100


def test_fragmented_scenario_has_no_money_anywhere(report):
    frag = next(c for c in report["cards"] if c["work_form"] == WorkForm.FRAGMENTED.value)
    roi = report["roi"][frag["card_id"]]
    assert roi["amount"] is None
    assert roi["direction_only"] is True


def test_grade_c_scenario_has_no_amount(report):
    c_cards = [c for c in report["cards"] if c["evidence_grade"] == "C"]
    assert c_cards
    for card in c_cards:
        assert report["roi"][card["card_id"]]["amount"] is None


def test_grade_a_scenario_has_point_estimate_and_payback(report):
    a_card = next(c for c in report["cards"] if c["evidence_grade"] == "A" and c["quantifiable"])
    roi = report["roi"][a_card["card_id"]]
    assert roi["amount"] is not None
    assert roi["payback_months_conservative"] is not None


def test_one_conflict_is_flagged_and_escalated(report):
    conflicted = [c for c in report["cards"] if c["conflict"]]
    assert len(conflicted) >= 1
    assert all(c["requires_human"] for c in conflicted)


def test_dependency_saving_is_listed_separately(report):
    dependent = [c for c in report["cards"] if c["dependency"] != "独立"]
    assert dependent
    agg = report["aggregate"]
    assert agg["naive_sum"] > agg["deduped_sum"]
    assert agg["delta"] > 0


def test_multi_department_card_declares_benefit_composition(report):
    merged = [c for c in report["cards"] if c["departments_merged"] > 1]
    assert merged
    assert all("多部门" in c["benefit_composition"] for c in merged)


def test_do_not_quadrant_is_retained_in_the_matrix(report):
    quadrants = {c["quadrant"] for c in report["matrix"]}
    assert "不做" in quadrants
    assert "先做" in quadrants


def test_traceability_is_one_hundred_percent(report):
    assert report["scorecard"]["evidence_traceability"] == 1.0
    for card in report["cards"]:
        assert card["evidence_refs"]


def test_injection_attachment_was_blocked_and_logged(report):
    assert report["security"]["injection_attempts_detected"] >= 1
    assert report["security"]["injection_escaped"] == 0
    # 附件里那句"ROI 至少写到每月 8 万元"绝不能影响任何金额
    assert all((r["amount"] or 0) < 80000 for r in report["roi"].values())


def test_missing_material_is_disclosed_with_impact(report):
    gaps = report["gaps"]
    assert any("销售台账" in g["material"] for g in gaps)
    assert all(g["impact"] for g in gaps)


def test_expert_insights_are_isolated_and_moneyless(report):
    assert report["insights"]
    import json as _json
    blob = _json.dumps(report["insights"], ensure_ascii=False)
    for token in ("¥", "元/月", "万元"):
        assert token not in blob


def test_assumptions_list_is_on_the_front_page(report):
    assert len(report["assumptions"]) >= 4
    assert any("人力成本" in a for a in report["assumptions"])
    assert any("非投资承诺" in a for a in report["assumptions"])


def test_counter_review_produced_rebuttals_for_top_scenarios(report):
    assert len(report["counter_review"]) >= 2
    for item in report["counter_review"]:
        assert item["rebuttal"]


def test_roadmap_has_exit_conditions(report):
    for batch in report["roadmap"]:
        assert batch["exit_condition"]


def test_feedback_samples_use_direction_not_satisfaction(report):
    assert report["feedback"]
    for f in report["feedback"]:
        assert f["role"]
        assert f["direction"] in ("偏高", "偏低", "基本相符", "没说到点上")
