"""七维可行性 rubric（§11.2 难度轴、§6 feasibility_score）。

纪律：返回分项 + 缺失项，不返回单一总分结论。
缺任一维度时不合成难度值——缺就报缺，由编排层决定补数还是降级。
"""

from __future__ import annotations

from .models import FeasibilityScore, ToolResult

# 七维及其权重（rubric 权重属"需人工批准生效"的可演进区，§9.7）
DIMENSIONS: dict[str, float] = {
    "数据可得性": 0.22,
    "系统集成度": 0.18,
    "流程标准化程度": 0.16,
    "人员接受度": 0.14,
    "AI能力匹配度": 0.16,
    "合规风险": 0.08,
    "维护成本": 0.06,
}

RUBRIC_VERSION = "v1"
_MIN, _MAX = 1.0, 5.0


def feasibility_score(*, card_id: str, scores: dict[str, float]) -> ToolResult:
    """打分：1 = 很容易，5 = 很难。返回分项与缺失项。"""
    unknown = [k for k in scores if k not in DIMENSIONS]
    if unknown:
        return ToolResult.invalid(
            f"未知维度 {unknown}",
            next_action=f"七维固定为 {list(DIMENSIONS)}；rubric 权重变更须人工批准，不可在运行时新增维度",
        )
    bad = {k: v for k, v in scores.items() if not (_MIN <= float(v) <= _MAX)}
    if bad:
        return ToolResult.invalid(
            f"分值越界 {bad}",
            next_action="每维取值范围为 1–5（1=很容易，5=很难）；请重新打分",
        )

    missing = [k for k in DIMENSIONS if k not in scores]
    weighted: float | None = None
    if not missing:
        weighted = round(sum(DIMENSIONS[k] * float(scores[k]) for k in DIMENSIONS), 3)

    fs = FeasibilityScore(
        card_id=card_id,
        dimensions={k: round(float(v), 2) for k, v in scores.items()},
        missing=missing,
        weighted_difficulty=weighted,
        rubric_version=RUBRIC_VERSION,
    )
    note = "七维分项已返回" + ("" if not missing else f"；缺 {len(missing)} 维，未合成难度值")
    return ToolResult.success(
        {"feasibility": fs},
        note=note,
        source=[f"rubric:{RUBRIC_VERSION}"],
    )
