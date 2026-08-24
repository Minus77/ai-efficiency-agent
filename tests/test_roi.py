"""Task 2：roi_estimate 必须是纯函数，缺参报错不猜数（§6 取舍 2、§11.3）。"""
import pytest

from aiea.models import EvidenceGrade, ResultCode, WorkForm
from aiea.roi import aggregate_dedup, discount_factor, roi_estimate


BASE = dict(
    card_id="s-01",
    monthly_minutes=1200.0,
    work_form=WorkForm.BATCH,
    evidence_grade=EvidenceGrade.A,
    hourly_cost_range=(38.0, 52.0),
    automation_rate_range=(0.5, 0.7),
    implementation_cost_range=(12000.0, 20000.0),
)


def test_missing_hourly_cost_returns_invalid_params_with_next_action():
    r = roi_estimate(**{**BASE, "hourly_cost_range": None})
    assert r.ok is False
    assert r.code is ResultCode.INVALID_PARAMS
    assert "metric_probe" in r.next_action or "benchmark" in r.next_action


def test_fragmented_work_is_excluded_from_roi():
    # §3.2 真碎片不进 ROI，仅定性描述
    assert discount_factor(WorkForm.FRAGMENTED) == 0.0
    r = roi_estimate(**{**BASE, "work_form": WorkForm.FRAGMENTED})
    assert r.ok is True
    roi = r.data["roi"]
    assert roi.direction_only is True
    assert roi.amount is None


def test_batch_work_is_fully_discounted_like_continuous():
    # §3.2 批量作业全额计入——原按单次时长的规则会系统性低估它
    assert discount_factor(WorkForm.BATCH) == 1.0 == discount_factor(WorkForm.CONTINUOUS)


def test_grade_a_gets_point_estimate_and_range():
    roi = roi_estimate(**BASE).data["roi"]
    conservative = next(t for t in roi.tiers if t.name == "保守")
    assert conservative.point_estimate is not None
    assert conservative.monthly_saving_low < conservative.monthly_saving_high
    assert roi.payback_months_conservative is not None
    assert roi.calculation_trace, "§13.4 必须强制展示计算过程"


def test_grade_b_gives_range_only_no_point_estimate():
    roi = roi_estimate(**{**BASE, "evidence_grade": EvidenceGrade.B}).data["roi"]
    for tier in roi.tiers:
        assert tier.point_estimate is None
    assert {t.name for t in roi.tiers} == {"保守", "中性"}, "乐观档仅 A 级且客户要求时给"


def test_grade_c_returns_no_amount_at_all():
    # §11.3.1 C 级不给数字，只给方向
    roi = roi_estimate(**{**BASE, "evidence_grade": EvidenceGrade.C}).data["roi"]
    assert roi.amount is None
    assert roi.direction_only is True
    assert roi.tiers == []


def test_optimistic_tier_only_when_requested_and_grade_a():
    roi = roi_estimate(**BASE, include_optimistic=True).data["roi"]
    assert "乐观" in {t.name for t in roi.tiers}
    roi_b = roi_estimate(**{**BASE, "evidence_grade": EvidenceGrade.B}, include_optimistic=True).data["roi"]
    assert "乐观" not in {t.name for t in roi_b.tiers}


def test_is_pure_function_same_input_same_output():
    a = roi_estimate(**BASE).data["roi"].model_dump()
    b = roi_estimate(**BASE).data["roi"].model_dump()
    assert a == b


def test_dependency_saving_is_listed_separately_not_merged():
    # §11.3.4 依赖收益单列，不并入任一场景自身收益
    roi = roi_estimate(**BASE, dependency_released_saving=800.0).data["roi"]
    conservative = next(t for t in roi.tiers if t.name == "保守")
    assert roi.dependency_released_saving == 800.0
    assert conservative.point_estimate is not None
    assert conservative.point_estimate < 800.0 + conservative.point_estimate


def test_aggregate_dedup_shows_delta_before_and_after():
    # §11.3.4：汇总先去重再相加，并展示去重前后差额
    rois = [
        roi_estimate(**BASE).data["roi"],
        roi_estimate(**{**BASE, "card_id": "s-02"}, dependency_of="s-01", dependency_released_saving=800.0).data["roi"],
    ]
    agg = aggregate_dedup(rois)
    assert agg["naive_sum"] > agg["deduped_sum"]
    assert agg["delta"] == pytest.approx(agg["naive_sum"] - agg["deduped_sum"])
    assert agg["dependency_released_total"] == 800.0
