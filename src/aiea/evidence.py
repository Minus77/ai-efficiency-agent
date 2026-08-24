"""证据分级、冲突裁决、作业形态判定（§3、§3.1、§3.2、§13.4、§17.1.1）。

纪律：
- 分级由证据类型决定，非模型自评（冻结口径）。
- 冲突偏差 > 30% 强制标注并转人工，禁止取均值掩盖分歧。
- 作业形态以时间戳分布为主判据；自述保留记录但不直接采信。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .config import FROZEN
from .models import DeliveryForm, EvidenceGrade, SourceType, WorkForm

# §3 取证路径 → 证据级别
_GRADE_BY_SOURCE: dict[SourceType, EvidenceGrade] = {
    SourceType.TIMESTAMP_EXPORT: EvidenceGrade.A,
    SourceType.TIME_LOG: EvidenceGrade.A,
    SourceType.SYSTEM_DATA: EvidenceGrade.A,
    SourceType.SUPPLEMENT_FORM: EvidenceGrade.B,
    SourceType.CROSS_CHECK: EvidenceGrade.B,
    SourceType.MEETING_NOTES: EvidenceGrade.C,
    SourceType.SELF_REPORT: EvidenceGrade.C,
    SourceType.BENCHMARK: EvidenceGrade.C,
}

# 同一时间窗判定：窗内间隔阈值与窗内合计门槛（§3.2）
_WINDOW_GAP_MINUTES = 15.0
_WINDOW_MIN_TOTAL_MINUTES = 30.0
_CONTINUOUS_MIN_MINUTES = 30.0


def grade_of(
    source_type: SourceType,
    *,
    cross_checked: bool = False,
    has_objective_trace: bool = False,
    for_quantification: bool = False,
) -> EvidenceGrade:
    """按证据类型定级。

    B 级的硬定义：多方交叉 **且至少一路有客观痕迹**。
    纯自述的多人互证只能到 C——多人可能只是印证了同一个组织性偏差（§3）。
    """
    base = _GRADE_BY_SOURCE[source_type]
    if source_type is SourceType.CROSS_CHECK:
        if cross_checked and has_objective_trace:
            return EvidenceGrade.B
        return EvidenceGrade.C
    # 纪要类文档永不因交叉而升级，且不得用于量化
    if source_type in (SourceType.MEETING_NOTES, SourceType.SELF_REPORT, SourceType.BENCHMARK):
        return EvidenceGrade.C
    if for_quantification and base is EvidenceGrade.C:
        return EvidenceGrade.C
    return base


@dataclass(frozen=True)
class Claim:
    """一路取证得到的声明。"""

    source_type: SourceType
    value: float
    origin: str
    note: str = ""


@dataclass
class Adjudication:
    chosen_value: float
    chosen_source: SourceType
    chosen_origin: str
    grade: EvidenceGrade
    conflict: bool = False
    requires_human: bool = False
    divergence: float = 0.0
    note: str = ""
    considered: list[str] = field(default_factory=list)


def adjudicate(claims: list[Claim]) -> Adjudication:
    """按固定裁决序择一，不取均值（§3.1）。"""
    if not claims:
        raise ValueError("无任何取证声明，无法裁决；应返回 insufficient_data")

    order: list[str] = list(FROZEN["adjudication_order"])  # type: ignore[arg-type]

    def rank(c: Claim) -> int:
        key = c.source_type.value
        if key in order:
            return order.index(key)
        if c.source_type is SourceType.SYSTEM_DATA:
            return order.index("timestamp_export")
        if c.source_type in (SourceType.MEETING_NOTES, SourceType.BENCHMARK):
            return order.index("self_report")
        return len(order)

    ranked = sorted(claims, key=rank)
    winner = ranked[0]

    values = [c.value for c in claims if c.value is not None]
    divergence = 0.0
    if len(values) >= 2:
        lo, hi = min(values), max(values)
        divergence = (hi - lo) / hi if hi else 0.0

    threshold = float(FROZEN["conflict_threshold"])  # type: ignore[arg-type]
    conflict = divergence > threshold

    note = (
        f"按固定裁决序采信 {winner.source_type.value}（{winner.origin}）。"
        f"多路偏差 {divergence:.0%}。"
    )
    if conflict:
        note += (
            f"偏差超过 {threshold:.0%} 阈值，已标注冲突并转人工判断——"
            "不取均值，均值会掩盖分歧。"
        )
    return Adjudication(
        chosen_value=winner.value,
        chosen_source=winner.source_type,
        chosen_origin=winner.origin,
        grade=grade_of(winner.source_type),
        conflict=conflict,
        requires_human=conflict,
        divergence=round(divergence, 4),
        note=note,
        considered=[f"{c.source_type.value}={c.value}（{c.origin}）" for c in claims],
    )


@dataclass
class WorkFormVerdict:
    work_form: WorkForm
    discount: float
    evidence_grade: EvidenceGrade
    windows: list[dict] = field(default_factory=list)
    requires_human: bool = False
    self_report_recorded: bool = False
    note: str = ""


def _cluster(stamps: list[datetime]) -> list[list[datetime]]:
    """按窗内间隔阈值聚簇。"""
    clusters: list[list[datetime]] = []
    current: list[datetime] = []
    for ts in sorted(stamps):
        if not current:
            current = [ts]
            continue
        gap = (ts - current[-1]).total_seconds() / 60.0
        if gap <= _WINDOW_GAP_MINUTES:
            current.append(ts)
        else:
            clusters.append(current)
            current = [ts]
    if current:
        clusters.append(current)
    return clusters


def judge_work_form(
    timestamps: list[str],
    *,
    minutes_per_run: float | None,
    self_reported_form: WorkForm | None = None,
) -> WorkFormVerdict:
    """用时间戳分布判定作业形态（§3.2）。

    主判据是聚集性，不是单次时长——批量作业最适合自动化，按单次时长判会系统性低估它。
    """
    if not timestamps:
        # 无客观痕迹：按真碎片处理，自述留作参考（C1）
        note = "无时间戳证据，按真碎片处理。"
        if self_reported_form is not None:
            note += f"客户自述为「{self_reported_form.value}」，信息已记录留作参考，不直接采信。"
        note += "若为批量作业，收益可上调，请提供导出以校正。"
        return WorkFormVerdict(
            work_form=WorkForm.FRAGMENTED,
            discount=0.0,
            evidence_grade=EvidenceGrade.C,
            self_report_recorded=self_reported_form is not None,
            note=note,
        )

    parsed = [datetime.fromisoformat(s) for s in timestamps]
    per_run = minutes_per_run or 0.0
    clusters = _cluster(parsed)
    windows = []
    for c in clusters:
        span = (c[-1] - c[0]).total_seconds() / 60.0
        total = max(span, len(c) * per_run)
        windows.append(
            {
                "start": c[0].isoformat(timespec="minutes"),
                "end": c[-1].isoformat(timespec="minutes"),
                "operations": len(c),
                "window_minutes": round(total, 1),
            }
        )

    biggest = max(windows, key=lambda w: w["window_minutes"])
    single_run_continuous = per_run >= _CONTINUOUS_MIN_MINUTES

    if single_run_continuous:
        form, discount = WorkForm.CONTINUOUS, 1.0
        note = f"单次连续 {per_run:.0f} 分钟 ≥ 30 分钟，判定连续作业，全额计入。"
    elif biggest["operations"] > 1 and biggest["window_minutes"] >= _WINDOW_MIN_TOTAL_MINUTES:
        form, discount = WorkForm.BATCH, 1.0
        note = (
            f"检测到 {biggest['operations']} 次操作聚集在 {biggest['start']}–{biggest['end']} 同一时间窗内，"
            f"窗内合计约 {biggest['window_minutes']:.0f} 分钟 ≥ 30 分钟，判定批量作业，全额计入。"
        )
    else:
        form, discount = WorkForm.FRAGMENTED, 0.0
        note = (
            "操作分散在全天不同时刻、无聚集性，判定真碎片，不计入 ROI，仅作定性描述。"
            "若实际为批量作业，收益可上调，请提供更完整导出以校正。"
        )

    requires_human = False
    if self_reported_form is not None and self_reported_form is not form:
        requires_human = True
        note += (
            f"客户自述为「{self_reported_form.value}」与时间戳判定「{form.value}」差异极大，"
            "已标记冲突转人工评判，不自动裁决。"
        )

    return WorkFormVerdict(
        work_form=form,
        discount=discount,
        evidence_grade=EvidenceGrade.A,
        windows=windows,
        requires_human=requires_human,
        self_report_recorded=self_reported_form is not None,
        note=note,
    )


def closure_rate(*, filled: int, total: int) -> float:
    """§12.2.2 证据空缺闭合率：采集的收敛判据，替代轮次上限。"""
    if total <= 0:
        return 0.0
    return round(filled / total, 4)


@dataclass
class ReachabilityProbe:
    grade: EvidenceGrade
    delivery_form: DeliveryForm
    accepted: bool
    note: str


def probe_material_reachability(
    *, has_records: bool, has_timestamps: bool, structured: bool = True
) -> ReachabilityProbe:
    """§17.1.1 受理前材料可得性探测：把 C 级风险从交付事故转为明码交付形态。"""
    if not structured:
        return ReachabilityProbe(
            grade=EvidenceGrade.C,
            delivery_form=DeliveryForm.LIGHT,
            accepted=False,
            note="导不出任何结构化数据 → 拒接。真实需求是先把流程数字化，而非识别 AI 提效点。",
        )
    if has_records and has_timestamps:
        return ReachabilityProbe(
            grade=EvidenceGrade.A,
            delivery_form=DeliveryForm.FULL,
            accepted=True,
            note="有单条记录 + 时间戳 → A 级可达，正常受理，走完整诊断。",
        )
    if has_records:
        return ReachabilityProbe(
            grade=EvidenceGrade.B,
            delivery_form=DeliveryForm.LIMITED,
            accepted=True,
            note="有单条记录、无时间戳 → B 级上限。受理并事前书面告知：可给频次，耗时需补数表，ROI 仅区间。",
        )
    return ReachabilityProbe(
        grade=EvidenceGrade.C,
        delivery_form=DeliveryForm.LIGHT,
        accepted=True,
        note="仅汇总数字、无明细 → C 级上限，仅接受轻量咨询交付，不出完整 ROI 报告。",
    )
