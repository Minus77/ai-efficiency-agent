"""LLM 客户端（OpenAI 兼容协议）。

纪律：
- 密钥只在本层从环境/vault 读取，永不进上下文、永不进用量日志（§13.3）。
- 成本熔断为**挂起 + 告警 + 落 trace**，不是静默降级（§13.2）。
- 角色分离：主 Agent / judge / 采集子 Agent 用不同模型档位（§14.3 judge 不与主 Agent 同源）。
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, Protocol

from .config import settings

Role = Literal["primary", "judge", "extractor"]


class Transport(Protocol):
    """可注入的传输层，便于测试不打真网络。"""

    def post(self, url: str, *, headers: dict[str, str], json: dict[str, Any]) -> dict[str, Any]: ...


class HttpxTransport:
    def __init__(self, timeout: float = 120.0) -> None:
        self._timeout = timeout

    def post(self, url: str, *, headers: dict[str, str], json: dict[str, Any]) -> dict[str, Any]:
        import httpx

        with httpx.Client(timeout=self._timeout) as client:
            resp = client.post(url, headers=headers, json=json)
            resp.raise_for_status()
            return resp.json()


class CostBreakerTripped(RuntimeError):
    """熔断：挂起当前诊断并要求归因，不允许简单提高阈值了事（§13.2）。"""


class LLMResponseFormatError(ValueError):
    """模型未返回可解析结构。

    这属于模型输出质量问题（可回退），不同于配置或依赖缺失（必须冒泡）。
    """


@dataclass
class LLMUsage:
    model: str
    role: Role
    input_tokens: int
    output_tokens: int
    cached_tokens: int
    usd: float
    stage: str
    at: str


@dataclass
class LLMOutput:
    text: str
    usage: LLMUsage
    finish_reason: str = "stop"


# 粗粒度价目表（USD / 1K tokens），仅用于熔断与成本可观测，不作账单依据
_PRICE: dict[str, tuple[float, float]] = {
    "claude-sonnet-4-5": (0.003, 0.015),
    "claude-opus-4-5-20251101": (0.015, 0.075),
    "claude-haiku-4-5-20251001": (0.0008, 0.004),
}
_DEFAULT_PRICE = (0.003, 0.015)
_CACHE_READ_DISCOUNT = 0.1


@dataclass
class LLMClient:
    api_key: str = ""
    base_url: str = ""
    transport: Transport | None = None
    session_limit_usd: float | None = None
    hour_limit_usd: float | None = None
    max_retries: int = 3
    retry_backoff: float = 0.0
    tracer: Any | None = None

    usage_log: list[LLMUsage] = field(default_factory=list)
    total_usd: float = 0.0
    _hour_window: list[tuple[float, float]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.api_key = self.api_key or settings.api_key
        if not self.api_key:
            raise ValueError(
                "缺少 API 密钥：请设置环境变量 AIEA_API_KEY（密钥只在工具层读取，永不进上下文）"
            )
        self.base_url = (self.base_url or settings.base_url).rstrip("/")
        self.transport = self.transport or HttpxTransport()
        if self.session_limit_usd is None:
            self.session_limit_usd = settings.session_limit_usd
        if self.hour_limit_usd is None:
            self.hour_limit_usd = settings.hour_limit_usd

    # -- 内部 ---------------------------------------------------------------
    def _model_for(self, role: Role) -> str:
        return {
            "primary": settings.primary_model,
            "judge": settings.judge_model,
            "extractor": settings.extractor_model,
        }[role]

    def _price(self, model: str, usage: dict[str, Any]) -> float:
        pin, pout = _PRICE.get(model, _DEFAULT_PRICE)
        cached = int((usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0) or 0)
        fresh_in = max(int(usage.get("prompt_tokens", 0) or 0) - cached, 0)
        out = int(usage.get("completion_tokens", 0) or 0)
        return round(
            fresh_in / 1000 * pin + cached / 1000 * pin * _CACHE_READ_DISCOUNT + out / 1000 * pout,
            6,
        )

    def _check_breakers(self) -> None:
        # 用 is not None 判断：阈值 0.0 表示"立即熔断"，是合法配置而非未设置
        if self.session_limit_usd is not None and self.total_usd >= self.session_limit_usd:
            self._trip(
                f"单次诊断累计成本 ${self.total_usd:.4f} 已达上限 ${self.session_limit_usd:.2f}",
            )
        now = time.time()
        self._hour_window = [(t, c) for t, c in self._hour_window if now - t < 3600]
        hourly = sum(c for _, c in self._hour_window)
        if self.hour_limit_usd is not None and hourly >= self.hour_limit_usd:
            self._trip(f"近一小时累计成本 ${hourly:.4f} 已达上限 ${self.hour_limit_usd:.2f}")

    def _trip(self, reason: str) -> None:
        if self.tracer is not None:
            self.tracer.event("cost_breaker_tripped", {"reason": reason})
        raise CostBreakerTripped(
            f"{reason}。已挂起当前诊断并保留完整轨迹，等待人工排查——"
            "熔断触发一律视为待排查缺陷（材料重复解析、检索死循环、反评审震荡），不允许简单提高阈值了事。"
        )

    # -- 对外 ---------------------------------------------------------------
    def complete(
        self,
        messages: list[dict[str, str]],
        *,
        role: Role = "primary",
        stage: str = "-",
        temperature: float = 0.0,
        max_tokens: int = 2048,
        response_format: dict[str, Any] | None = None,
    ) -> LLMOutput:
        self._check_breakers()
        model = self._model_for(role)
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if response_format:
            payload["response_format"] = response_format

        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        url = f"{self.base_url}/chat/completions"

        last_err: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            try:
                assert self.transport is not None
                data = self.transport.post(url, headers=headers, json=payload)
                break
            except Exception as err:  # 传输层瞬时故障重试
                last_err = err
                if attempt >= self.max_retries:
                    raise
                if self.retry_backoff:
                    time.sleep(self.retry_backoff * attempt)
        else:  # pragma: no cover
            raise last_err  # type: ignore[misc]

        choice = (data.get("choices") or [{}])[0]
        text = ((choice.get("message") or {}).get("content")) or ""
        raw_usage = data.get("usage") or {}
        usage = LLMUsage(
            model=model,
            role=role,
            input_tokens=int(raw_usage.get("prompt_tokens", 0) or 0),
            output_tokens=int(raw_usage.get("completion_tokens", 0) or 0),
            cached_tokens=int((raw_usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0) or 0),
            usd=self._price(model, raw_usage),
            stage=stage,
            at=datetime.now().isoformat(timespec="seconds"),
        )
        self.usage_log.append(usage)
        self.total_usd = round(self.total_usd + usage.usd, 6)
        self._hour_window.append((time.time(), usage.usd))
        if self.tracer is not None:
            with self.tracer.span(
                "llm",
                name=f"{role}:{stage}",
                attrs={
                    "gen_ai.request.model": model,
                    "gen_ai.usage.input_tokens": usage.input_tokens,
                    "gen_ai.usage.output_tokens": usage.output_tokens,
                    "gen_ai.usage.cache_read.input_tokens": usage.cached_tokens,
                    "gen_ai.response.finish_reasons": [choice.get("finish_reason", "stop")],
                },
            ):
                pass
            self.tracer.record_cost(
                stage=stage,
                usd=usage.usd,
                cache_read_tokens=usage.cached_tokens,
                input_tokens=max(usage.input_tokens - usage.cached_tokens, 0),
            )
        return LLMOutput(text=text, usage=usage, finish_reason=choice.get("finish_reason", "stop"))

    def complete_json(self, messages: list[dict[str, str]], **kwargs: Any) -> dict[str, Any]:
        """要求结构化输出并容错解析 ```json 围栏。"""
        out = self.complete(messages, **kwargs)
        return parse_json_block(out.text)


def salvage_json_objects(text: str) -> list[dict[str, Any]]:
    """从被截断的 JSON 文本中救回已经完整的对象。

    模型撞上 max_tokens 时返回的是合法前缀 + 半个对象。整批丢弃等于浪费
    已经付费拿到的内容，也会让"模型有话说"看起来像"模型没话说"。

    做法：按括号配平扫描，收集所有能完整闭合的对象，再只保留最外层的那些
    （被包含在其他对象里的会被丢弃，避免同一内容重复出现）。
    """
    fenced = re.search(r"```(?:json)?\s*(.+)", text, re.DOTALL)
    body = fenced.group(1) if fenced else text

    found: list[tuple[int, int, dict[str, Any]]] = []
    stack: list[int] = []
    in_string = False
    escaped = False

    for idx, ch in enumerate(body):
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            stack.append(idx)
        elif ch == "}" and stack:
            begin = stack.pop()
            try:
                parsed = json.loads(body[begin : idx + 1])
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                found.append((begin, idx, parsed))

    # 只保留最外层对象：若某对象的区间被另一个对象包含，则丢弃它
    outermost = [
        (b, e, obj)
        for (b, e, obj) in found
        if not any(ob < b and oe > e for (ob, oe, _) in found)
    ]
    objects = [obj for (_, _, obj) in sorted(outermost, key=lambda t: t[0])]

    # 完整响应形如 {"rebuttals": [...]}：展开其中的对象列表
    if len(objects) == 1:
        for value in objects[0].values():
            if isinstance(value, list) and value and all(isinstance(v, dict) for v in value):
                return value
    return objects


def parse_json_block(text: str) -> dict[str, Any]:
    fenced = re.search(r"```(?:json)?\s*(.+?)\s*```", text, re.DOTALL)
    candidate = fenced.group(1) if fenced else text
    candidate = candidate.strip()
    if not candidate.startswith(("{", "[")):
        brace = candidate.find("{")
        bracket = candidate.find("[")
        starts = [i for i in (brace, bracket) if i >= 0]
        if starts:
            candidate = candidate[min(starts) :]
    try:
        return json.loads(candidate)
    except json.JSONDecodeError as err:
        raise LLMResponseFormatError(f"模型未返回可解析 JSON：{text[:200]}") from err
