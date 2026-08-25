"""配置与冻结区。

架构依据 §9.7：Agent 无任何工具可修改自身配置、护栏或 playbook 冻结区。
因此冻结口径以只读映射暴露，运行时写入直接抛错。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Final

# ---------------------------------------------------------------------------
# 冻结区（§9.7）：仅人工 + 版本发布可改，Agent 与自学习回路一律不可触碰
# ---------------------------------------------------------------------------
_FROZEN: Final[dict[str, object]] = {
    # §13.4 置信度分级由证据类型决定，非模型自评
    "evidence_grading": {
        "A": "单据痕迹或系统数据支撑 → 点估 + 区间",
        "B": "多方交叉且至少一路有客观痕迹 → 仅区间",
        "C": "单方陈述或基准外推 → 仅方向性判断，不给金额",
    },
    # §11.3 ROI 公式
    "roi_formula": (
        "折现后月度工时 = 月度工时 × 折现系数(作业形态); "
        "月度收益 = 折现后工时/60 × 小时综合成本 × 自动化率; "
        "净收益 = 折现后收益 − 实施成本; 回本月数 = 实施成本 / 月度净收益"
    ),
    # §6 取舍 1 / §8.3
    "insufficient_data_semantics": (
        "insufficient_data 与 no_grounding 是 ok=True 的一等公民返回值，"
        "不得被异常吞掉，也不得由模型用训练知识补齐"
    ),
    # §4：全部连接器只读，无一例外
    "readonly_boundary": "连接器只读；写操作只落本地 workspace/",
    # §13.2 硬预算
    "max_steps": 20,
    "cost_breaker": {"session_limit_usd": 6.0, "hour_limit_usd": 20.0, "counter_review_max_rounds": 3},
    # §3.1 冲突裁决序（固定，不由模型自由决定）
    "adjudication_order": [
        "timestamp_export",
        "time_log",
        "supplement_form",
        "cross_check",
        "self_report",
    ],
    "conflict_threshold": 0.30,
    # §3.2 折现规则
    "discount_rules": {"continuous": 1.0, "batch": 1.0, "fragmented": 0.0},
}

FROZEN: Final[MappingProxyType] = MappingProxyType(_FROZEN)

# 可自演进区（§9.7 左列）——仅登记，供观测与审计对照
EVOLVABLE_SURFACES: Final[tuple[str, ...]] = (
    "material_checklist_templates",
    "industry_priors",
    "retrieval_query_strategy",
    "scenario_clustering_naming",
    "rubric_weights_pending_human_approval",
)


@dataclass(frozen=True)
class Settings:
    """运行配置。密钥永不写入上下文，只在工具层读取（§13.3）。"""

    base_url: str = field(default_factory=lambda: os.getenv("AIEA_BASE_URL", "https://api.wenwen-ai.com/v1"))
    api_key: str = field(default_factory=lambda: os.getenv("AIEA_API_KEY", ""))
    # 主 Agent
    primary_model: str = field(default_factory=lambda: os.getenv("AIEA_PRIMARY_MODEL", "claude-sonnet-4-5"))
    # §14.3 judge 不得与主 Agent 同源
    judge_model: str = field(default_factory=lambda: os.getenv("AIEA_JUDGE_MODEL", "claude-opus-4-5-20251101"))
    # 采集类子 Agent：便宜档，主 Agent 不看原始数据（§5）
    extractor_model: str = field(default_factory=lambda: os.getenv("AIEA_EXTRACTOR_MODEL", "claude-haiku-4-5-20251001"))

    workspace_root: str = field(default_factory=lambda: os.getenv("AIEA_WORKSPACE", "workspace"))

    @property
    def max_steps(self) -> int:
        return int(FROZEN["max_steps"])  # type: ignore[arg-type]

    @property
    def session_limit_usd(self) -> float:
        return float(FROZEN["cost_breaker"]["session_limit_usd"])  # type: ignore[index]

    @property
    def hour_limit_usd(self) -> float:
        return float(FROZEN["cost_breaker"]["hour_limit_usd"])  # type: ignore[index]


settings = Settings()


def default_workspace_root() -> str:
    """工作区根目录的**唯一**取值入口。

    各处默认参数一律调用它，而不是各写一遍字符串 "workspace"——
    硬写默认值会让 AIEA_WORKSPACE 静默失效：调用方以为自己指定了目录，
    数据却落进了仓库。每次读环境变量而不缓存，测试才能用 monkeypatch 覆盖。
    """
    return os.getenv("AIEA_WORKSPACE", "workspace")
