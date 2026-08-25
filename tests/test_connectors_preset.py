"""Task 2：预置连接器的能力声明必须诚实（§4、§13.4）。

最重要的性质：**声明的等级上限不能超过实际能拿到的粒度**。
把 IM 声明成 A 级会让下游误以为能量化，最终变成 ROI 幻觉。
"""
import pytest

from aiea.connectors import build_connector, get_spec, list_specs
from aiea.connectors.base import CredentialRef
from aiea.models import EvidenceGrade

PRODUCTION_KEYS = [
    "ticketing_readonly",
    "crm_readonly",
    "im_readonly",
    "erp_readonly",
    "ecommerce_readonly",
]


def _conn(key: str, tenant: str = "t1"):
    return build_connector(key, tenant=tenant, credential=CredentialRef(key, "k", "s"))


def test_five_production_connectors_registered():
    keys = {s.key for s in list_specs()}
    for k in PRODUCTION_KEYS:
        assert k in keys, f"缺少预置连接器 {k}"


@pytest.mark.parametrize("key", PRODUCTION_KEYS)
def test_every_spec_declares_metrics_and_limits(key):
    spec = get_spec(key)
    assert spec.metrics, f"{key} 必须声明能算哪些过程指标"
    assert spec.known_limits, f"{key} 必须写明拿不到什么"
    assert spec.auth_hint
    assert spec.scopes
    # 只读：scope 里不得出现写权限
    for scope in spec.scopes:
        assert not any(w in scope.lower() for w in ("write", "delete", "admin"))


@pytest.mark.parametrize("key", PRODUCTION_KEYS)
def test_declared_grade_is_not_higher_than_actual_data(key):
    """核心诚实性检查：声明 A 级就必须真的拿到时间戳。"""
    spec = get_spec(key)
    r = _conn(key).pull(requested_tenant="t1")
    assert r.ok
    if spec.max_evidence_grade is EvidenceGrade.A:
        assert r.timestamp_columns, f"{key} 声明 A 级却没有时间戳列"
        assert r.evidence_grade is EvidenceGrade.A
    else:
        # 非 A 级的连接器实际等级不得被抬高
        assert r.evidence_grade is not EvidenceGrade.A


def test_im_connector_is_honest_about_being_summary_only():
    """IM 拿不到批量明细，这是真实平台限制，必须声明为 C 级。"""
    spec = get_spec("im_readonly")
    assert spec.max_evidence_grade is EvidenceGrade.C
    assert spec.provides_timestamps is False
    assert "批量导出" in spec.known_limits or "汇总" in spec.known_limits
    r = _conn("im_readonly").pull(requested_tenant="t1")
    assert r.evidence_grade is EvidenceGrade.C
    assert r.timestamp_columns == []


def test_erp_connector_caps_at_grade_b():
    spec = get_spec("erp_readonly")
    assert spec.max_evidence_grade is EvidenceGrade.B
    r = _conn("erp_readonly").pull(requested_tenant="t1")
    assert r.evidence_grade is EvidenceGrade.B


def test_ticketing_returns_dual_timestamps_for_duration_math():
    r = _conn("ticketing_readonly").pull(requested_tenant="t1")
    assert len(r.timestamp_columns) >= 2, "需要成对时间戳才能推算单次耗时"
    assert "created_at" in r.columns
    assert "first_response_at" in r.columns
    assert r.row_count > 100


def test_crm_captures_evening_batch_pattern():
    r = _conn("crm_readonly").pull(requested_tenant="t1")
    assert r.row_count > 100
    hours = {int(row["logged_at"][11:13]) for row in r.rows}
    # 销售傍晚集中补录：小时分布应集中而非铺满全天
    assert len(hours) <= 4, f"应体现集中补录，实际跨 {len(hours)} 个小时"


def test_ecommerce_provides_reconcile_signal():
    r = _conn("ecommerce_readonly").pull(requested_tenant="t1")
    assert "reconcile_mismatch" in r.columns
    assert "invoice_required" in r.columns


def test_pull_is_deterministic_for_same_tenant():
    a = _conn("ticketing_readonly", "same").pull(requested_tenant="same")
    b = _conn("ticketing_readonly", "same").pull(requested_tenant="same")
    assert a.rows == b.rows, "固定种子应保证可复现，便于测试与回归"


def test_different_tenants_get_different_data():
    a = _conn("ticketing_readonly", "t-a").pull(requested_tenant="t-a")
    b = _conn("ticketing_readonly", "t-b").pull(requested_tenant="t-b")
    assert a.rows != b.rows


def test_limit_is_respected():
    c = _conn("ticketing_readonly")
    r = c.pull(requested_tenant="t1", limit=25)
    assert r.row_count <= 25


@pytest.mark.parametrize("key", PRODUCTION_KEYS)
def test_write_is_denied_on_every_production_connector(key):
    assert _conn(key).execute(operation="update", resource="anything").ok is False


@pytest.mark.parametrize("key", PRODUCTION_KEYS)
def test_credential_secret_never_leaks_from_connector(key):
    c = build_connector(key, tenant="t1", credential=CredentialRef(key, "kid", "TOP-SECRET-XYZ"))
    assert "TOP-SECRET-XYZ" not in repr(c.credential)
    assert "TOP-SECRET-XYZ" not in str(c.credential.to_dict())
    r = c.pull(requested_tenant="t1")
    assert "TOP-SECRET-XYZ" not in str(r.to_meta())


def test_metrics_are_process_metrics_not_business_outcomes():
    """§19.4：只采信过程指标。连接器声明里不得出现营收类指标。"""
    banned = ("营收", "利润", "毛利", "revenue", "profit", "人力成本占比")
    for spec in list_specs():
        for m in spec.metrics:
            assert not any(b in m.lower() or b in m for b in banned), f"{spec.key} 声明了经营结果指标 {m}"
