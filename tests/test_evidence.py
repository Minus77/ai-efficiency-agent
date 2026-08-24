"""Task 3：证据分级与冲突裁决（§3、§3.1、§3.2、§13.4）。"""
import pytest

from aiea.evidence import (
    Claim,
    adjudicate,
    grade_of,
    judge_work_form,
    closure_rate,
    probe_material_reachability,
)
from aiea.models import EvidenceGrade, SourceType, WorkForm


def test_timestamp_export_is_grade_a():
    assert grade_of(SourceType.TIMESTAMP_EXPORT) is EvidenceGrade.A
    assert grade_of(SourceType.TIME_LOG) is EvidenceGrade.A
    assert grade_of(SourceType.SYSTEM_DATA) is EvidenceGrade.A


def test_meeting_notes_are_grade_c_and_never_quantifiable():
    assert grade_of(SourceType.MEETING_NOTES) is EvidenceGrade.C
    # §3 R5：纪要类文档仅用于识别痛点，不得用于量化
    assert grade_of(SourceType.MEETING_NOTES, for_quantification=True) is EvidenceGrade.C


def test_pure_self_report_cross_check_stays_grade_c():
    # §3 关键约束：B 级要求至少一路有客观痕迹；纯自述的多人互证只到 C
    g = grade_of(SourceType.CROSS_CHECK, cross_checked=True, has_objective_trace=False)
    assert g is EvidenceGrade.C


def test_cross_check_with_objective_trace_is_grade_b():
    g = grade_of(SourceType.CROSS_CHECK, cross_checked=True, has_objective_trace=True)
    assert g is EvidenceGrade.B


def test_supplement_form_is_grade_b():
    assert grade_of(SourceType.SUPPLEMENT_FORM) is EvidenceGrade.B


def test_adjudicate_prefers_timestamp_over_self_report():
    # §3.1 裁决序固定，不由模型自由决定
    result = adjudicate([
        Claim(source_type=SourceType.SELF_REPORT, value=60.0, origin="客服主管口述"),
        Claim(source_type=SourceType.TIMESTAMP_EXPORT, value=180.0, origin="tickets.csv"),
    ])
    assert result.chosen_value == 180.0
    assert result.chosen_source is SourceType.TIMESTAMP_EXPORT


def test_large_divergence_flags_conflict_and_forbids_mean():
    result = adjudicate([
        Claim(source_type=SourceType.SELF_REPORT, value=60.0, origin="老板自述"),
        Claim(source_type=SourceType.TIMESTAMP_EXPORT, value=180.0, origin="导出记录"),
    ])
    assert result.conflict is True
    assert result.requires_human is True
    # 禁止取均值掩盖分歧
    assert result.chosen_value != pytest.approx((60.0 + 180.0) / 2)
    assert "均值" in result.note or "不取均值" in result.note


def test_small_divergence_is_not_a_conflict():
    result = adjudicate([
        Claim(source_type=SourceType.SUPPLEMENT_FORM, value=100.0, origin="补数表"),
        Claim(source_type=SourceType.TIMESTAMP_EXPORT, value=110.0, origin="导出"),
    ])
    assert result.conflict is False
    assert result.requires_human is False


def test_batch_detected_when_operations_cluster_in_one_window():
    # §3.2 十次录入一口气做完 = 批量作业，全额计入
    stamps = [f"2026-03-12T09:{m:02d}:00" for m in (0, 4, 8, 13, 17, 22, 26, 31, 35, 40)]
    verdict = judge_work_form(stamps, minutes_per_run=4.0)
    assert verdict.work_form is WorkForm.BATCH
    assert verdict.discount == 1.0
    assert verdict.evidence_grade is EvidenceGrade.A


def test_fragmented_when_spread_across_the_day():
    stamps = [f"2026-03-12T{h:02d}:05:00" for h in (9, 11, 13, 15, 17)]
    verdict = judge_work_form(stamps, minutes_per_run=3.0)
    assert verdict.work_form is WorkForm.FRAGMENTED
    assert verdict.discount == 0.0
    assert "可上调" in verdict.note  # 把不确定性显式留给客户校正


def test_self_report_alone_cannot_prove_batch():
    # C1：自述保留但不直接采信
    verdict = judge_work_form([], minutes_per_run=None, self_reported_form=WorkForm.BATCH)
    assert verdict.work_form is WorkForm.FRAGMENTED
    assert verdict.evidence_grade is EvidenceGrade.C
    assert verdict.self_report_recorded is True


def test_self_report_conflicting_with_timestamps_escalates_to_human():
    stamps = [f"2026-03-12T{h:02d}:05:00" for h in (9, 11, 13, 15, 17)]
    verdict = judge_work_form(stamps, minutes_per_run=3.0, self_reported_form=WorkForm.BATCH)
    assert verdict.requires_human is True


def test_closure_rate_drives_collection_stop():
    # §12.2.2 用证据空缺闭合率收敛，不用轮次
    assert closure_rate(filled=8, total=10) == 0.8


def test_material_probe_maps_sample_to_delivery_form():
    # §17.1.1 受理前探测：能导出到什么粒度决定交付形态
    assert probe_material_reachability(has_records=True, has_timestamps=True).grade is EvidenceGrade.A
    assert probe_material_reachability(has_records=True, has_timestamps=False).grade is EvidenceGrade.B
    summary_only = probe_material_reachability(has_records=False, has_timestamps=False)
    assert summary_only.grade is EvidenceGrade.C
    assert summary_only.delivery_form.value == "轻量咨询"
    rejected = probe_material_reachability(has_records=False, has_timestamps=False, structured=False)
    assert rejected.accepted is False
