"""厂商模板：具体产品而非抽象类别。

顾问接客户时听到的是"我们用钉钉"，不是"我们用企业 IM"。
因此连接器必须能按产品名找到，且每个产品的能力边界要**分别**声明——
钉钉和飞书的审批 API 开放程度不同，混成一个"OA"会丢掉这个差异。

诚实性要求（比抽象版更严）：
- scope 必须是该产品真实存在的权限名，不能编
- 未经核对的字段标 unverified，界面上要能看出来
"""
import pytest

from aiea.connectors import build_connector, get_spec, list_specs
from aiea.connectors.base import CredentialRef
from aiea.models import EvidenceGrade


def _vendor_specs():
    return [s for s in list_specs() if s.vendor]


# ---------------------------- 新增字段 ----------------------------
def test_spec_has_vendor_fields():
    spec = get_spec("im_dingtalk")
    assert spec is not None
    assert spec.vendor == "钉钉"
    assert spec.product
    assert spec.docs_url, "必须给官方文档链接，顾问要去申请权限"
    assert spec.category == "IM"


def test_vendor_appears_in_serialized_form():
    d = get_spec("im_dingtalk").to_dict()
    for key in ("vendor", "product", "docs_url", "verified", "setup_steps"):
        assert key in d, f"to_dict 缺少 {key}，前端无从渲染"


def test_unverified_capability_is_flagged():
    """未经官方文档核对的能力声明必须标出来，不能让人误以为已验证。"""
    for spec in _vendor_specs():
        assert isinstance(spec.verified, bool)
        if not spec.verified:
            assert spec.verify_note, "标为未核对时必须说明待核对什么"


# ---------------------------- IM 三家 ----------------------------
@pytest.mark.parametrize("key,vendor", [
    ("im_wecom", "企业微信"),
    ("im_dingtalk", "钉钉"),
    ("im_feishu", "飞书"),
])
def test_im_vendors_registered(key, vendor):
    spec = get_spec(key)
    assert spec is not None, f"缺少 {vendor} 连接器"
    assert spec.vendor == vendor
    assert spec.category == "IM"


def test_im_vendors_are_honest_about_message_export():
    """三家都不开放聊天记录批量导出——这是产品事实，不能因为想抬高等级而含糊。"""
    for key in ("im_wecom", "im_dingtalk", "im_feishu"):
        spec = get_spec(key)
        assert spec.max_evidence_grade is EvidenceGrade.C, f"{key} 不应声明高于 C 级"
        assert spec.provides_timestamps is False
        assert "导出" in spec.known_limits or "明细" in spec.known_limits


def test_im_pull_returns_summary_only():
    for key in ("im_wecom", "im_dingtalk", "im_feishu"):
        r = build_connector(key, tenant="t1", credential=CredentialRef(key, "k", "s")).pull(
            requested_tenant="t1"
        )
        assert r.ok
        assert r.evidence_grade is EvidenceGrade.C
        assert r.timestamp_columns == []


# ---------------------------- OA 审批（可量化，与 IM 分开） ----------------------------
@pytest.mark.parametrize("key", ["oa_dingtalk_approval", "oa_feishu_approval", "oa_wecom_approval"])
def test_oa_approval_connectors_can_be_quantified(key):
    """审批流与聊天记录不同：审批实例有明确的提交/通过时间戳，可达 A 级。

    把它们混进"IM"会丢掉这个关键差异——这正是按产品建模的价值。
    """
    spec = get_spec(key)
    assert spec is not None
    assert spec.category == "OA审批"
    assert spec.max_evidence_grade is EvidenceGrade.A
    assert spec.provides_timestamps is True
    r = build_connector(key, tenant="t1", credential=CredentialRef(key, "k", "s")).pull(
        requested_tenant="t1"
    )
    assert r.timestamp_columns, f"{key} 声明 A 级必须真的返回时间戳"
    assert r.evidence_grade is EvidenceGrade.A


