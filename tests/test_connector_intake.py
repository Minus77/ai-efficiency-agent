"""Task 3：L1 拉取接入诊断（§4 双轨并行、§3.1 冲突裁决）。

核心性质：
- L1 拉取结果能转成 ParsedMaterial，被 derive_scenarios 直接消费
- L1 证据类型为 system_data（A 级），与 L0 手工导出并行而非二选一
- L1 与 L0 对同一活动偏差 > 30% → 标冲突转人工，不取均值
- L1 拉取失败不阻断诊断，降级为显式缺口
"""
import pytest

from aiea.clients import ClientRegistry
from aiea.connectors.base import CredentialRef
from aiea.connector_intake import (
    ConnectorBinding,
    cross_check_l0_l1,
    list_bindings,
    pull_to_material,
    save_binding,
    sync_connector,
)
from aiea.derive import derive_scenarios
from aiea.intake import parse_bytes
from aiea.models import EvidenceGrade, ResultCode, SourceType


@pytest.fixture
def client(tmp_path):
    reg = ClientRegistry(root=tmp_path)
    c = reg.create(name="连接器客户", industry="建材分销", headcount=70, departments=["客服"])
    return tmp_path, c.slug


# ---------------------------- 拉取 → ParsedMaterial ----------------------------
def test_pull_converts_to_parsed_material(client):
    root, slug = client
    pm, meta = pull_to_material(
        key="ticketing_readonly", tenant=slug, credential=CredentialRef("t", "k", "s")
    )
    assert pm is not None
    assert pm.kind == "csv"
    assert pm.row_count > 100
    assert pm.timestamp_columns, "工单连接器应提供时间戳列"
    assert meta["evidence_grade"] == "A"
    assert meta["source_kind"] == "L1"


def test_derived_scenarios_consume_connector_material(client):
    root, slug = client
    pm, _ = pull_to_material(
        key="ticketing_readonly", tenant=slug, credential=CredentialRef("t", "k", "s")
    )
    out = derive_scenarios([pm])
    assert out["cards"], "L1 材料必须能推出场景"
    top = out["cards"][0]
    assert top["evidence_grade"] == "A"
    assert top["monthly_minutes"] > 0
    assert top["evidence_refs"]


def test_im_connector_material_cannot_be_quantified(client):
    """IM 只有汇总，推出的场景必须零金额。"""
    root, slug = client
    pm, meta = pull_to_material(
        key="im_readonly", tenant=slug, credential=CredentialRef("i", "k", "s")
    )
    assert meta["evidence_grade"] == "C"
    out = derive_scenarios([pm])
    for card in out["cards"]:
        assert card["monthly_minutes"] == 0
        assert card["evidence_grade"] == "C"


# ---------------------------- 绑定持久化 ----------------------------
def test_save_and_list_binding_without_secret(client):
    root, slug = client
    rec = save_binding(
        root=root, slug=slug, key="ticketing_readonly",
        credential=CredentialRef("ticketing", "kid-1", "SECRET-ABC"),
    )
    assert rec["key"] == "ticketing_readonly"
    assert "SECRET-ABC" not in str(rec)
    items = list_bindings(root=root, slug=slug)
    assert len(items) == 1
    assert items[0]["credential"]["secret_present"] is True
    assert "SECRET-ABC" not in str(items[0])


def test_binding_rejects_unknown_connector(client):
    root, slug = client
    with pytest.raises(KeyError):
        save_binding(root=root, slug=slug, key="nope", credential=CredentialRef("x", "k", "s"))


def test_binding_is_per_tenant(client, tmp_path):
    root, slug = client
    reg = ClientRegistry(root=root)
    other = reg.create(name="另一个客户", industry="零售", headcount=40, departments=["客服"])
    save_binding(root=root, slug=slug, key="crm_readonly", credential=CredentialRef("c", "k", "s"))
    assert len(list_bindings(root=root, slug=slug)) == 1
    assert len(list_bindings(root=root, slug=other.slug)) == 0


