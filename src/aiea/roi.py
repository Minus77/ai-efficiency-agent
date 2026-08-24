"""ROI 计算：纯函数，不许猜数（§6 取舍 2、§11.3）。

设计纪律：
- 无 IO、无 LLM、无随机、无默认值兜底。缺参即返回 invalid_params + next_action。
- 折现按作业形态（§3.2/§11.3.3），不按单次时长。
- C 级不给任何金额（§11.3.1）；B 级仅区间；A 级点估 + 区间。
- 依赖收益单列，汇总先去重再相加并展示差额（§11.3.4）。
"""

from __future__ import annotations

from typing import Any

from .config import FROZEN
from .models import EvidenceGrade, ROIResult, ROITier, ToolResult, WorkForm

# §11.3.1 三档口径的效率提升系数（中性/乐观档的额外增益）
_NEUTRAL_UPLIFT = 1.15
_OPTIMISTIC_UPLIFT = 1.35


def discount_factor(work_form: WorkForm) -> float:
    """§3.2 折现：连续与批量皆 100%，真碎片 0%。"""
    rules: dict[str, float] = dict(FROZEN["discount_rules"])  # type: ignore[arg-type]
    return float(rules[work_form.value])


def _tier(
    name: str,
    hours: float,
    cost_range: tuple[float, float],
    rate_range: tuple[float, float],
    uplift: float,
    *,
    with_point: bool,
) -> ROITier:
    low = hours * cost_range[0] * rate_range[0] * uplift
    high = hours * cost_range[1] * rate_range[1] * uplift
    point = round((low + high) / 2, 2) if with_point else None
    return ROITier(
        name=name,  # type: ignore[arg-type]
        monthly_saving_low=round(low, 2),
        monthly_saving_high=round(high, 2),
        point_estimate=point,
    )


