"""LLM 分析子 Agent：反评审与洞察生成（§5 Isolate、§9.2 L1、§14.3 judge 不同源）。

纪律：
- 反评审只拿任务卡与证据台账，拿不到主 Agent 推理链
- 反评审用 judge 模型（与主 Agent 不同源）
- LLM 不可用时回退到确定性内容，绝不中断交付，也绝不让模型凭空造数字
- 模型产出的洞察一律过金额检查后才入库
"""
import json

import pytest

from aiea.agents import generate_counter_review, generate_insights
from aiea.llm import LLMClient

CARDS = [
    {
        "card_id": "s-01", "name": "微信咨询转录进工单", "monthly_minutes": 1200,
        "evidence_grade": "A", "evidence_refs": ["e01"], "work_form": "batch",
        "status_quo": "手工转录", "operator": "客服专员", "systems": ["微信"],
    },
    {
        "card_id": "s-04", "name": "供应商对账手工比对", "monthly_minutes": 765,
        "evidence_grade": "A", "evidence_refs": ["e02"], "work_form": "batch",
        "status_quo": "逐条比对", "operator": "财务专员", "systems": ["Excel"],
    },
]
EVIDENCE = [
    {"evidence_id": "e01", "grade": "A", "origin": "tickets.csv", "source_type": "timestamp_export"},
    {"evidence_id": "e02", "grade": "A", "origin": "revisions.csv", "source_type": "timestamp_export"},
]


