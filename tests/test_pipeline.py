"""Task 10：S0–S5 Plan-and-Execute 编排、阶段门禁、长周期恢复（§2、§5、§13.1、§13.2）。"""
import pytest

from aiea.knowledge import KnowledgeBase
from aiea.models import EvidenceGrade, Stage, WorkForm
from aiea.pipeline import Diagnosis, StepLimitExceeded
from aiea.telemetry import Tracer
from aiea.workspace import Workspace


@pytest.fixture
def diag(tmp_path):
    ws = Workspace(tenant="minghui", root=tmp_path)
    return Diagnosis(
        tenant="minghui",
        workspace=ws,
        kb=KnowledgeBase.load_seed(),
        tracer=Tracer(session_id="s1", tenant="minghui", out_dir=tmp_path / "trace"),
    )


def test_stages_are_fixed_s0_to_s5(diag):
    assert [s.name for s in diag.stages] == ["S0", "S1", "S2", "S3", "S4", "S5"]


def test_s4_is_blocked_for_grade_c_scenario(diag):
    r = diag.enter_stage_for_card(Stage.S4, evidence_grade=EvidenceGrade.C, quantifiable=True)
    assert r.ok is False
    assert "C 级" in r.note or "C 级" in r.next_action


def test_s4_is_blocked_for_fragmented_work(diag):
    r = diag.enter_stage_for_card(Stage.S4, evidence_grade=EvidenceGrade.A, quantifiable=False)
    assert r.ok is False


def test_max_steps_is_per_stage_not_per_diagnosis(diag):
    for _ in range(20):
        diag.step(Stage.S2, "解析一批材料")
    with pytest.raises(StepLimitExceeded):
        diag.step(Stage.S2, "再解析一批")
    # 换阶段后步数重置——MAX_STEPS 按阶段计（§5 长周期会话模型）
    diag.step(Stage.S3, "打分")
    assert diag.step_count(Stage.S3) == 1


def test_state_is_rebuilt_from_disk_not_from_context(diag, tmp_path):
    diag.workspace.write_json("scope.json", {"client_name": "明辉家居建材", "as_of": "2026-08-20"})
    diag.workspace.write_json(
        "task-cards/s-01.json",
        {"card_id": "s-01", "name": "客服咨询转工单", "evidence_grade": "A", "evidence_refs": ["e01"]},
    )
    fresh = Diagnosis(tenant="minghui", workspace=Workspace(tenant="minghui", root=tmp_path), kb=KnowledgeBase.load_seed())
    state = fresh.resume()
    assert state["scope"]["client_name"] == "明辉家居建材"
    assert len(state["cards"]) == 1


def test_context_budget_contract_triggers_soft_compaction(diag):
    budget = diag.context_budget(used_ratio=0.72)
    assert budget["compaction"] == "soft"
    hard = diag.context_budget(used_ratio=0.93)
    assert hard["compaction"] == "hard"
    assert hard["action"].startswith("强制落盘")


def test_compression_must_declare_loss_budget(diag):
    out = diag.compress(["材料摘要1", "材料摘要2", "材料摘要3"], keep=2)
    assert out["dropped_count"] == 1
    assert out["loss_declaration"], "压缩必须显式声明丢弃内容（§5 Compress）"


def test_cards_are_batched_by_business_flow_not_department(diag):
    cards = [
        {"card_id": "s-01", "business_flow": "客户咨询", "department": "客服"},
        {"card_id": "s-02", "business_flow": "客户咨询", "department": "销售"},
        {"card_id": "s-03", "business_flow": "月度对账", "department": "财务"},
    ]
    batches = diag.batch_cards(cards, batch_size=8)
    assert len(batches) == 2
    assert {c["card_id"] for c in batches[0]} == {"s-01", "s-02"}


def test_counter_review_rounds_are_capped(diag):
    assert diag.counter_review_max_rounds == 3


def test_quadrant_assignment_keeps_do_not_bucket(diag):
    q = diag.quadrant(benefit=200.0, difficulty=4.5, benefit_threshold=1000.0, difficulty_threshold=3.0)
    assert q.value == "不做"
    q2 = diag.quadrant(benefit=8000.0, difficulty=2.0, benefit_threshold=1000.0, difficulty_threshold=3.0)
    assert q2.value == "先做"


def test_roadmap_has_three_batches_with_exit_conditions(diag):
    cards = [
        {"card_id": "s-01", "name": "A", "quadrant": "先做", "monthly_saving": 9000},
        {"card_id": "s-02", "name": "B", "quadrant": "先做", "monthly_saving": 6000},
        {"card_id": "s-03", "name": "C", "quadrant": "规划", "monthly_saving": 4000},
    ]
    roadmap = diag.roadmap(cards)
    assert [b["window"] for b in roadmap] == ["第 1–30 天", "第 31–60 天", "第 61–90 天"]
    for b in roadmap:
        assert b["acceptance"], "每批次必须写明验收标准"
        assert b["exit_condition"], "失败退出条件常被省略，但它决定客户会不会在坑里越投越多"


def test_traceability_rate_requires_every_quantified_claim_cited(diag):
    cards = [
        {"card_id": "s-01", "monthly_minutes": 1200, "evidence_refs": ["e01"]},
        {"card_id": "s-02", "monthly_minutes": 800, "evidence_refs": []},
    ]
    rate = diag.traceability_rate(cards)
    assert rate == 0.5