def roi_estimate(
    *,
    card_id: str,
    monthly_minutes: float | None,
    work_form: WorkForm | None,
    evidence_grade: EvidenceGrade | None,
    hourly_cost_range: tuple[float, float] | None,
    automation_rate_range: tuple[float, float] | None,
    implementation_cost_range: tuple[float, float] | None,
    include_optimistic: bool = False,
    dependency_of: str | None = None,
    dependency_released_saving: float | None = None,
    assumptions: list[str] | None = None,
) -> ToolResult:
    """估算单张子场景卡的分级 ROI。

    缺任一必需基线即报结构化错误并指明去哪个工具取——工具层挡住的幻觉，胜过 prompt 叮嘱十遍。
    """
    if monthly_minutes is None:
        return ToolResult.invalid(
            "缺少 monthly_minutes（月度工时基线）",
            next_action="先调 metric_probe(activity_id=...) 取频次与单次耗时；确无数据则用 insufficient_data 标记缺口",
        )
    if work_form is None:
        return ToolResult.invalid(
            "缺少 work_form（作业形态）",
            next_action="先调 document_forensics(...) 用时间戳分布判定连续/批量/真碎片，自述不可单独采信",
        )
    if evidence_grade is None:
        return ToolResult.invalid(
            "缺少 evidence_grade（证据等级）",
            next_action="先调 evidence.grade_of(...) 依据证据类型定级；分级口径为冻结区，不可由模型自评",
        )
    if hourly_cost_range is None:
        return ToolResult.invalid(
            "缺少 hourly_cost_range（岗位小时综合成本区间）",
            next_action="先调 benchmark_lookup(role=...) 取公开薪酬基准区间；基准仅作假设写入假设清单，不填 ROI 空缺",
        )
    if automation_rate_range is None:
        return ToolResult.invalid(
            "缺少 automation_rate_range（自动化率区间）",
            next_action="先调 capability_match(...) 依据能力库判定可替代比例",
        )
    if implementation_cost_range is None:
        return ToolResult.invalid(
            "缺少 implementation_cost_range（实施成本区间）",
            next_action="先调 benchmark_lookup(capability=...) 取订阅费/集成人力/培训/维护的市场区间（§11.3.5）",
        )

    factor = discount_factor(work_form)
    discounted_minutes = monthly_minutes * factor
    hours = discounted_minutes / 60.0
    trace = [
        f"月度工时 {monthly_minutes:.0f} 分钟 × 折现系数 {factor:.0%}（作业形态={work_form.value}）= {discounted_minutes:.0f} 分钟",
        f"折现后工时 {hours:.1f} 小时/月",
    ]

    base = ROIResult(
        card_id=card_id,
        evidence_grade=evidence_grade,
        work_form=work_form,
        discount_factor=factor,
        discounted_monthly_minutes=round(discounted_minutes, 2),
        implementation_cost_low=implementation_cost_range[0],
        implementation_cost_high=implementation_cost_range[1],
        dependency_released_saving=dependency_released_saving,
        assumptions=assumptions or [],
        calculation_trace=trace,
    )

    # 真碎片：不进 ROI，仅定性（§3.2）
    # 不进 ROI 意味着**任何金额字段都不得残留**——只给实施成本却不给收益会误导读者。
    if factor == 0.0:
        base.direction_only = True
        base.amount = None
        base.tiers = []
        base.implementation_cost_low = None
        base.implementation_cost_high = None
        base.calculation_trace.append("真碎片作业不计入 ROI，仅作定性描述；若为批量作业须用时间戳重新判定")
        return ToolResult.success({"roi": base}, note="真碎片：不给金额，仅定性")

    # C 级：不给任何数字，只给方向（§11.3.1）
    if evidence_grade is EvidenceGrade.C:
        base.direction_only = True
        base.amount = None
        base.tiers = []
        base.implementation_cost_low = None
        base.implementation_cost_high = None
        base.calculation_trace.append("C 级证据：仅方向性判断，不输出金额，需先补客观痕迹")
        return ToolResult.success({"roi": base}, note="C 级：仅方向，无金额")

    with_point = evidence_grade is EvidenceGrade.A
    tiers = [
        _tier("保守", hours, hourly_cost_range, automation_rate_range, 1.0, with_point=with_point),
        _tier("中性", hours, hourly_cost_range, automation_rate_range, _NEUTRAL_UPLIFT, with_point=with_point),
    ]
    if include_optimistic and evidence_grade is EvidenceGrade.A:
        tiers.append(
            _tier("乐观", hours, hourly_cost_range, automation_rate_range, _OPTIMISTIC_UPLIFT, with_point=with_point)
        )
    base.tiers = tiers

    conservative, neutral = tiers[0], tiers[1]
    trace.append(
        f"保守档月度收益 = {hours:.1f}h × ¥{hourly_cost_range[0]:.0f}–{hourly_cost_range[1]:.0f}/h × "
        f"自动化率 {automation_rate_range[0]:.0%}–{automation_rate_range[1]:.0%} "
        f"= ¥{conservative.monthly_saving_low:.0f}–{conservative.monthly_saving_high:.0f}"
    )

    # §11.3.5 净收益与回本周期
    base.net_monthly_low = round((conservative.monthly_saving_low or 0.0), 2)
    base.net_monthly_high = round((neutral.monthly_saving_high or 0.0), 2)
    if base.net_monthly_low > 0:
        base.payback_months_conservative = round(implementation_cost_range[1] / base.net_monthly_low, 1)
    if (neutral.monthly_saving_low or 0) > 0:
        base.payback_months_neutral = round(implementation_cost_range[0] / (neutral.monthly_saving_low or 1), 1)
    trace.append(
        f"回本周期（保守，取实施成本上限）= ¥{implementation_cost_range[1]:.0f} / "
        f"¥{base.net_monthly_low:.0f}/月 ≈ {base.payback_months_conservative} 个月"
    )

    base.amount = conservative.point_estimate if with_point else None
    if dependency_of:
        base.calculation_trace.append(
            f"本场景依赖 {dependency_of}：只计入独立可实现部分；依赖释放收益单列，不并入自身收益"
        )
    return ToolResult.success({"roi": base}, note=f"{evidence_grade.value} 级：" + ("点估 + 区间" if with_point else "仅区间"))


def aggregate_dedup(rois: list[ROIResult]) -> dict[str, Any]:
    """汇总：先去重再相加，并展示去重前后差额（§11.3.4）。

    naive_sum 是客户自己会算出的那个更大数字；deduped_sum 是报告采用值。
    差额必须展示，否则客户自行加总会得出更大数字并质疑报告。
    """
    naive = 0.0
    deduped = 0.0
    dependency_total = 0.0
    for roi in rois:
        if roi.direction_only or not roi.tiers:
            continue
        conservative = next((t for t in roi.tiers if t.name == "保守"), None)
        if conservative is None:
            continue
        own = conservative.point_estimate or conservative.monthly_saving_low or 0.0
        released = roi.dependency_released_saving or 0.0
        naive += own + released  # 分别计算会重复计上同一份收益
        deduped += own
        dependency_total += released
    return {
        "naive_sum": round(naive, 2),
        "deduped_sum": round(deduped, 2),
        "delta": round(naive - deduped, 2),
        "dependency_released_total": round(dependency_total, 2),
        "note": "依赖释放收益单列，不并入任一场景自身收益；报告展示去重前后差额以防客户自行加总",
    }
