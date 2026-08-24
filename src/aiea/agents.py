"""LLM 分析子 Agent：反评审与洞察生成。

三条纪律落在本模块，而不是提示词的祈使句里：

1. **上下文隔离（§5 Isolate）**：反评审的入参由代码构造，主 Agent 推理链
   在这里根本没有传入通道——不是"提示模型不要看"，而是它拿不到。
2. **judge 不与主 Agent 同源（§14.3）**：反评审固定走 judge 档模型。
3. **模型产出一律过校验**：编造的场景 ID 直接丢弃；带金额的洞察直接拒收
   （§7 专家判断区不得出现 ROI 数字）。

LLM 不可用时回退到确定性内容：交付物不能因为上游抖动而空掉，
但回退内容同样不含任何模型凭空生成的数字。
"""

from __future__ import annotations

from typing import Any

from .guardrails import contains_money
from .llm import CostBreakerTripped, LLMResponseFormatError, salvage_json_objects

_SEVERITIES = ("高", "中", "低")

# 这些错误**绝不回退**：
# - CostBreakerTripped：§13.2 熔断动作为挂起 + 告警 + 落 trace，静默降级会掩盖待排查缺陷
# - ImportError / ValueError：依赖或配置问题（如缺少代理依赖、未设密钥）。
#   把它们当成"模型没话说"会让真正的故障看起来像正常输出——这是最难查的一类 bug。
_NEVER_FALLBACK = (CostBreakerTripped, ImportError, TypeError, AssertionError)

# 模型输出质量问题：可以回退，但必须记录原因
_MAY_FALLBACK = (LLMResponseFormatError,)

COUNTER_REVIEW_SYSTEM = """你是一名独立的资深顾问，负责对另一位顾问的诊断结论做最严苛的反评审。

你只会看到两样东西：场景卡与证据台账。你看不到对方的推理过程，这是刻意的——
看了会让你认同对方的前提。

请针对每个场景给出最强反驳，聚焦三点：
1. 数字是否站得住（口径、样本、折算方式是否有系统性偏差）
2. 是否漏了上游瓶颈（真正的问题可能在别的环节）
3. 落地是否有硬约束（系统即将更换、人员抵触、合规限制）

严格要求：
- 不要提出新的金额或收益数字，你的职责是质疑而非重算
- 每条反驳必须对应给定的 card_id，不得编造不存在的场景
- **每条 rebuttal 不超过 120 字，resolution 不超过 60 字**，只讲最致命的那一点，不要罗列
- 最多输出 3 条，挑最值得质疑的场景
- 只输出 JSON，不要任何解释文字：
  {"rebuttals":[{"card_id":"...","rebuttal":"...","severity":"高|中|低","resolution":"建议如何处理"}]}"""

INSIGHT_SYSTEM = """你是一名资深顾问，负责提出"证据不足但可能很关键"的判断。

这类判断会被放进报告的独立附录《基于经验的判断（无数据支撑）》，与数据结论物理隔离。

严格要求：
- **绝对不得出现任何金额、百分比收益或回本周期**。这一区只给方向与验证路径。
- 每条必须写明：判断内容、依据（基于哪些观察或常见模式）、建议的验证方式。
- 宁少勿滥：只提真正有价值的判断，最多 3 条。
- **每个字段不超过 110 字**，讲清方向即可，不要展开论证。
- 只输出 JSON，不要任何解释文字：
  {"insights":[{"statement":"...","basis":"...","verification_suggestion":"..."}]}"""


def _parse_items(text: str, key: str) -> list[dict[str, Any]]:
    """解析模型输出；被 max_tokens 截断时救回已完整的条目。

    截断是这类长输出的常见结局，整批丢弃会浪费已付费内容，
    也会让"模型有话说"看起来像"模型没话说"。
    """
    from .llm import parse_json_block

    try:
        data = parse_json_block(text)
    except ValueError:
        salvaged = salvage_json_objects(text)
        return [s for s in salvaged if isinstance(s, dict)]
    if isinstance(data, dict):
        value = data.get(key)
        if isinstance(value, list):
            return [v for v in value if isinstance(v, dict)]
        return []
    if isinstance(data, list):
        return [v for v in data if isinstance(v, dict)]
    return []


