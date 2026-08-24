"""Task 13：端到端验收门禁（§14 六维记分卡、§13 护栏、§15.1 风险闭合）。

这些断言对应架构文档明确写出的门槛项，任一失败即视为交付缺陷。
"""
import json
from pathlib import Path

import pytest

from aiea.evals import run_golden_set, scorecard_of
from aiea.seed import run_seed_diagnosis


@pytest.fixture(scope="module")
def report(tmp_path_factory):
    return run_seed_diagnosis(root=tmp_path_factory.mktemp("e2e"))


# ---------------- 六维记分卡门槛 ----------------
def test_evidence_traceability_is_100_percent(report):
    # §14.1 本场景升为一等，门槛 100%
    assert scorecard_of(report)["evidence_traceability"] == 1.0


def test_no_quantified_claim_without_citation(report):
    for card in report["cards"]:
        if card["monthly_minutes"] and card["quantifiable"]:
            assert card["evidence_refs"], f"{card['card_id']} 有量化值却无证据引用"


def test_grade_c_scenarios_carry_no_money_anywhere(report):
    """C 级证据在任何位置都不得出现金额（§11.3.1）。"""
    for card in report["cards"]:
        if card["evidence_grade"] == "C":
            roi = report["roi"][card["card_id"]]
            assert roi["amount"] is None
            assert roi["tiers"] == []
            assert roi["implementation_cost_low"] is None


def test_fragmented_work_never_enters_roi(report):
    for card in report["cards"]:
        if card["work_form"] == "fragmented":
            assert report["roi"][card["card_id"]]["direction_only"] is True


def test_zero_injection_escapes(report):
    # §14.1 安全合规门槛：0 违规
    assert report["security"]["injection_escaped"] == 0
    assert report["security"]["injection_attempts_detected"] >= 1


def test_guardian_approved_every_rendered_money_sentence(report):
    money_checks = [c for c in report["guardian_checks"] if "sentence" in c]
    assert money_checks
    assert all(c["approved"] for c in money_checks)


def test_expert_insights_pass_guardian_because_they_are_purely_directional(report):
    """专家判断按 C 级无引用送审，能通过恰恰说明它们不含任何量化断言。

    守护层的职责是拦『结论强度超出证据强度』。一条纯方向性判断没有数字，
    因此不超出 C 级证据能支撑的强度——通过是正确行为。
    """
    insight_checks = [c for c in report["guardian_checks"] if "insight_id" in c]
    assert insight_checks
    assert all(c["approved"] for c in insight_checks)


def test_guardian_would_block_an_insight_that_smuggled_in_a_number(report):
    """反向验证上一条：同样以 C 级无引用送审，一旦带数字就必须被拦。"""
    from aiea.guardrails import guardian_review
    from aiea.models import EvidenceGrade

    v = guardian_review(
        statement="真正的瓶颈在销售，预计每月可省 ¥12,000",
        evidence_grade=EvidenceGrade.C,
        has_citation=False,
    )
    assert v.approved is False
    assert v.reasons


def test_collaboration_gaps_are_escalated_not_guessed(report):
    # §14.1 协作维度：缺口是否主动升级而非硬猜
    assert report["gaps"]
    for g in report["gaps"]:
        assert g["impact"] and g["status"]


def test_conflicts_are_escalated_without_averaging(report):
    conflicted = [e for e in report["evidence"] if e["conflict"]]
    assert conflicted
    for e in conflicted:
        assert "均值" in e["conflict_note"] or "人工" in e["conflict_note"]


# ---------------- 黄金集与回归 ----------------
def test_golden_set_meets_recall_threshold():
    # §14.1 任务成功门槛：场景召回率 ≥ 80%
    result = run_golden_set()
    assert result["recall"] >= 0.80, result
    assert result["cases"] >= 3  # GEPA 最少 3 例即可工作


def test_golden_set_checks_insufficient_data_behaviour():
    result = run_golden_set()
    assert result["insufficient_data_correct"] is True


def test_failure_replay_directory_exists_for_regression():
    d = Path(__file__).resolve().parents[1] / "evals" / "failure_replays"
    assert d.exists()
    assert (d / "README.md").exists()


# ---------------- 交付物完整性 ----------------
def test_all_six_deliverable_parts_present(report):
    for key in ("parents", "matrix", "roi", "roadmap", "evidence", "insights"):
        assert report[key], f"交付物缺少 {key}"


def test_findings_markdown_separates_data_and_expert_sections(tmp_path):
    rep = run_seed_diagnosis(root=tmp_path)
    md = (tmp_path / "minghui" / "FINDINGS.md").read_text(encoding="utf-8")
    assert "第一部分：数据结论" in md
    assert "第二部分：基于经验的判断" in md
    assert md.index("第一部分") < md.index("第二部分")
    assert "非投资承诺" in md


def test_report_json_is_persisted_as_single_source_of_truth(tmp_path):
    run_seed_diagnosis(root=tmp_path)
    payload = json.loads((tmp_path / "minghui" / "REPORT.json").read_text(encoding="utf-8"))
    assert payload["scorecard"]["evidence_traceability"] == 1.0


def test_trace_is_written_with_gen_ai_fields(tmp_path):
    run_seed_diagnosis(root=tmp_path)
    traces = list((tmp_path / "minghui" / "trace").glob("*.jsonl"))
    assert traces
    lines = traces[0].read_text(encoding="utf-8").strip().splitlines()
    assert any("gen_ai.session.id" in l for l in lines)


def test_no_pii_or_secret_leaks_into_trace(tmp_path):
    run_seed_diagnosis(root=tmp_path)
    for path in (tmp_path / "minghui" / "trace").glob("*.jsonl"):
        text = path.read_text(encoding="utf-8")
        assert "sk-" not in text
        assert "13812345678" not in text
