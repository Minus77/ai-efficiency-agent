"""Task 1 契约测试：枚举、冻结区、ToolResult 一等公民返回值。"""
import pytest

from aiea.config import FROZEN, Settings
from aiea.models import (
    Evidence,
    EvidenceGrade,
    ResultCode,
    SourceType,
    TaskCard,
    ToolResult,
    WorkForm,
)


def test_evidence_grade_has_three_levels():
    assert [g.value for g in EvidenceGrade] == ["A", "B", "C"]


def test_frozen_zone_covers_six_invariants():
    # §9.7 冻结边界：这些口径禁止自演进
    for key in (
        "evidence_grading",
        "roi_formula",
        "insufficient_data_semantics",
        "readonly_boundary",
        "max_steps",
        "cost_breaker",
    ):
        assert key in FROZEN, key


def test_frozen_zone_is_immutable_at_runtime():
    with pytest.raises(TypeError):
        FROZEN["max_steps"] = 999  # type: ignore[index]


def test_insufficient_data_is_a_successful_first_class_return():
    # §6 取舍 1：'没有数据' 是一等公民返回值，不是异常
    r = ToolResult.insufficient(
        reason="工单导出未覆盖该活动",
        next_action="向客户索取 3 月工单导出；确无数据则标记缺口",
    )
    assert r.ok is True
    assert r.code is ResultCode.INSUFFICIENT_DATA
    assert r.data == {}
    assert "工单导出" in r.next_action


def test_invalid_params_carries_executable_next_action():
    # §6：错误不是 'Error 422'，而是可执行的下一步
    r = ToolResult.invalid("缺少 baseline_minutes", next_action="先调 metric_probe(activity_id=...)")
    assert r.ok is False
    assert r.code is ResultCode.INVALID_PARAMS
    assert r.next_action.startswith("先调 metric_probe")


def test_workform_and_sourcetype_enums():
    assert {w.value for w in WorkForm} == {"continuous", "batch", "fragmented"}
    assert SourceType.TIMESTAMP_EXPORT.value == "timestamp_export"
    assert SourceType.MEETING_NOTES.value == "meeting_notes"


def test_taskcard_requires_evidence_refs():
    # §11.1 约束：无证据引用的场景卡不得进入清单
    with pytest.raises(ValueError):
        TaskCard(
            card_id="s-01",
            name="客服咨询转工单",
            operator="客服专员",
            systems=["微信", "工单系统"],
            status_quo="手工把微信咨询转录进工单",
            monthly_minutes=1200.0,
            evidence_grade=EvidenceGrade.A,
            work_form=WorkForm.BATCH,
            evidence_refs=[],
        )


def test_evidence_records_asof_and_reason():
    e = Evidence(
        evidence_id="e01",
        source_type=SourceType.TIMESTAMP_EXPORT,
        origin="客户提供的工单系统导出 tickets.csv",
        obtained_at="2026-08-20",
        as_of="2026-08-20",
        grade=EvidenceGrade.A,
        grade_reason="含单条记录与创建/首响时间戳",
        sample_size=612,
    )
    assert e.grade is EvidenceGrade.A
    assert e.sample_size == 612


def test_settings_point_at_configured_gateway():
    s = Settings()
    assert s.base_url == "https://api.wenwen-ai.com/v1"
    assert s.primary_model == "claude-sonnet-4-5"
    # §14.3 judge 不得与主 Agent 同源
    assert s.judge_model != s.primary_model
    assert s.max_steps == FROZEN["max_steps"]