# ---------------------------- 工单 ----------------------------
@pytest.mark.parametrize("key,vendor", [
    ("ticketing_zendesk", "Zendesk"),
    ("ticketing_udesk", "Udesk"),
    ("ticketing_jira_sm", "Jira Service Management"),
])
def test_ticketing_vendors(key, vendor):
    spec = get_spec(key)
    assert spec is not None
    assert spec.vendor == vendor
    assert spec.max_evidence_grade is EvidenceGrade.A
    r = build_connector(key, tenant="t1", credential=CredentialRef(key, "k", "s")).pull(
        requested_tenant="t1"
    )
    assert len(r.timestamp_columns) >= 2, "工单需要成对时间戳才能推算耗时"


# ---------------------------- CRM ----------------------------
@pytest.mark.parametrize("key,vendor", [
    ("crm_salesforce", "Salesforce"),
    ("crm_xiaoshouyi", "销售易"),
    ("crm_hubspot", "HubSpot"),
])
def test_crm_vendors(key, vendor):
    spec = get_spec(key)
    assert spec is not None
    assert spec.vendor == vendor
    assert spec.category == "CRM"


# ---------------------------- ERP / 进销存 ----------------------------
@pytest.mark.parametrize("key,vendor", [
    ("erp_kingdee", "金蝶"),
    ("erp_yonyou", "用友"),
    ("erp_guanjia", "管家婆"),
])
def test_erp_vendors_cap_at_b_without_timestamps(key, vendor):
    spec = get_spec(key)
    assert spec is not None
    assert spec.vendor == vendor
    assert spec.max_evidence_grade in (EvidenceGrade.A, EvidenceGrade.B)
    r = build_connector(key, tenant="t1", credential=CredentialRef(key, "k", "s")).pull(
        requested_tenant="t1"
    )
    if not spec.provides_timestamps:
        assert r.evidence_grade is not EvidenceGrade.A


# ---------------------------- 电商 ----------------------------
@pytest.mark.parametrize("key,vendor", [
    ("ecom_youzan", "有赞"),
    ("ecom_weimob", "微盟"),
    ("ecom_taobao", "淘宝/天猫"),
    ("ecom_shopify", "Shopify"),
])
def test_ecommerce_vendors(key, vendor):
    spec = get_spec(key)
    assert spec is not None
    assert spec.vendor == vendor
    assert spec.category == "电商"


# ---------------------------- 全局纪律 ----------------------------
def test_every_vendor_spec_declares_setup_steps():
    """顾问要照着做才能拿到只读权限，光给 docs_url 不够。"""
    for spec in _vendor_specs():
        assert spec.setup_steps, f"{spec.key} 缺少接入步骤"
        assert len(spec.setup_steps) >= 2


def test_no_vendor_scope_requests_write_access():
    for spec in _vendor_specs():
        for scope in spec.scopes:
            low = scope.lower()
            for bad in ("write", "update", "delete", "admin", "manage", "modify"):
                assert bad not in low, f"{spec.key} 的 scope {scope} 含写权限"


def test_vendor_metrics_are_process_metrics_only():
    banned = ("营收", "利润", "毛利", "revenue", "profit", "人力成本占比")
    for spec in _vendor_specs():
        for m in spec.metrics:
            assert not any(b in m or b in m.lower() for b in banned), f"{spec.key}: {m}"


def test_declared_grade_matches_actual_data_for_all_vendors():
    """全局诚实性回归：声明 A 级就必须真的拿到时间戳。"""
    for spec in _vendor_specs():
        r = build_connector(
            spec.key, tenant="t1", credential=CredentialRef(spec.key, "k", "s")
        ).pull(requested_tenant="t1")
        assert r.ok, f"{spec.key} 拉取失败"
        if spec.max_evidence_grade is EvidenceGrade.A:
            assert r.timestamp_columns, f"{spec.key} 声明 A 级却无时间戳"
        if not spec.provides_timestamps:
            assert r.evidence_grade is not EvidenceGrade.A, f"{spec.key} 无时间戳却给了 A 级"


def test_write_denied_on_every_vendor_connector():
    for spec in _vendor_specs():
        c = build_connector(spec.key, tenant="t1", credential=CredentialRef(spec.key, "k", "s"))
        assert c.execute(operation="update", resource="x").ok is False


def test_vendor_count_covers_six_categories():
    cats = {s.category for s in _vendor_specs()}
    for expected in ("IM", "OA审批", "工单", "CRM", "ERP", "电商"):
        assert expected in cats, f"缺少 {expected} 类别的厂商模板"
    assert len(_vendor_specs()) >= 16
