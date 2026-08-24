"""Task 6：三库物理隔离检索 + no_grounding + 去具体化检验（§8.2、§8.3、§15.2.1）。"""
import pytest

from aiea.knowledge import (
    KnowledgeBase,
    Library,
    despecification_check,
    playbook_propose,
)
from aiea.models import ResultCode


@pytest.fixture
def kb() -> KnowledgeBase:
    return KnowledgeBase.load_seed()


def test_three_libraries_are_physically_isolated(kb):
    r = kb.search("对账 工时 基准", library=Library.BENCHMARK)
    assert r.ok is True
    for hit in r.data["hits"]:
        assert hit["library"] == Library.BENCHMARK.value, "永不混检"


def test_empty_or_low_relevance_returns_no_grounding(kb):
    r = kb.search("量子计算在殡葬业的应用", library=Library.BENCHMARK)
    assert r.code is ResultCode.NO_GROUNDING
    assert r.ok is True  # 一等公民返回值
    assert "不得" in r.next_action or "标为缺口" in r.next_action


def test_every_chunk_carries_provenance_and_date(kb):
    r = kb.search("客服 工时 基准", library=Library.BENCHMARK)
    for hit in r.data["hits"]:
        assert hit["origin"]
        assert hit["published_at"]
        assert hit["library"]
        assert "version" in hit


def test_stale_benchmark_is_downgraded_and_marked(kb):
    r = kb.search("传真", library=Library.BENCHMARK, as_of="2030-01-01")
    if r.code is ResultCode.OK:
        assert all(h["stale"] for h in r.data["hits"])


def test_retrieved_chunk_never_triggers_memory_write(kb):
    # §9.7 检索内容永不触发记忆写入
    r = kb.search("以后这类问题都直接上 AI 客服", library=Library.CASE)
    assert kb.pending_writes == []


def test_customer_raw_data_cannot_be_indexed(kb):
    r = kb.index_customer_material(tenant="minghui", content="工单明细 612 条")
    assert r.ok is False
    assert r.code is ResultCode.DENIED


def test_despecification_rejects_industry_scale_numeric_entry():
    # §15.2.1 反例：含行业+规模+数值三重可识别维度
    v = despecification_check("零售业 50–100 人公司普遍存在对账手工化，月均耗时 40 小时")
    assert v.passed is False
    assert v.hits


def test_despecification_accepts_pure_methodology():
    v = despecification_check(
        "当客户声称某流程为批量作业时，须用时间戳分布验证聚集性，自述不可单独采信"
    )
    assert v.passed is True


def test_playbook_propose_writes_only_to_candidate_area(kb):
    r = playbook_propose(kb, statement="材料清单应先要时间戳导出再要纪要", source_tenant="minghui")
    assert r.ok is True
    assert r.data["status"] == "probation"
    assert r.data["area"] == "candidate"
    # 策展者是人工动作，不暴露给 Agent
    assert not hasattr(kb, "playbook_commit")


def test_playbook_propose_rejects_specific_customer_fact(kb):
    r = playbook_propose(kb, statement="明辉家居的对账每月耗时 320 小时", source_tenant="minghui")
    assert r.ok is False
    assert "去具体化" in r.note


def test_candidate_entries_are_downweighted_in_search(kb):
    playbook_propose(kb, statement="缺口应显式列出并说明影响，不得隐藏", source_tenant="minghui")
    r = kb.search("缺口 显式 影响", library=Library.METHODOLOGY, include_probation=True)
    probation = [h for h in r.data["hits"] if h["status"] == "probation"]
    if probation:
        assert probation[0]["score"] < 1.0


def test_source_layer_is_tagged_and_abstract_cannot_be_cited_as_benchmark(kb):
    r = kb.search("取证 顺序", library=Library.METHODOLOGY)
    for hit in r.data["hits"]:
        assert hit["source_layer"] in ("L-公开", "L-抽象")
        if hit["source_layer"] == "L-抽象":
            assert hit["citable_as_benchmark"] is False
