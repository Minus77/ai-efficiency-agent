"""Task 7：OTel GenAI 语义约定 + 自然语言化观测层（§10）。"""
import json

import pytest

from aiea.telemetry import Tracer, daily_brief, customer_progress_line, translate_alert


@pytest.fixture
def tracer(tmp_path):
    return Tracer(session_id="sess-1", tenant="minghui", out_dir=tmp_path)


def test_spans_use_gen_ai_semantic_conventions(tracer):
    with tracer.span("llm", name="scenario_split", attrs={"gen_ai.request.model": "claude-sonnet-4-5"}) as s:
        s.set("gen_ai.usage.input_tokens", 1200)
        s.set("gen_ai.usage.output_tokens", 300)
    rec = tracer.records[-1]
    assert rec["attributes"]["gen_ai.request.model"] == "claude-sonnet-4-5"
    assert rec["attributes"]["gen_ai.usage.input_tokens"] == 1200
    assert rec["kind"] == "llm"
    assert rec["gen_ai.session.id"] == "sess-1"


def test_tool_span_hashes_arguments_not_stores_raw(tracer):
    tracer.tool_call("document_forensics", arguments={"path": "tickets.csv", "phone": "13812345678"}, result_size=940, status="ok")
    rec = tracer.records[-1]
    assert "13812345678" not in json.dumps(rec, ensure_ascii=False)
    assert rec["attributes"]["gen_ai.tool.name"] == "document_forensics"
    assert len(rec["attributes"]["tool.call.arguments_hash"]) == 16


def test_events_are_persisted_as_jsonl(tracer, tmp_path):
    tracer.event("guardrail_triggered", {"layer": "工具出参", "action": "降级为不可信数据"})
    tracer.flush()
    lines = (tmp_path / "trace-sess-1.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert any(json.loads(l)["name"] == "guardrail_triggered" for l in lines)


def test_hallucination_signals_are_first_class_metrics(tracer):
    tracer.event("no_grounding", {"library": "benchmark"})
    tracer.event("insufficient_data", {"tool": "metric_probe"})
    m = tracer.metrics()
    assert m["no_grounding_count"] == 1
    assert m["insufficient_data_count"] == 1
    # §10.5：触发率过低反而可疑
    assert "honesty_signal_rate" in m


def test_daily_brief_is_natural_language_without_internal_jargon():
    brief = daily_brief([
        {
            "client": "明辉家居建材",
            "scenarios_total": 8,
            "scenarios_solid": 5,
            "gaps": ["销售台账导出"],
            "cost_usd": 1.42,
            "avg_cost_usd": 1.10,
            "no_grounding": 1,
            "conflicts": 1,
        }
    ])
    for jargon in ("insufficient_data", "metric_probe", "no_grounding", "span", "token"):
        assert jargon not in brief
    assert "明辉家居建材" in brief
    assert "销售台账" in brief


def test_customer_progress_hides_internal_logic():
    line = customer_progress_line(done=["客服", "财务"], pending=["销售跟单"])
    assert "客服" in line and "销售跟单" in line
    for leak in ("S3", "S4", "tool", "rubric", "置信度"):
        assert leak not in line


def test_alerts_are_translated_to_plain_language():
    msg = translate_alert("faithfulness_score", value=0.62, threshold=0.7, details={"unsupported_sentences": 2, "section": 3})
    assert "faithfulness" not in msg
    assert "2 句" in msg and "第 3 节" in msg


def test_cost_metrics_split_by_stage(tracer):
    tracer.record_cost(stage="S2", usd=0.4, cache_read_tokens=800, input_tokens=2000)
    tracer.record_cost(stage="S4", usd=0.2, cache_read_tokens=1200, input_tokens=1500)
    m = tracer.metrics()
    assert m["cost_by_stage"]["S2"] == 0.4
    assert 0.0 < m["cache_hit_rate"] < 1.0
