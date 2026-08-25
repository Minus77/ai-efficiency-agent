"""Task 3：场景推导 —— 从解析出的材料信号推出任务卡。

这是"工具"与"demo"的分界线：卡片必须由材料算出来，不能内置任何默认场景。

纪律：
- 有时间戳 → 用聚簇判定作业形态，A 级
- 无时间戳有明细 → B 级，且不得声称作业形态（没有痕迹就是没有）
- 仅汇总/纪要 → C 级，零金额、仅方向
- LLM 只负责"给这簇活动起个业务名"，所有数字仍由 evidence.py 算
- LLM 编造不存在的列名 → 丢弃该条
- 无 LLM 时确定性兜底仍能出卡（离线可跑）
"""
import json

import pytest

from aiea.derive import derive_scenarios
from aiea.intake import parse_bytes
from aiea.llm import LLMClient
from aiea.models import EvidenceGrade, WorkForm

# 40 条工单，聚集在同一上午时间窗内 → 批量作业
TICKETS = "\n".join(
    ["ticket_no,created_at,first_response_at,channel,category,handler"]
    + [
        f"WD{1000 + i},2026-03-{12 + i // 20:02d}T09:{(i * 3) % 55:02d}:00,"
        f"2026-03-{12 + i // 20:02d}T09:{(i * 3 + 2) % 58:02d}:00,微信,送货时间,王芳"
        for i in range(40)
    ]
)
# 有明细但无时间戳
ORDERS = "\n".join(["order_no,amount,sales_owner"] + [f"SC{i},{200 + i},赵强" for i in range(30)])
# 仅汇总
SUMMARY = "月份,咨询总量,平均时长\n2026-03,612,3.2\n2026-04,588,3.4"
NOTES = "# 会议纪要\n\n客服反馈微信咨询量涨得快，人手紧张。决定优化响应流程。"


def _parsed(*pairs):
    return [parse_bytes(c.encode("utf-8"), filename=f) for f, c in pairs]


class FakeLLM:
    """只用于命名的假 LLM。"""

    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def complete_json(self, messages, **kw):
        self.calls.append({"messages": messages, **kw})
        return self.payload


# ---------------------------- 证据等级与作业形态 ----------------------------
def test_timestamped_material_yields_grade_a_and_work_form():
    out = derive_scenarios(_parsed(("tickets.csv", TICKETS)))
    cards = out["cards"]
    assert cards, "有时间戳的材料必须能推出场景"
    top = cards[0]
    assert top["evidence_grade"] == EvidenceGrade.A.value
    assert top["work_form"] in (WorkForm.BATCH.value, WorkForm.CONTINUOUS.value, WorkForm.FRAGMENTED.value)
    assert top["monthly_minutes"] > 0
    assert top["evidence_refs"], "无证据引用的卡不得产出"


def test_batch_is_detected_from_clustered_timestamps():
    out = derive_scenarios(_parsed(("tickets.csv", TICKETS)))
    top = out["cards"][0]
    # 40 条集中在每天上午一个窗口内 → 批量作业，全额折现
    assert top["work_form"] == WorkForm.BATCH.value
    assert "聚集" in top["forensics_note"] or "时间窗" in top["forensics_note"]


def test_records_without_timestamps_cap_at_grade_b_and_no_work_form_claim():
    out = derive_scenarios(_parsed(("orders.csv", ORDERS)))
    cards = out["cards"]
    assert cards
    card = cards[0]
    assert card["evidence_grade"] == EvidenceGrade.B.value
    # 没有痕迹就不许声称作业形态；应落到真碎片（保守）并说明原因
    assert card["work_form"] == WorkForm.FRAGMENTED.value
    assert "无时间戳" in card["forensics_note"] or "无法验证" in card["forensics_note"]


def test_summary_only_material_produces_direction_only_card():
    out = derive_scenarios(_parsed(("summary.csv", SUMMARY)))
    cards = out["cards"]
    assert cards
    assert cards[0]["evidence_grade"] == EvidenceGrade.C.value
    assert cards[0]["monthly_minutes"] == 0


def test_meeting_notes_alone_never_produce_quantified_card():
    out = derive_scenarios(_parsed(("notes.md", NOTES)))
    for c in out["cards"]:
        assert c["evidence_grade"] == EvidenceGrade.C.value
        assert c["monthly_minutes"] == 0
    assert out["gaps"], "只有纪要时必须显式报缺口"


def test_no_materials_returns_structured_gap_not_crash():
    out = derive_scenarios([])
    assert out["cards"] == []
    assert out["gaps"]
    gap = out["gaps"][0]
    assert gap["material"] and gap["impact"]
    assert "上传" in gap["impact"] or "导出" in gap["impact"]


# ---------------------------- 证据台账 ----------------------------
def test_every_card_ref_exists_in_evidence_ledger():
    out = derive_scenarios(_parsed(("tickets.csv", TICKETS), ("orders.csv", ORDERS)))
    ids = {e["evidence_id"] for e in out["evidence"]}
    for c in out["cards"]:
        for ref in c["evidence_refs"]:
            assert ref in ids, f"{c['card_id']} 引用了不存在的证据 {ref}"


