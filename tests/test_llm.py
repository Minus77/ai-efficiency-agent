"""Task 8：LLM 客户端、成本累计与双档熔断（§13.2、§10.3）。"""
import pytest

from aiea.llm import CostBreakerTripped, LLMClient, LLMUsage


class FakeTransport:
    """注入式假传输：断言请求形状，不打真网络。"""

    def __init__(self, replies=None, fail_times=0):
        self.calls = []
        self.replies = replies or ["OK"]
        self.fail_times = fail_times
        self._idx = 0

    def post(self, url, *, headers, json):
        self.calls.append({"url": url, "headers": headers, "json": json})
        if self.fail_times > 0:
            self.fail_times -= 1
            raise TimeoutError("upstream timeout")
        content = self.replies[min(self._idx, len(self.replies) - 1)]
        self._idx += 1
        return {
            "choices": [{"message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
            "usage": {
                "prompt_tokens": 1000,
                "completion_tokens": 200,
                "prompt_tokens_details": {"cached_tokens": 400},
            },
        }


def test_calls_the_configured_gateway_and_model():
    t = FakeTransport()
    c = LLMClient(api_key="sk-test", transport=t)
    c.complete([{"role": "user", "content": "hi"}])
    call = t.calls[0]
    assert call["url"] == "https://api.wenwen-ai.com/v1/chat/completions"
    assert call["json"]["model"] == "claude-sonnet-4-5"
    assert call["headers"]["Authorization"] == "Bearer sk-test"


def test_api_key_never_appears_in_usage_log():
    t = FakeTransport()
    c = LLMClient(api_key="sk-secret-123456789", transport=t)
    c.complete([{"role": "user", "content": "hi"}])
    assert all("sk-secret" not in str(u) for u in c.usage_log)


def test_judge_role_uses_a_different_model():
    t = FakeTransport()
    c = LLMClient(api_key="sk-test", transport=t)
    c.complete([{"role": "user", "content": "反驳"}], role="judge")
    assert t.calls[0]["json"]["model"] != "claude-sonnet-4-5"


def test_usage_and_cost_accumulate():
    t = FakeTransport()
    c = LLMClient(api_key="sk-test", transport=t)
    c.complete([{"role": "user", "content": "hi"}])
    c.complete([{"role": "user", "content": "hi again"}])
    assert c.total_usd > 0
    assert len(c.usage_log) == 2
    assert isinstance(c.usage_log[0], LLMUsage)
    assert c.usage_log[0].cached_tokens == 400


def test_session_breaker_suspends_rather_than_silently_degrading():
    t = FakeTransport()
    c = LLMClient(api_key="sk-test", transport=t, session_limit_usd=0.0001)
    with pytest.raises(CostBreakerTripped) as exc:
        for _ in range(5):
            c.complete([{"role": "user", "content": "hi"}])
    # 熔断动作为挂起 + 告警 + 落 trace，不是静默降级
    assert "挂起" in str(exc.value)


def test_retries_transient_failure_then_succeeds():
    t = FakeTransport(fail_times=2)
    c = LLMClient(api_key="sk-test", transport=t, max_retries=3)
    out = c.complete([{"role": "user", "content": "hi"}])
    assert out.text == "OK"
    assert len(t.calls) == 3


def test_json_mode_parses_structured_output():
    t = FakeTransport(replies=['```json\n{"a": 1}\n```'])
    c = LLMClient(api_key="sk-test", transport=t)
    out = c.complete_json([{"role": "user", "content": "give json"}])
    assert out == {"a": 1}


def test_missing_api_key_is_a_clear_config_error():
    with pytest.raises(ValueError) as exc:
        LLMClient(api_key="", transport=FakeTransport())
    assert "AIEA_API_KEY" in str(exc.value)
