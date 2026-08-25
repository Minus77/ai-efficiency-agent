"""Task 1：连接器框架（§4 只读双轨、§13.3 凭据边界）。

三条纪律在框架层强制，任何具体连接器都绕不过：
1. 只读——写方法一律 denied，不靠实现者自觉
2. 凭据只存引用，明文永不进上下文/日志/序列化结果
3. 每次拉取带 tenant 过滤 + 速率配额 + 注入检测
"""
import pytest

from aiea.connectors.base import (
    ConnectorSpec,
    CredentialRef,
    PullQuota,
    PullResult,
    build_connector,
    list_specs,
    register,
)
from aiea.models import EvidenceGrade, ResultCode


# ---------------------------- 凭据边界 ----------------------------
def test_credential_ref_never_exposes_secret():
    ref = CredentialRef(provider="ticketing", key_id="tk-001", secret="super-secret-token")
    dumped = ref.to_dict()
    assert "super-secret-token" not in str(dumped)
    assert dumped["key_id"] == "tk-001"
    assert dumped["secret_present"] is True
    # repr 也不能泄漏——日志里最常见的泄漏路径
    assert "super-secret-token" not in repr(ref)


def test_credential_ref_marks_absent_secret():
    ref = CredentialRef(provider="crm", key_id="c-1", secret="")
    assert ref.to_dict()["secret_present"] is False


# ---------------------------- 只读断言 ----------------------------
def test_write_operation_is_denied_at_framework_level():
    c = build_connector("demo_ticketing", tenant="t1", credential=CredentialRef("demo", "k", "s"))
    r = c.execute(operation="update", resource="tickets")
    assert r.ok is False
    assert r.code is ResultCode.DENIED
    assert "只读" in r.note


def test_read_operation_is_allowed():
    c = build_connector("demo_ticketing", tenant="t1", credential=CredentialRef("demo", "k", "s"))
    r = c.execute(operation="read", resource="tickets")
    assert r.ok is True


@pytest.mark.parametrize("op", ["write", "update", "insert", "delete", "patch", "post"])
def test_all_write_verbs_are_denied(op):
    c = build_connector("demo_ticketing", tenant="t1", credential=CredentialRef("demo", "k", "s"))
    assert c.execute(operation=op, resource="tickets").ok is False


# ---------------------------- 能力声明 ----------------------------
def test_spec_declares_capabilities_and_limits():
    spec = next(s for s in list_specs() if s.key == "demo_ticketing")
    assert spec.provides_timestamps is True
    assert spec.max_evidence_grade is EvidenceGrade.A
    assert spec.metrics, "必须声明能算哪些过程指标，否则效果衡量无从下手"
    assert spec.known_limits, "必须写明边界"
    assert spec.category


def test_specs_declare_grade_ceiling_honestly():
    """IM 类系统拿不到批量明细，等级上限必须诚实声明为 C。"""
    im = next(s for s in list_specs() if s.category == "IM")
    assert im.max_evidence_grade is EvidenceGrade.C
    assert im.provides_timestamps is False


# ---------------------------- tenant 隔离 ----------------------------
def test_cross_tenant_pull_is_denied():
    c = build_connector("demo_ticketing", tenant="t1", credential=CredentialRef("demo", "k", "s"))
    r = c.pull(requested_tenant="other-tenant")
    assert r.ok is False
    assert r.code is ResultCode.DENIED


def test_same_tenant_pull_succeeds():
    c = build_connector("demo_ticketing", tenant="t1", credential=CredentialRef("demo", "k", "s"))
    r = c.pull(requested_tenant="t1")
    assert r.ok is True
    assert r.rows
    assert r.evidence_grade is EvidenceGrade.A


# ---------------------------- 速率配额 ----------------------------
def test_quota_exhaustion_returns_structured_error():
    c = build_connector(
        "demo_ticketing", tenant="t1", credential=CredentialRef("demo", "k", "s"),
        quota=PullQuota(max_pulls_per_hour=2),
    )
    assert c.pull(requested_tenant="t1").ok is True
    assert c.pull(requested_tenant="t1").ok is True
    third = c.pull(requested_tenant="t1")
    assert third.ok is False
    assert "配额" in third.note
    assert third.next_action


# ---------------------------- 注入检测 ----------------------------
def test_injected_content_from_upstream_is_flagged_not_executed():
    c = build_connector(
        "demo_injection", tenant="t1", credential=CredentialRef("demo", "k", "s")
    )
    r = c.pull(requested_tenant="t1")
    assert r.ok is True
    assert r.injection_suspected is True
    assert r.treated_as_instruction is False


# ---------------------------- 注册表 ----------------------------
def test_unknown_connector_raises_clear_error():
    with pytest.raises(KeyError) as exc:
        build_connector("no-such-thing", tenant="t1", credential=CredentialRef("d", "k", "s"))
    assert "no-such-thing" in str(exc.value)


def test_register_rejects_duplicate_key():
    spec = ConnectorSpec(
        key="demo_ticketing", name="重复", category="工单",
        max_evidence_grade=EvidenceGrade.A, provides_timestamps=True,
        metrics=["x"], known_limits="y",
    )
    with pytest.raises(ValueError):
        register(spec, lambda **kw: None)


def test_pull_result_carries_provenance():
    c = build_connector("demo_ticketing", tenant="t1", credential=CredentialRef("demo", "k", "s"))
    r = c.pull(requested_tenant="t1")
    assert isinstance(r, PullResult)
    assert r.source_name
    assert r.pulled_at
    assert r.row_count == len(r.rows)
    assert r.columns