def test_evidence_records_sample_size_and_grade_reason():
    out = derive_scenarios(_parsed(("tickets.csv", TICKETS)))
    for e in out["evidence"]:
        assert e["grade_reason"]
        assert e["origin"]


def test_parents_group_children_and_totals_match():
    out = derive_scenarios(_parsed(("tickets.csv", TICKETS), ("orders.csv", ORDERS)))
    assert out["parents"]
    child_ids = {c["card_id"] for c in out["cards"]}
    for p in out["parents"]:
        assert p["child_ids"]
        for cid in p["child_ids"]:
            assert cid in child_ids
        total = sum(c["monthly_minutes"] for c in out["cards"] if c["card_id"] in p["child_ids"])
        assert abs(p["total_monthly_minutes"] - total) < 1.0


# ---------------------------- LLM 命名 ----------------------------
def test_llm_names_activities_but_numbers_stay_derived():
    llm = FakeLLM({"activities": [
        {"column": "category", "value": "送货时间",
         "scenario_name": "送货时间咨询人工答复", "operator": "客服专员",
         "business_outcome": "客户咨询得到回复", "status_quo": "逐条手工回复",
         "role": "客服专员"},
    ]})
    out = derive_scenarios(_parsed(("tickets.csv", TICKETS)), llm=llm)
    assert llm.calls, "应调用 LLM 做业务命名"
    named = [c for c in out["cards"] if "送货时间" in c["name"]]
    assert named, "应采用模型给的场景名"
    # 但数字仍来自时间戳推算，不是模型给的
    assert named[0]["monthly_minutes"] > 0
    assert named[0]["evidence_grade"] == EvidenceGrade.A.value


def test_llm_hallucinated_column_is_dropped():
    llm = FakeLLM({"activities": [
        {"column": "不存在的列", "value": "x", "scenario_name": "编造场景",
         "operator": "谁", "business_outcome": "无", "status_quo": "无", "role": "客服专员"},
    ]})
    out = derive_scenarios(_parsed(("tickets.csv", TICKETS)), llm=llm)
    assert not any("编造场景" == c["name"] for c in out["cards"])


def test_llm_failure_falls_back_to_deterministic_naming():
    class Boom:
        def complete_json(self, *a, **k):
            raise TimeoutError("down")

    out = derive_scenarios(_parsed(("tickets.csv", TICKETS)), llm=Boom())
    assert out["cards"], "LLM 挂掉不能导致推导失败"
    assert out["naming_source"] == "fallback"


def test_cost_breaker_propagates_not_silently_degraded():
    from aiea.llm import CostBreakerTripped

    class Breaker:
        def complete_json(self, *a, **k):
            raise CostBreakerTripped("挂起")

    with pytest.raises(CostBreakerTripped):
        derive_scenarios(_parsed(("tickets.csv", TICKETS)), llm=Breaker())


# ---------------------------- 可复现性 ----------------------------
def test_derivation_is_deterministic_without_llm():
    a = derive_scenarios(_parsed(("tickets.csv", TICKETS)))
    b = derive_scenarios(_parsed(("tickets.csv", TICKETS)))
    assert json.dumps(a["cards"], sort_keys=True) == json.dumps(b["cards"], sort_keys=True)


def test_changing_material_changes_conclusions():
    """核心断言：改材料，结论必须跟着变——否则就是硬编码。"""
    few = "\n".join(
        ["ticket_no,created_at,first_response_at,category"]
        + [f"WD{i},2026-03-12T09:{i:02d}:00,2026-03-12T09:{i + 2:02d}:00,送货时间" for i in range(5)]
    )
    small = derive_scenarios(_parsed(("tickets.csv", few)))
    big = derive_scenarios(_parsed(("tickets.csv", TICKETS)))
    assert big["cards"][0]["monthly_minutes"] != small["cards"][0]["monthly_minutes"]


# ---------------------------- 切分列的选择 ----------------------------
MULTI = "\n".join(
    ["id,created_at,channel,category,handler"]
    + [f"A{i},2026-03-12T09:{i % 60:02d}:00,微信,送货时间,王芳" for i in range(24)]
    + [f"B{i},2026-03-12T14:{i % 60:02d}:00,电话,开票问题,李静" for i in range(12)]
)


def test_split_prefers_activity_category_over_channel_or_person():
    """切分应按"做的是什么事"，而不是"从哪来"或"谁做的"——后两者不是活动划分。"""
    out = derive_scenarios(_parsed(("tickets.csv", MULTI)))
    values = {c["source_value"] for c in out["cards"]}
    assert {"送货时间", "开票问题"} <= values, values
    assert "微信" not in values and "王芳" not in values


def test_per_value_frequency_is_exact_not_evenly_split():
    """30:6 的不均分布不能被摊成 18:18。"""
    out = derive_scenarios(_parsed(("tickets.csv", MULTI)))
    by_val = {c["source_value"]: c for c in out["cards"] if c["source_value"]}
    assert "24" in by_val["送货时间"]["frequency_desc"]
    assert "12" in by_val["开票问题"]["frequency_desc"]
