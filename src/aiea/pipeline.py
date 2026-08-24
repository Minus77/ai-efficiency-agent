"""S0–S5 编排（§2 Tier 2.5：Plan-and-Execute 骨架 + 局部 ReAct）。

诊断流程本身写死，只有「材料缺口判定」与「跨源证据拼接」交给模型自由发挥。
Plan-and-Execute 额外买到可审计的中间产物——客户要看的是"你凭什么这么说"。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .config import FROZEN, settings
from .guardrails import check_stage_transition
from .knowledge import KnowledgeBase
from .models import EvidenceGrade, Quadrant, Stage, ToolResult
from .workspace import Workspace


class StepLimitExceeded(RuntimeError):
    """单阶段步数上限。动作为挂起 + 告警 + 落 trace，不是静默降级（§13.2）。"""


# §5 Working Context 预算契约
CONTEXT_BUDGET = {
    "system_prompt": 0.15,
    "semantic_retrieval": 0.15,
    "task_card_working_set": 0.30,
    "material_summary_window": 0.20,
    "state_and_plan": 0.20,
}
SOFT_COMPACTION_AT = 0.70
HARD_LIMIT_AT = 0.90


@dataclass
class StageDef:
    name: str
    stage: Stage
    purpose: str
    outputs: list[str]


STAGES: list[StageDef] = [
    StageDef("S0", Stage.S0, "立项与口径：AS_OF、部门边界、数据可得性、准入探测", ["SCOPE.md", "scope.json"]),
    StageDef("S1", Stage.S1, "业务地图采集：材料清单驱动，缺口透明回报", ["materials/", "MATERIAL_REQUEST.md"]),
    StageDef("S2", Stage.S2, "任务级分解：父层业务结果 × 子层操作序列", ["task-cards/"]),
    StageDef("S3", Stage.S3, "AI 适配性打分：能力匹配 + 七维可行性", ["feasibility/"]),
    StageDef("S4", Stage.S4, "ROI 与可行性：纯函数估算，分档呈现", ["roi/"]),
    StageDef("S5", Stage.S5, "反评审与排序：独立上下文最强反驳 + 优先级矩阵", ["FINDINGS.md", "COUNTER_REVIEW.md"]),
]


@dataclass
class Diagnosis:
    """一次诊断的编排器。状态以磁盘为唯一真相源，上下文可随时丢弃重建。"""

    tenant: str
    workspace: Workspace
    kb: KnowledgeBase
    tracer: Any | None = None
    llm: Any | None = None
    stages: list[StageDef] = field(default_factory=lambda: list(STAGES))
    _steps: dict[str, int] = field(default_factory=dict)

    @property
    def max_steps(self) -> int:
        return settings.max_steps

    @property
    def counter_review_max_rounds(self) -> int:
        return int(FROZEN["cost_breaker"]["counter_review_max_rounds"])  # type: ignore[index]

    # -- 阶段与步数 ----------------------------------------------------------
    def step(self, stage: Stage, description: str) -> int:
        key = stage.value
        used = self._steps.get(key, 0) + 1
        if used > self.max_steps:
            if self.tracer is not None:
                self.tracer.event("step_limit_exceeded", {"stage": key, "limit": self.max_steps})
            raise StepLimitExceeded(
                f"阶段 {key} 步数超过上限 {self.max_steps}，已挂起并保留完整轨迹待人工排查"
            )
        self._steps[key] = used
        if self.tracer is not None:
            self.tracer.event("stage_step", {"stage": key, "n": used, "what": description})
        return used

    def step_count(self, stage: Stage) -> int:
        return self._steps.get(stage.value, 0)

    def enter_stage_for_card(
        self, stage: Stage, *, evidence_grade: EvidenceGrade, quantifiable: bool
    ) -> ToolResult:
        """阶段跃迁校验：证据不足不许进 S4（§13.1 推理校验层）。"""
        verdict = check_stage_transition(
            target_stage=stage.name, evidence_grade=evidence_grade, quantifiable=quantifiable
        )
        if not verdict.ok and self.tracer is not None:
            self.tracer.event("guardrail_triggered", {"layer": "推理校验", "action": verdict.note})
        return verdict

    # -- 长周期会话（§5） ----------------------------------------------------
    def resume(self) -> dict[str, Any]:
        """每次恢复会话先读盘重建状态，不依赖历史上下文。"""
        state = self.workspace.state()
        if self.tracer is not None:
            self.tracer.event("session_resumed", {"cards": len(state["cards"]), "stage": state["stage"]})
        return state

    def context_budget(self, *, used_ratio: float) -> dict[str, Any]:
        if used_ratio >= HARD_LIMIT_AT:
            return {
                "compaction": "hard",
                "action": "强制落盘并换批：把当前工作集写入 task-cards/ 后清空上下文重建",
                "partitions": CONTEXT_BUDGET,
            }
        if used_ratio >= SOFT_COMPACTION_AT:
            return {
                "compaction": "soft",
                "action": "触发压缩：合并明确重复的材料摘要，并声明丢弃内容",
                "partitions": CONTEXT_BUDGET,
            }
        return {"compaction": "none", "action": "继续", "partitions": CONTEXT_BUDGET}

    def compress(self, summaries: list[str], *, keep: int) -> dict[str, Any]:
        """压缩必须显式声明丢弃内容（loss budget，§5 Compress）。

        只允许合并明确重复项，禁止全量重写——防 ACE 记录的 brevity bias。
        """
        kept = summaries[-keep:] if keep > 0 else []
        dropped = summaries[: max(len(summaries) - keep, 0)]
        return {
            "kept": kept,
            "dropped_count": len(dropped),
            "loss_declaration": (
                f"已丢弃最早 {len(dropped)} 条材料摘要（仍在 materials/ 可回读）："
                + "；".join(d[:24] for d in dropped)
            )
            if dropped
            else "",
            "rule": "只增量追加与局部修订，禁止全量重写",
        }

    def batch_cards(self, cards: list[dict[str, Any]], *, batch_size: int = 8) -> list[list[dict[str, Any]]]:
        """按同一业务流聚类分批，不按部门（跨流对比引入无关干扰，§5 Select）。"""
        grouped: dict[str, list[dict[str, Any]]] = {}
        for c in cards:
            grouped.setdefault(c.get("business_flow", "未分类"), []).append(c)
        batches: list[list[dict[str, Any]]] = []
        for flow_cards in grouped.values():
            for i in range(0, len(flow_cards), batch_size):
                batches.append(flow_cards[i : i + batch_size])
        return batches

    # -- 排序与路线图（§11.2、§11.4） ----------------------------------------
    def quadrant(
        self, *, benefit: float, difficulty: float, benefit_threshold: float, difficulty_threshold: float
    ) -> Quadrant:
        high_benefit = benefit >= benefit_threshold
        high_difficulty = difficulty >= difficulty_threshold
        if high_benefit and not high_difficulty:
            return Quadrant.DO_FIRST
        if high_benefit and high_difficulty:
            return Quadrant.PLAN
        if not high_benefit and not high_difficulty:
            return Quadrant.OPPORTUNISTIC
        return Quadrant.DO_NOT

    def roadmap(self, cards: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """90 天三批次，每批写明验收标准与失败退出条件。"""
        do_first = sorted(
            [c for c in cards if c.get("quadrant") == Quadrant.DO_FIRST.value],
            key=lambda c: c.get("monthly_saving", 0),
            reverse=True,
        )
        planned = [c for c in cards if c.get("quadrant") == Quadrant.PLAN.value]

        batch1 = do_first[:1] or do_first
        batch2 = do_first[1:3]
        return [
            {
                "window": "第 1–30 天",
                "goal": "建立信心与验证方法：选最容易验证的 1–2 个场景",
                "cards": [{"card_id": c["card_id"], "name": c["name"]} for c in batch1],
                "owner_role": "运营负责人 + 一名一线执行者",
                "resources": "能力订阅试用额度、1 名 IT 对接人（约 3 人日）",
                "acceptance": "该环节人工处理时长较改造前基线下降 ≥ 30%，且错误率不上升",
                "exit_condition": "两周内准确率达不到可用线（低于 80%）即停止投入，转回人工并复盘原因",
            },
            {
                "window": "第 31–60 天",
                "goal": "复用第一批经验，扩展到 2–3 个场景",
                "cards": [{"card_id": c["card_id"], "name": c["name"]} for c in batch2],
                "owner_role": "运营负责人 + 财务/客服各 1 名对接人",
                "resources": "正式订阅 + 集成人力（约 5–8 人日）+ 一线培训 2 小时",
                "acceptance": "第一批场景保持稳定运行，新场景达到与第一批相当的下降幅度",
                "exit_condition": "第一批出现回退或维护投入超过节省工时，暂停扩展",
            },
            {
                "window": "第 61–90 天",
                "goal": "复盘并决定是否扩大范围",
                "cards": [{"card_id": c["card_id"], "name": c["name"]} for c in planned[:2]],
                "owner_role": "老板 + 运营负责人",
                "resources": "半天复盘会 + 数据整理",
                "acceptance": "用实际数据替换报告中的假设值，重算 ROI 并确认是否符合预期",
                "exit_condition": "累计净收益为负或一线抵触明显，则收缩至已验证场景，不再扩大",
            },
        ]

    def traceability_rate(self, cards: list[dict[str, Any]]) -> float:
        """§14.1 证据可追溯率：每条量化声明能否回指证据。门槛 100%。"""
        quantified = [c for c in cards if c.get("monthly_minutes") is not None]
        if not quantified:
            return 1.0
        cited = [c for c in quantified if c.get("evidence_refs")]
        return round(len(cited) / len(quantified), 4)