def _parse_rebuttals(text: str) -> list[dict[str, Any]]:
    return _parse_items(text, "rebuttals")


def _pick_focus(cards: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    """挑最值得反驳的场景：证据越弱、工时越大越该被质疑。"""
    rank = {"C": 0, "B": 1, "A": 2}
    ordered = sorted(
        cards,
        key=lambda c: (rank.get(c.get("evidence_grade"), 3), -(c.get("monthly_minutes") or 0)),
    )
    return ordered[: max(limit, 1)]


def _card_digest(cards: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """只提取反评审需要的字段。构造式隔离：其余信息没有传入通道。"""
    return [
        {
            "card_id": c.get("card_id"),
            "name": c.get("name"),
            "operator": c.get("operator"),
            "systems": c.get("systems"),
            "status_quo": c.get("status_quo"),
            "monthly_minutes": c.get("monthly_minutes"),
            "work_form": c.get("work_form"),
            "evidence_grade": c.get("evidence_grade"),
            "evidence_refs": c.get("evidence_refs"),
        }
        for c in cards
    ]


def _evidence_digest(evidence: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "evidence_id": e.get("evidence_id"),
            "grade": e.get("grade"),
            "origin": e.get("origin"),
            "source_type": e.get("source_type"),
            "sample_size": e.get("sample_size"),
        }
        for e in evidence
    ]


# ---------------------------------------------------------------------------
# 反评审
# ---------------------------------------------------------------------------
def _fallback_counter_review(
    cards: list[dict[str, Any]], *, reason: str = "未启用模型生成"
) -> list[dict[str, Any]]:
    """确定性回退：针对证据类型本身的固有局限提出质疑，不涉及任何新数字。"""
    out: list[dict[str, Any]] = []
    for card in cards[:3]:
        grade = card.get("evidence_grade")
        if grade == "A":
            rebuttal = (
                f"「{card.get('name')}」的工时来自时间戳推算，而时间戳间隔通常同时包含操作、"
                "思考与等待，直接当作可自动化耗时会偏高。"
            )
            resolution = "落地前用一小批真实数据实测净操作耗时，再回填修正。"
            severity = "中"
        elif grade == "B":
            rebuttal = (
                f"「{card.get('name')}」依据补数表等自述类材料，缺少系统痕迹交叉验证，"
                "填写者的口径差异会直接传导到结论。"
            )
            resolution = "索取对应系统导出以升级证据等级；在此之前只给区间。"
            severity = "高"
        else:
            rebuttal = (
                f"「{card.get('name')}」没有任何客观痕迹支撑，当前只能定位痛点、无法量化，"
                "不应据此安排投入。"
            )
            resolution = "先解决该环节的数据留痕，再评估是否值得改造。"
            severity = "高"
        out.append(
            {
                "card_id": card.get("card_id"),
                "rebuttal": rebuttal,
                "severity": severity,
                "resolution": resolution,
                "source": "fallback",
                "fallback_reason": reason,
            }
        )
    return out


def generate_counter_review(
    cards: list[dict[str, Any]],
    evidence: list[dict[str, Any]],
    *,
    llm: Any | None = None,
    reasoning_chain: str = "",
    max_items: int = 3,
) -> list[dict[str, Any]]:
    """S5 反评审。reasoning_chain 参数存在但**被显式忽略**，用于让调用方无处夹带。"""
    del reasoning_chain  # 刻意丢弃：反评审不得看主 Agent 推理链

    if llm is None:
        return _fallback_counter_review(cards)

    # 只送最值得质疑的场景：卡越多、模型越想逐条长篇论述，越容易撞上 token 上限
    focus = _pick_focus(cards, max_items)
    payload = {
        "task_cards": _card_digest(focus),
        "evidence_ledger": _evidence_digest(evidence),
    }
    import json as _json

    messages = [
        {"role": "system", "content": COUNTER_REVIEW_SYSTEM},
        {"role": "user", "content": _json.dumps(payload, ensure_ascii=False)},
    ]
    try:
        out = llm.complete(
            messages, role="judge", stage="S5", temperature=0.0, max_tokens=3200
        )
        rebuttals = _parse_rebuttals(out.text)
    except _MAY_FALLBACK as err:
        return _fallback_counter_review(cards, reason=f"模型输出无法解析：{err}")
    except _NEVER_FALLBACK:
        # 熔断与配置错误必须冒泡：它们是缺陷信号，不是"模型没话说"
        raise
    except Exception as err:
        return _fallback_counter_review(cards, reason=f"模型调用失败（{type(err).__name__}）：{err}")

    known = {c.get("card_id") for c in cards}
    items: list[dict[str, Any]] = []
    for raw in rebuttals[: max_items * 2]:
        cid = raw.get("card_id")
        rebuttal = (raw.get("rebuttal") or "").strip()
        # 编造不存在的场景 ID → 丢弃（工具误用式幻觉）
        if cid not in known or not rebuttal:
            continue
        severity = raw.get("severity") if raw.get("severity") in _SEVERITIES else "中"
        items.append(
            {
                "card_id": cid,
                "rebuttal": rebuttal,
                "severity": severity,
                "resolution": (raw.get("resolution") or "转人工终审").strip(),
                "source": "llm",
            }
        )
        if len(items) >= max_items:
            break

    return items or _fallback_counter_review(cards, reason="模型未返回可用反驳")


# ---------------------------------------------------------------------------
# 洞察
# ---------------------------------------------------------------------------
def _fallback_insights(*, reason: str = "未启用模型生成") -> list[dict[str, Any]]:
    base = [
        {
            "statement": "真正的瓶颈可能不在人手不足，而在上游信息没有向下传递，导致下游反复追问。",
            "basis": "咨询分类的分布高度集中在少数几类可预知的问题上，且不同部门对同一信息各自维护。",
            "verification_suggestion": "抽查一批该类记录，看处理者是否需要二次向其他部门确认才能答复。",
        },
        {
            "statement": "口径不统一属于流程未定义，先上工具可能只是把混乱搬得更快。",
            "basis": "材料显示同一业务对象在不同来源存在口径差异，且尚无统一定义责任人。",
            "verification_suggestion": "统计一个周期内差异项的成因分布，看多少来自口径而非操作失误。",
        },
        {
            "statement": "没有留痕的环节往往是最大的观测盲区，也常常是实际损耗最重的地方。",
            "basis": "由个人自行维护、无版本记录的环节无法量化，这类环节长期被低估。",
            "verification_suggestion": "让一名执行者配合记录 3 个工作日的实际操作，即可把盲区变成可用证据。",
        },
    ]
    return [
        {**b, "label": "此为经验判断，无数据支撑", "source": "fallback", "fallback_reason": reason}
        for b in base
    ]


def generate_insights(
    cards: list[dict[str, Any]], *, llm: Any | None = None, max_items: int = 3
) -> list[dict[str, Any]]:
    """§7 第二部分：低证据高价值洞察。带金额者一律拒收。"""
    if llm is None:
        return _fallback_insights()

    import json as _json

    payload = {"task_cards": _card_digest(cards)}
    try:
        out = llm.complete(
            [
                {"role": "system", "content": INSIGHT_SYSTEM},
                {"role": "user", "content": _json.dumps(payload, ensure_ascii=False)},
            ],
            role="primary",
            stage="S5",
            temperature=0.3,
            max_tokens=2600,
        )
        raw_items = _parse_items(out.text, "insights")
    except _MAY_FALLBACK as err:
        return _fallback_insights(reason=f"模型输出无法解析：{err}")
    except _NEVER_FALLBACK:
        raise
    except Exception as err:
        return _fallback_insights(reason=f"模型调用失败（{type(err).__name__}）：{err}")

    items: list[dict[str, Any]] = []
    for raw in raw_items[: max_items * 2]:
        statement = (raw.get("statement") or "").strip()
        verify = (raw.get("verification_suggestion") or "").strip()
        if not statement or not verify:
            continue
        # 专家判断区不得出现金额：模型越界即丢弃该条
        if contains_money(statement) or contains_money(verify):
            continue
        items.append(
            {
                "statement": statement,
                "basis": (raw.get("basis") or "").strip(),
                "verification_suggestion": verify,
                "label": "此为经验判断，无数据支撑",
                "source": "llm",
            }
        )
        if len(items) >= max_items:
            break

    return items or _fallback_insights(reason="模型未返回合规洞察（可能因含金额被拒收）")