class RecordingTransport:
    """记录请求并返回预设 JSON 文本。"""

    def __init__(self, content):
        self.calls = []
        self.content = content

    def post(self, url, **kwargs):
        self.calls.append(kwargs["json"])
        return {
            "choices": [{"message": {"role": "assistant", "content": self.content}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 900, "completion_tokens": 210, "prompt_tokens_details": {"cached_tokens": 100}},
        }


class BoomTransport:
    def post(self, url, **kwargs):
        raise TimeoutError("upstream down")


def _client(transport):
    return LLMClient(api_key="sk-test", transport=transport, max_retries=1)


# ----------------------------- 反评审 -----------------------------
def test_counter_review_uses_judge_model_not_primary():
    t = RecordingTransport(json.dumps({"rebuttals": [
        {"card_id": "s-01", "rebuttal": "首响间隔含思考时间，转录净耗时可能被高估", "severity": "中",
         "resolution": "用 20 条真实数据实测净耗时"},
    ]}, ensure_ascii=False))
    generate_counter_review(CARDS, EVIDENCE, llm=_client(t))
    assert t.calls[0]["model"] != "claude-sonnet-4-5"
    assert t.calls[0]["temperature"] == 0.0


def test_counter_review_prompt_excludes_main_agent_reasoning():
    t = RecordingTransport(json.dumps({"rebuttals": []}, ensure_ascii=False))
    generate_counter_review(
        CARDS, EVIDENCE, llm=_client(t), reasoning_chain="主 Agent 认为客服是最大瓶颈因此优先"
    )
    blob = json.dumps(t.calls[0], ensure_ascii=False)
    assert "主 Agent 认为" not in blob
    assert "最大瓶颈因此优先" not in blob
    # 但任务卡与台账必须在
    assert "s-01" in blob and "e01" in blob


def test_counter_review_parses_model_output():
    t = RecordingTransport(json.dumps({"rebuttals": [
        {"card_id": "s-04", "rebuttal": "两个对账周期折半可能不代表月均", "severity": "高",
         "resolution": "用第三个周期复核"},
    ]}, ensure_ascii=False))
    out = generate_counter_review(CARDS, EVIDENCE, llm=_client(t))
    assert out[0]["card_id"] == "s-04"
    assert out[0]["severity"] == "高"
    assert out[0]["source"] == "llm"


def test_counter_review_falls_back_when_llm_unavailable():
    out = generate_counter_review(CARDS, EVIDENCE, llm=_client(BoomTransport()))
    assert out, "LLM 不可用时必须回退，不得让交付物空掉"
    assert all(i["source"] == "fallback" for i in out)
    assert all(i["rebuttal"] for i in out)


def test_counter_review_falls_back_on_malformed_json():
    out = generate_counter_review(CARDS, EVIDENCE, llm=_client(RecordingTransport("这不是 JSON")))
    assert all(i["source"] == "fallback" for i in out)


def test_counter_review_drops_rebuttals_for_unknown_cards():
    """模型编造不存在的场景 ID 时必须被丢弃（防工具误用式幻觉）。"""
    t = RecordingTransport(json.dumps({"rebuttals": [
        {"card_id": "s-99", "rebuttal": "编造的场景", "severity": "高", "resolution": "无"},
        {"card_id": "s-01", "rebuttal": "真实场景的合理反驳", "severity": "中", "resolution": "复核"},
    ]}, ensure_ascii=False))
    out = generate_counter_review(CARDS, EVIDENCE, llm=_client(t))
    ids = [i["card_id"] for i in out]
    assert "s-99" not in ids
    assert "s-01" in ids


# ----------------------------- 洞察 -----------------------------
def test_insights_reject_model_output_containing_money():
    """模型给出带金额的洞察时必须被拒（§7 专家判断区不得出现 ROI 数字）。"""
    t = RecordingTransport(json.dumps({"insights": [
        {"statement": "瓶颈在销售，预计每月可省 ¥12,000", "basis": "观察", "verification_suggestion": "抽查订单"},
        {"statement": "对账口径不统一是上游问题", "basis": "纪要", "verification_suggestion": "统计差异成因"},
    ]}, ensure_ascii=False))
    out = generate_insights(CARDS, llm=_client(t))
    texts = [i["statement"] for i in out]
    assert not any("12,000" in s for s in texts)
    assert any("上游问题" in s for s in texts)


def test_insights_require_verification_suggestion():
    t = RecordingTransport(json.dumps({"insights": [
        {"statement": "没有验证方式的判断", "basis": "经验", "verification_suggestion": ""},
    ]}, ensure_ascii=False))
    out = generate_insights(CARDS, llm=_client(t))
    assert all(i["verification_suggestion"] for i in out)


def test_insights_fall_back_when_llm_unavailable():
    out = generate_insights(CARDS, llm=_client(BoomTransport()))
    assert out
    assert all(i["label"] for i in out)


def test_insights_carry_expert_judgment_label():
    t = RecordingTransport(json.dumps({"insights": [
        {"statement": "真正的瓶颈可能在上游信息传递", "basis": "工单分类分布", "verification_suggestion": "抽查一批工单"},
    ]}, ensure_ascii=False))
    out = generate_insights(CARDS, llm=_client(t))
    assert "经验判断" in out[0]["label"]


def test_no_llm_client_returns_fallback_without_crashing():
    assert generate_counter_review(CARDS, EVIDENCE, llm=None)
    assert generate_insights(CARDS, llm=None)


# ----------------------------- 回退纪律 -----------------------------
class BreakerTransport:
    """模拟成本熔断：熔断必须挂起，不得被当成"上游抖动"静默降级。"""

    def post(self, url, **kwargs):  # pragma: no cover - 不应被调用到
        raise AssertionError("熔断应在发请求前触发")


def test_cost_breaker_is_not_silently_degraded_to_fallback():
    """§13.2：熔断动作为挂起 + 告警 + 落 trace，不是静默降级。"""
    from aiea.llm import CostBreakerTripped

    client = LLMClient(api_key="sk-test", transport=BreakerTransport(), session_limit_usd=0.0)
    with pytest.raises(CostBreakerTripped):
        generate_counter_review(CARDS, EVIDENCE, llm=client)


def test_missing_dependency_or_config_error_is_not_silently_swallowed():
    """配置/依赖类错误（如缺少代理依赖）必须抛出，否则会被误读成『模型没话说』。"""

    class ConfigBroken:
        def post(self, url, **kwargs):
            raise ImportError("Using SOCKS proxy, but the 'socksio' package is not installed.")

    with pytest.raises(ImportError):
        generate_counter_review(CARDS, EVIDENCE, llm=_client(ConfigBroken()))


def test_transient_network_failure_falls_back_with_recorded_reason():
    """真正的瞬时故障才回退，且必须留下回退原因，不能静默。"""
    out = generate_counter_review(CARDS, EVIDENCE, llm=_client(BoomTransport()))
    assert all(i["source"] == "fallback" for i in out)
    assert all(i.get("fallback_reason") for i in out), "回退必须记录原因，便于排查"


def test_insights_transient_failure_records_reason():
    out = generate_insights(CARDS, llm=_client(BoomTransport()))
    assert all(i.get("fallback_reason") for i in out)


def test_insights_breaker_propagates():
    from aiea.llm import CostBreakerTripped

    client = LLMClient(api_key="sk-test", transport=BreakerTransport(), session_limit_usd=0.0)
    with pytest.raises(CostBreakerTripped):
        generate_insights(CARDS, llm=client)


# ----------------------------- 截断与产出规模 -----------------------------
class TruncatingTransport:
    """模拟因 max_tokens 用尽导致的 JSON 截断（finish_reason=length）。"""

    def __init__(self, text):
        self.text = text
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append(kwargs["json"])
        return {
            "choices": [{"message": {"role": "assistant", "content": self.text}, "finish_reason": "length"}],
            "usage": {"prompt_tokens": 1200, "completion_tokens": 2000, "prompt_tokens_details": {"cached_tokens": 0}},
        }


TRUNCATED = (
    '```json\n{"rebuttals":[' 
    '{"card_id":"s-01","rebuttal":"\u65f6\u95f4\u6233\u53ea\u8bc1\u660e\u521b\u5efa\u65f6\u95f4","severity":"\u4e2d","resolution":"\u5b9e\u6d4b\u51c0\u8017\u65f6"},'
    '{"card_id":"s-04","rebuttal":"\u4e24\u4e2a\u5468\u671f\u6298\u534a\u4e0d\u4ee3\u8868\u6708\u5747","severity":"\u9ad8","resolution":"\u7b2c\u4e09\u4e2a\u5468\u671f\u590d\u6838"},'
    '{"card_id":"s-01","rebuttal":"\u88ab\u622a'
)


def test_truncated_json_salvages_complete_objects_instead_of_total_fallback():
    """截断时应救回已完整的条目，而不是整批丢弃退回默认内容。"""
    out = generate_counter_review(CARDS, EVIDENCE, llm=_client(TruncatingTransport(TRUNCATED)))
    assert any(i["source"] == "llm" for i in out), "完整条目应被救回"
    ids = [i["card_id"] for i in out]
    assert "s-01" in ids and "s-04" in ids
    assert all(i["rebuttal"] for i in out)


def test_counter_review_asks_for_brevity_and_limits_scope():
    """产出规模必须被约束，否则会稳定撞上 token 上限。"""
    t = RecordingTransport('{"rebuttals":[]}')
    many = [dict(CARDS[0], card_id=f"s-{i:02d}") for i in range(1, 9)]
    generate_counter_review(many, EVIDENCE, llm=_client(t), max_items=3)
    req = t.calls[0]
    system = req["messages"][0]["content"]
    assert "字" in system, "系统提示应给出长度上限"
    # 只送最需要反驳的场景，不把 8 张卡全部灌进去
    user = req["messages"][1]["content"]
    assert user.count('"card_id"') <= 4
    assert req["max_tokens"] >= 3000


def test_salvage_helper_handles_plain_truncated_array():
    from aiea.llm import salvage_json_objects

    items = salvage_json_objects('{"rebuttals":[{"a":1},{"a":2},{"a":')
    assert items == [{"a": 1}, {"a": 2}]