# ---------------------------- 同步落盘 ----------------------------
def test_sync_writes_material_and_records_evidence_grade(client):
    root, slug = client
    save_binding(root=root, slug=slug, key="ticketing_readonly",
                 credential=CredentialRef("t", "kid", "s"))
    result = sync_connector(root=root, slug=slug, key="ticketing_readonly")
    assert result["ok"] is True
    assert result["row_count"] > 100
    assert result["evidence_grade"] == "A"
    stored = (root / slug / "materials" / result["stored_as"])
    assert stored.exists()
    # 落盘内容应可被常规解析路径读回
    pm = parse_bytes(stored.read_bytes(), filename=result["stored_as"])
    assert pm.row_count == result["row_count"]


def test_sync_unbound_connector_is_rejected(client):
    root, slug = client
    r = sync_connector(root=root, slug=slug, key="ticketing_readonly")
    assert r["ok"] is False
    assert "未绑定" in r["note"]


def test_sync_failure_degrades_to_gap_not_crash(client, monkeypatch):
    """上游不可达时必须降级为缺口，而不是抛栈中断整次诊断。

    注意打补丁的位置：build_connector 用的是注册表里在导入时就捕获的工厂，
    改 presets 模块属性没有效果——必须替换 _REGISTRY 里的那一项才算真正
    模拟到故障路径。
    """
    from aiea.connectors import base as cbase

    root, slug = client
    save_binding(root=root, slug=slug, key="ticketing_readonly",
                 credential=CredentialRef("t", "kid", "s"))

    def boom(*, tenant, since, limit):
        raise ConnectionError("upstream unreachable")

    spec, _ = cbase._REGISTRY["ticketing_readonly"]
    monkeypatch.setitem(cbase._REGISTRY, "ticketing_readonly", (spec, lambda: boom))

    r = sync_connector(root=root, slug=slug, key="ticketing_readonly")
    assert r["ok"] is False
    assert r["next_action"], "失败必须给可执行下一步"
    assert "L0" in r["next_action"] or "导出" in r["next_action"]


def test_injection_from_upstream_is_flagged_on_sync(client):
    root, slug = client
    save_binding(root=root, slug=slug, key="demo_injection",
                 credential=CredentialRef("d", "kid", "s"))
    r = sync_connector(root=root, slug=slug, key="demo_injection")
    assert r["ok"] is True
    assert r["injection_suspected"] is True
    assert r["treated_as_instruction"] is False


# ---------------------------- L0 / L1 交叉互校 ----------------------------
def test_l1_beats_l0_self_report_in_adjudication():
    """§3.1 裁决序：系统数据 > 单方自述。"""
    r = cross_check_l0_l1(
        activity="客服转录",
        l0_value=60.0, l0_source=SourceType.SELF_REPORT, l0_origin="客服主管口述",
        l1_value=180.0, l1_origin="工单系统只读 API",
    )
    assert r["chosen_value"] == 180.0
    assert r["chosen_source"] == SourceType.SYSTEM_DATA.value
    assert r["evidence_grade"] == "A"


def test_large_divergence_flags_conflict_and_forbids_mean():
    r = cross_check_l0_l1(
        activity="对账比对",
        l0_value=100.0, l0_source=SourceType.SUPPLEMENT_FORM, l0_origin="补数表",
        l1_value=180.0, l1_origin="ERP 只读视图",
    )
    assert r["conflict"] is True
    assert r["requires_human"] is True
    assert r["chosen_value"] != pytest.approx(140.0), "禁止取均值掩盖分歧"
    assert "均值" in r["note"] or "人工" in r["note"]


def test_small_divergence_is_not_conflict():
    r = cross_check_l0_l1(
        activity="开票整理",
        l0_value=100.0, l0_source=SourceType.SUPPLEMENT_FORM, l0_origin="补数表",
        l1_value=108.0, l1_origin="商城只读 API",
    )
    assert r["conflict"] is False
    assert r["requires_human"] is False


def test_cross_check_requires_both_sides():
    r = cross_check_l0_l1(
        activity="仅有一路", l0_value=None, l0_source=None, l0_origin="",
        l1_value=180.0, l1_origin="工单 API",
    )
    assert r["code"] == ResultCode.INSUFFICIENT_DATA.value
    assert r["conflict"] is False
    assert "单路" in r["note"] or "无法交叉" in r["note"]
