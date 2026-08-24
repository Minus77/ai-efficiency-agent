"""全部数据契约。

依据架构设计 §6（工具返回）、§11.1（场景卡字段）、§11.5（证据台账）、§13.4（置信度分级）。
所有模型只做校验，不做计算——计算在 roi.py / evidence.py / feasibility.py。
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator


# ---------------------------------------------------------------------------
# 枚举
# ---------------------------------------------------------------------------
class EvidenceGrade(str, Enum):
    """§13.4 置信度分级由证据类型决定，非模型自评。"""

    A = "A"
    B = "B"
    C = "C"


class WorkForm(str, Enum):
    """§3.2 作业形态，决定折现。"""

    CONTINUOUS = "continuous"  # 单次连续 ≥30 分钟
    BATCH = "batch"  # 同一时间窗内聚集，窗内合计 ≥30 分钟
    FRAGMENTED = "fragmented"  # 全天分散无聚集 → 不进 ROI


class SourceType(str, Enum):
    """§3 三路并行取证的证据来源类型。"""

    TIMESTAMP_EXPORT = "timestamp_export"  # R1 唯一主力，A 级
    TIME_LOG = "time_log"  # R4 工时记录，A 级
    SUPPLEMENT_FORM = "supplement_form"  # R2 补数表，B 级
    CROSS_CHECK = "cross_check"  # R3 多方交叉，B 级（须至少一路有客观痕迹）
    SYSTEM_DATA = "system_data"  # L1/L2 只读接口，A 级
    MEETING_NOTES = "meeting_notes"  # R5 纪要，C 级，不得用于量化
    SELF_REPORT = "self_report"  # 单方自述，C 级
    BENCHMARK = "benchmark"  # 行业基准，仅横向对照（§11.3.6）


class ResultCode(str, Enum):
    OK = "ok"
    INSUFFICIENT_DATA = "insufficient_data"  # §6 一等公民
    NO_GROUNDING = "no_grounding"  # §8.3 RAG 版的 insufficient_data
    INVALID_PARAMS = "invalid_params"
    DENIED = "denied"  # 护栏拒绝（越权 / 跨 tenant / 只读断言）


class Stage(str, Enum):
    S0 = "S0_立项与口径"
    S1 = "S1_业务地图采集"
    S2 = "S2_任务级分解"
    S3 = "S3_AI适配性打分"
    S4 = "S4_ROI与可行性"
    S5 = "S5_反评审与排序"


class InterventionMode(str, Enum):
    REPLACE = "替代"
    ASSIST = "辅助"
    AUGMENT = "增强"


class Quadrant(str, Enum):
    DO_FIRST = "先做"
    PLAN = "规划"
    OPPORTUNISTIC = "顺手做"
    DO_NOT = "不做"


class DeliveryForm(str, Enum):
    """§17.1.2 分级交付形态。"""

    FULL = "完整诊断"
    LIMITED = "限定诊断"
    LIGHT = "轻量咨询"


# ---------------------------------------------------------------------------
# 工具统一返回（§6）
# ---------------------------------------------------------------------------
class ToolResult(BaseModel):
    """结构化可执行的工具返回。

    错误一律带 next_action：不是 `Error 422`，而是"缺少 baseline_minutes；先调 metric_probe(...)"。
    """

    ok: bool = True
    code: ResultCode = ResultCode.OK
    data: dict[str, Any] = Field(default_factory=dict)
    source: list[str] = Field(default_factory=list)
    sample_size: int | None = None
    next_action: str = ""
    note: str = ""
    response_format: Literal["concise", "detailed"] = "concise"

    @classmethod
    def success(
        cls,
        data: dict[str, Any] | None = None,
        *,
        source: list[str] | None = None,
        sample_size: int | None = None,
        note: str = "",
        response_format: Literal["concise", "detailed"] = "concise",
    ) -> ToolResult:
        return cls(
            ok=True,
            code=ResultCode.OK,
            data=data or {},
            source=source or [],
            sample_size=sample_size,
            note=note,
            response_format=response_format,
        )

    @classmethod
    def insufficient(cls, reason: str, *, next_action: str, source: list[str] | None = None) -> ToolResult:
        """取不到数据。ok=True——这是正常结论，不是故障（§6 取舍 1）。"""
        return cls(
            ok=True,
            code=ResultCode.INSUFFICIENT_DATA,
            data={},
            source=source or [],
            next_action=next_action,
            note=reason,
        )

    @classmethod
    def no_grounding(cls, reason: str, *, next_action: str) -> ToolResult:
        """检索无接地。绝不许用训练知识补（§8.3）。"""
        return cls(ok=True, code=ResultCode.NO_GROUNDING, data={}, next_action=next_action, note=reason)

    @classmethod
    def invalid(cls, reason: str, *, next_action: str) -> ToolResult:
        return cls(ok=False, code=ResultCode.INVALID_PARAMS, data={}, next_action=next_action, note=reason)

    @classmethod
    def denied(cls, reason: str, *, next_action: str = "该请求超出只读边界或 tenant 范围，已拒绝") -> ToolResult:
        return cls(ok=False, code=ResultCode.DENIED, data={}, next_action=next_action, note=reason)


# ---------------------------------------------------------------------------
# 证据台账（§11.5）
# ---------------------------------------------------------------------------
class Evidence(BaseModel):
    evidence_id: str
    source_type: SourceType
    origin: str  # 来源与获取方式
    obtained_at: str
    as_of: str
    grade: EvidenceGrade
    grade_reason: str  # 判定理由——台账必须可复议
    sample_size: int | None = None
    supports: list[str] = Field(default_factory=list)  # 支撑了哪些场景卡的哪个字段
    conflict: bool = False
    conflict_note: str = ""
    stale: bool = False


# ---------------------------------------------------------------------------
# 场景卡（§11.1）
# ---------------------------------------------------------------------------
class TaskCard(BaseModel):
    """子场景：一次连续的人工操作序列。"""

    card_id: str
    parent_id: str = ""
    name: str  # 动词+对象
    operator: str
    systems: list[str]
    status_quo: str
    frequency_desc: str = ""
    minutes_per_run: float | None = None
    monthly_minutes: float
    evidence_grade: EvidenceGrade
    work_form: WorkForm
    benefit_composition: str = "单点"  # A4 强制字段：单点 | 多部门累加（列明部门数）
    departments_merged: int = 1
    intervention: InterventionMode = InterventionMode.ASSIST
    expected_effect: list[str] = Field(default_factory=list)  # 省时 | 提质 | 增量
    dependency: str = "独立"  # 独立 | 依赖 <子场景ID>
    landing_dependency: str = ""
    evidence_refs: list[str]
    conflict: bool = False
    conflict_note: str = ""
    requires_human: bool = False
    quantifiable: bool = True  # 真碎片 / C 级 → 仅定性

    @field_validator("evidence_refs")
    @classmethod
    def _must_cite_evidence(cls, v: list[str]) -> list[str]:
        # §11.1 约束：无证据引用的场景卡不得进入清单（taskcard_upsert schema 校验强制）
        if not v:
            raise ValueError("场景卡缺少证据引用，拒写；先调 document_forensics / metric_probe 取证")
        return v

    @model_validator(mode="after")
    def _fragmented_is_not_quantifiable(self) -> TaskCard:
        # §3.2 真碎片不进 ROI，仅定性描述
        if self.work_form is WorkForm.FRAGMENTED:
            object.__setattr__(self, "quantifiable", False)
        return self


class ParentScenario(BaseModel):
    """父场景：业务结果（老板视角）。"""

    parent_id: str
    business_outcome: str
    business_flow: str
    why_painful: str
    child_ids: list[str] = Field(default_factory=list)
    total_monthly_minutes: float = 0.0
    is_alternate: bool = False  # A2：优先级靠后者标注"备选，未做深度取证"


# ---------------------------------------------------------------------------
# ROI（§11.3）
# ---------------------------------------------------------------------------
class ROITier(BaseModel):
    name: Literal["保守", "中性", "乐观"]
    monthly_saving_low: float | None = None
    monthly_saving_high: float | None = None
    point_estimate: float | None = None  # 仅 A 级给点估


class ROIResult(BaseModel):
    card_id: str
    evidence_grade: EvidenceGrade
    work_form: WorkForm
    discount_factor: float
    discounted_monthly_minutes: float
    tiers: list[ROITier] = Field(default_factory=list)
    implementation_cost_low: float | None = None
    implementation_cost_high: float | None = None
    net_monthly_low: float | None = None
    net_monthly_high: float | None = None
    payback_months_conservative: float | None = None
    payback_months_neutral: float | None = None
    amount: float | None = None  # C 级 → None，只给方向
    direction_only: bool = False
    calculation_trace: list[str] = Field(default_factory=list)  # §13.4 强制展示计算过程
    assumptions: list[str] = Field(default_factory=list)
    dependency_released_saving: float | None = None  # A3 依赖收益单列


class FeasibilityScore(BaseModel):
    """§11.2 难度轴：七维 rubric，返回分项 + 缺失项，不返回单一总分。"""

    card_id: str
    dimensions: dict[str, float]
    missing: list[str] = Field(default_factory=list)
    weighted_difficulty: float | None = None  # 供矩阵定位，非"总分结论"
    rubric_version: str = "v1"


class Insight(BaseModel):
    """§7 第二部分：专家判断。不得出现具体 ROI 数字。"""

    insight_id: str
    statement: str
    basis: str
    verification_suggestion: str
    label: str = "此为经验判断，无数据支撑"


class Feedback(BaseModel):
    """§19.2 即时反馈：角色必填（G3）。"""

    feedback_id: str
    card_id: str
    role: str
    direction: Literal["偏高", "偏低", "基本相符", "没说到点上"]
    would_do_first: str | None = None
    would_not_do: str | None = None
    reason: str = ""
    created_at: str = ""

    @field_validator("role")
    @classmethod
    def _role_required(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("角色必填（G3）：不同角色的偏差方向已知，可反向校正")
        return v
