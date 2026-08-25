"""厂商模板：市面常见产品的连接器（§4 L1 只读双轨）。

**为什么按产品而不是按类别建模：** 顾问接客户时听到的是"我们用钉钉"，
不是"我们用企业 IM"。更关键的是不同产品的 API 开放程度差别很大——
钉钉的聊天记录拿不到，但钉钉的**审批实例**有完整时间戳、可达 A 级。
混成一个"OA/IM"类别会把这个差异抹平，进而误判可量化性。

**诚实性纪律（比抽象版更严）：**
- `scopes` 写该产品真实存在的权限名，写不准就标 `verified=False`
- `verified=False` 时必须写 `verify_note`，说明待核对什么
- 声明 A 级 → 演示数据源必须真的返回时间戳（测试逐个校验）

演示数据源用固定种子，因此没有真实客户系统也能跑通全链路。
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta
from typing import Any

from ..models import EvidenceGrade
from .base import ConnectorSpec, register

SEED = 20260826
_BASE = datetime(2026, 7, 20, 8, 30)

# 三家 IM 平台的共同事实：都不开放聊天记录批量导出
_IM_LIMIT = (
    "聊天记录不开放批量导出（三家平台均如此），只能拿到会话量汇总。"
    "因此**只能用于定位痛点，不得用于量化**——与纪要类材料同级。"
    "若要量化沟通耗时，需改从工单/审批等有落库记录的环节入手。"
)
_IM_VERIFY = "会话统计接口的字段名与聚合粒度需按当前版本官方文档复核"


def _workday(i: int) -> datetime:
    d = _BASE
    added = 0
    while added < i:
        d += timedelta(days=1)
        if d.weekday() < 5:
            added += 1
    return d


def _rng(tenant: str, salt: int) -> random.Random:
    return random.Random(SEED + salt + (hash(tenant) % 1000))


# ===========================================================================
# IM：会话量汇总（C 级）
# ===========================================================================
def _im_source(salt: int):
    def source(*, tenant: str, since: str | None, limit: int) -> list[dict[str, Any]]:
        rng = _rng(tenant, salt)
        rows = []
        for month in ("2026-06", "2026-07", "2026-08"):
            rows.append(
                {
                    "统计月份": month,
                    "外部会话数": rng.randint(1800, 3400),
                    "内部会话数": rng.randint(2600, 5200),
                    "平均首响分钟": round(rng.uniform(5, 22), 1),
                }
            )
        return rows[:limit]

    return source


IM_VENDORS = [
    (
        "im_wecom", "企业微信", "企业微信 · 会话统计",
        ["externalcontact:get_statistics", "user:list"],
        "https://developer.work.weixin.qq.com/document/path/92132",
        [
            "在企业微信管理后台创建自建应用，记录 CorpID 与 Secret",
            "只勾选「客户联系 - 统计数据」等读取权限，不要勾任何写权限",
            "把服务器出口 IP 加入应用的可信 IP 白名单",
        ],
    ),
    (
        "im_dingtalk", "钉钉", "钉钉 · 会话与组织统计",
        ["qyapi_get_conversation_statistics", "contact.user.read"],
        "https://open.dingtalk.com/document/orgapp/queries-conversation-statistics",
        [
            "在钉钉开放平台创建企业内部应用，获取 AppKey 与 AppSecret",
            "申请「通讯录只读」与「会话统计只读」权限并等待管理员审批",
            "上线应用后用 AccessToken 调用统计接口",
        ],
    ),
    (
        "im_feishu", "飞书", "飞书 · 会话与消息统计",
        ["im:chat:readonly", "contact:user.base:readonly"],
        "https://open.feishu.cn/document/server-docs/im-v1/chat/list",
        [
            "在飞书开放平台创建企业自建应用，获取 App ID 与 App Secret",
            "在「权限管理」中只添加以 readonly 结尾的权限并发布版本",
            "由企业管理员审批通过后即可调用",
        ],
    ),
]


def _register_im() -> None:
    for salt, (key, vendor, product, scopes, docs, steps) in enumerate(IM_VENDORS):
        register(
            ConnectorSpec(
                key=key,
                name=f"{vendor}（会话统计，只读）",
                category="IM",
                vendor=vendor,
                product=product,
                docs_url=docs,
                setup_steps=steps,
                verified=False,
                verify_note=_IM_VERIFY,
                max_evidence_grade=EvidenceGrade.C,
                provides_timestamps=False,
                metrics=["会话量（仅汇总）", "平均首响时长（仅汇总）"],
                known_limits=_IM_LIMIT,
                scopes=scopes,
                auth_hint=f"{vendor}自建应用的只读凭据（CorpID/AppKey + Secret）",
                description="只能拿到会话量汇总，刻意声明为 C 级以防下游误以为能算工时",
            ),
            lambda s=salt: _im_source(s),
        )


# ===========================================================================
# OA 审批：审批实例有完整时间戳（A 级）—— 与 IM 的关键差异
# ===========================================================================
def _approval_source(salt: int):
    def source(*, tenant: str, since: str | None, limit: int) -> list[dict[str, Any]]:
        rng = _rng(tenant, salt)
        rows: list[dict[str, Any]] = []
        kinds = ["采购付款审批", "报销审批", "折扣申请", "合同审批", "开票申请"]
        iid = 70000
        for d in range(22):
            base = _workday(d)
            for _ in range(rng.randint(6, 12)):
                iid += 1
                submitted = base.replace(
                    hour=rng.randint(9, 17), minute=rng.randint(0, 59)
                )
                # 审批耗时：多数当天完成，少数隔天
                finished = submitted + timedelta(
                    minutes=rng.choice([18, 35, 52, 90, 140, 260, 620])
                )
                rows.append(
                    {
                        "instance_id": f"AP{iid}",
                        "process_name": rng.choice(kinds),
                        "submitted_at": submitted.isoformat(timespec="seconds"),
                        "finished_at": finished.isoformat(timespec="seconds"),
                        "approver_count": rng.randint(1, 4),
                        "result": rng.choices(["同意", "驳回"], weights=[92, 8])[0],
                    }
                )
                if len(rows) >= limit:
                    return rows
        return rows

    return source


APPROVAL_VENDORS = [
    (
        "oa_dingtalk_approval", "钉钉", "钉钉 · 审批（OA）",
        ["dingtalk.oapi.processinstance.listids", "processinstance.get"],
        "https://open.dingtalk.com/document/orgapp/obtain-the-list-of-approval-instance-ids",
        [
            "在钉钉开放平台申请「审批」相关只读权限",
            "记录需要分析的审批模板 process_code",
            "用 listids + get 组合拉取实例，仅读不写",
        ],
    ),
    (
        "oa_feishu_approval", "飞书", "飞书 · 审批",
        ["approval:approval:readonly", "approval:instance:readonly"],
        "https://open.feishu.cn/document/server-docs/approval-v4/instance/list",
        [
            "在飞书开放平台为应用添加审批只读权限并发布",
            "获取审批定义 approval_code",
            "调用实例列表接口按时间范围拉取",
        ],
    ),
    (
        "oa_wecom_approval", "企业微信", "企业微信 · 审批",
        ["oa:approval:get_sp_detail", "oa:approval:get_sp_no_list"],
        "https://developer.work.weixin.qq.com/document/path/91816",
        [
            "在企业微信后台开启「审批」应用的 API 接口",
            "获取审批应用的 Secret（只读用途）",
            "先拉审批单号列表，再逐单取详情",
        ],
    ),
]


def _register_approval() -> None:
    for salt, (key, vendor, product, scopes, docs, steps) in enumerate(APPROVAL_VENDORS):
        register(
            ConnectorSpec(
                key=key,
                name=f"{vendor}审批（只读）",
                category="OA审批",
                vendor=vendor,
                product=product,
                docs_url=docs,
                setup_steps=steps,
                verified=False,
                verify_note="审批实例字段名与分页参数需按当前版本官方文档复核",
                max_evidence_grade=EvidenceGrade.A,
                provides_timestamps=True,
                metrics=["该环节处理时长", "该环节处理单量", "该环节返工率"],
                known_limits=(
                    "只覆盖走了线上审批流的单据；线下签批与口头决策拿不到。"
                    "审批时长包含审批人等待时间，不等于经办人的操作耗时——"
                    "要区分两者需结合经办环节的其他记录。"
                ),
                scopes=scopes,
                auth_hint=f"{vendor}审批应用的只读凭据",
                description="审批实例有提交/完成双时间戳，可达 A 级——这是它与聊天记录的关键差异",
            ),
            lambda s=salt: _approval_source(s),
        )


# ===========================================================================
# 工单（A 级）
# ===========================================================================
def _ticket_source(salt: int, cats: list[str]):
    def source(*, tenant: str, since: str | None, limit: int) -> list[dict[str, Any]]:
        rng = _rng(tenant, salt)
        rows: list[dict[str, Any]] = []
        tid = 50000
        for d in range(22):
            base = _workday(d)
            for start_h, n in ((9, 13), (14, 10)):
                cursor = base.replace(hour=start_h, minute=10)
                for _ in range(n):
                    tid += 1
                    created = cursor + timedelta(minutes=rng.randint(1, 4))
                    cursor = created
                    rows.append(
                        {
                            "ticket_id": f"T{tid}",
                            "created_at": created.isoformat(timespec="seconds"),
                            "first_response_at": (
                                created + timedelta(minutes=rng.randint(2, 7))
                            ).isoformat(timespec="seconds"),
                            "solved_at": (
                                created + timedelta(hours=rng.randint(1, 30))
                            ).isoformat(timespec="seconds"),
                            "category": rng.choice(cats),
                            "channel": rng.choices(
                                ["微信", "电话", "邮件", "在线"], weights=[46, 22, 12, 20]
                            )[0],
                            "assignee": rng.choice(["王芳", "李静", "周敏"]),
                            "reopened": "是" if rng.random() < 0.06 else "否",
                        }
                    )
                    if len(rows) >= limit:
                        return rows
        return rows

    return source


TICKET_VENDORS = [
    (
        "ticketing_zendesk", "Zendesk", "Zendesk Support",
        ["tickets:read", "users:read"],
        "https://developer.zendesk.com/api-reference/ticketing/tickets/tickets/",
        [
            "在 Zendesk 后台启用 API 并创建 API Token",
            "用 email/token 方式认证（该 Token 仅需读权限）",
            "用 Incremental Export 接口按时间增量拉取",
        ],
        ["送货时间查询", "开票信息补录", "退换货登记", "安装售后"],
    ),
    (
        "ticketing_udesk", "Udesk", "Udesk 客服工单",
        ["ticket:read"],
        "https://www.udesk.cn/doc/apidoc",
        [
            "在 Udesk 管理后台获取 email 与 API Token",
            "确认开放平台已启用工单查询接口",
            "按更新时间分页拉取工单列表",
        ],
        ["咨询答复", "售后报修", "投诉处理", "开票协助"],
    ),
    (
        "ticketing_jira_sm", "Jira Service Management", "Jira Service Management",
        ["read:jira-work", "read:servicedesk-request"],
        "https://developer.atlassian.com/cloud/jira/service-desk/rest/",
        [
            "在 Atlassian 账号中创建 API Token",
            "确认该账号对目标项目只有浏览权限即可",
            "用 JQL 按 project 与时间范围检索请求",
        ],
        ["IT 支持", "内部申请", "故障报修", "变更请求"],
    ),
]


def _register_tickets() -> None:
    for salt, (key, vendor, product, scopes, docs, steps, cats) in enumerate(TICKET_VENDORS):
        register(
            ConnectorSpec(
                key=key,
                name=f"{vendor}（工单，只读）",
                category="工单",
                vendor=vendor,
                product=product,
                docs_url=docs,
                setup_steps=steps,
                verified=False,
                verify_note="字段名与分页方式需按当前版本官方文档复核",
                max_evidence_grade=EvidenceGrade.A,
                provides_timestamps=True,
                metrics=["该环节处理时长", "该环节处理单量", "首响时长", "该环节返工率"],
                known_limits=(
                    "只覆盖已落工单的记录；客户在 IM 里的原始对话不在其中，"
                    "因此「转录耗时」仍是用首响间隔近似，可能偏高。"
                ),
                scopes=scopes,
                auth_hint=f"{vendor} 的只读 API Token",
                description="工单含创建/首响/解决多个时间戳，是最容易拿到 A 级证据的来源",
            ),
            lambda s=salt, c=cats: _ticket_source(s, c),
        )


# ===========================================================================
# CRM（A 级）
# ===========================================================================
def _crm_source(salt: int):
    def source(*, tenant: str, since: str | None, limit: int) -> list[dict[str, Any]]:
        rng = _rng(tenant, salt)
        rows: list[dict[str, Any]] = []
        aid = 8000
        for d in range(22):
            base = _workday(d)
            cursor = base.replace(hour=17, minute=20)
            for _ in range(rng.randint(9, 15)):
                aid += 1
                logged = cursor + timedelta(minutes=rng.randint(2, 6))
                cursor = logged
                rows.append(
                    {
                        "activity_id": f"A{aid}",
                        "logged_at": logged.isoformat(timespec="seconds"),
                        "activity_type": rng.choices(
                            ["跟单信息录入", "报价查询", "客户需求登记", "回访记录"],
                            weights=[44, 24, 20, 12],
                        )[0],
                        "owner": rng.choice(["赵强", "孙磊", "吴倩"]),
                        "opportunity_amount": round(rng.uniform(800, 9500), 2),
                        "stage": rng.choice(["初步接触", "方案报价", "商务谈判", "已成交"]),
                    }
                )
                if len(rows) >= limit:
                    return rows
        return rows

    return source


CRM_VENDORS = [
    (
        "crm_salesforce", "Salesforce", "Salesforce Sales Cloud",
        ["api", "refresh_token"],
        "https://developer.salesforce.com/docs/atlas.en-us.api_rest.meta/api_rest/",
        [
            "在 Salesforce 中创建 Connected App 并启用 OAuth",
            "为集成用户配置只读 Profile（仅 Read 对象权限）",
            "用 SOQL 查询 Task/Event 等活动对象",
        ],
    ),
    (
        "crm_xiaoshouyi", "销售易", "销售易 CRM",
        ["crm:object:query"],
        "https://open.xiaoshouyi.com/",
        [
            "在销售易开放平台创建应用，获取 AppId 与 AppSecret",
            "申请对象查询（只读）权限",
            "用 OpenAPI 按对象与时间条件查询",
        ],
    ),
    (
        "crm_hubspot", "HubSpot", "HubSpot CRM",
        ["crm.objects.contacts.read", "crm.objects.deals.read"],
        "https://developers.hubspot.com/docs/api/crm/understanding-the-crm",
        [
            "创建 Private App 并只勾选 .read 类 scope",
            "复制生成的 Access Token",
            "用 CRM Objects Search API 按时间范围拉取",
        ],
    ),
]


def _register_crm() -> None:
    for salt, (key, vendor, product, scopes, docs, steps) in enumerate(CRM_VENDORS):
        register(
            ConnectorSpec(
                key=key,
                name=f"{vendor}（CRM，只读）",
                category="CRM",
                vendor=vendor,
                product=product,
                docs_url=docs,
                setup_steps=steps,
                verified=False,
                verify_note="对象名与字段名需按客户实际配置与当前 API 版本复核",
                max_evidence_grade=EvidenceGrade.A,
                provides_timestamps=True,
                metrics=["该环节处理单量", "该环节处理时长", "跟单响应延迟"],
                known_limits=(
                    "只覆盖已录入 CRM 的活动；销售在个人微信与本地表格里的操作拿不到，"
                    "这部分仍是观测盲区。自定义对象需按客户实际配置调整。"
                ),
                scopes=scopes,
                auth_hint=f"{vendor} 的只读 OAuth scope 或 Private App Token",
                description="拉取跟单活动记录，用于量化销售侧的重复录入",
            ),
            lambda s=salt: _crm_source(s),
        )


# ===========================================================================
# ERP / 进销存（B 级：多数只开放无时间戳的明细或汇总）
# ===========================================================================
def _erp_source(salt: int, types: list[str]):
    def source(*, tenant: str, since: str | None, limit: int) -> list[dict[str, Any]]:
        rng = _rng(tenant, salt)
        rows = []
        vid = 300000
        for _ in range(min(limit, 460)):
            vid += 1
            rows.append(
                {
                    "voucher_no": f"V{vid}",
                    "voucher_type": rng.choice(types),
                    "amount": round(rng.uniform(300, 16000), 2),
                    "partner": rng.choice(["华兴建材", "永安五金", "长城板材", "恒通管业"]),
                    "mismatch": "是" if rng.random() < 0.12 else "否",
                }
            )
        return rows

    return source


ERP_VENDORS = [
    (
        "erp_kingdee", "金蝶", "金蝶云·星辰 / K3",
        ["bill:query"],
        "https://open.kingdee.com/",
        [
            "在金蝶开放平台注册应用并获取凭据",
            "由客户 IT 授予单据查询（只读）权限",
            "按单据类型与日期区间分页拉取",
        ],
    ),
    (
        "erp_yonyou", "用友", "用友 U8 / YonSuite",
        ["voucher:read"],
        "https://developer.yonyouup.com/",
        [
            "在用友开发者中心创建应用，获取 appKey 与 appSecret",
            "申请单据查询只读 API 权限",
            "用 OpenAPI 按凭证类型拉取",
        ],
    ),
    (
        "erp_guanjia", "管家婆", "管家婆进销存",
        ["bill:list"],
        "https://open.guanjiapo.com/",
        [
            "联系管家婆开放平台开通接口权限",
            "获取只读接口凭据",
            "按单据类型拉取列表",
        ],
    ),
]


def _register_erp() -> None:
    for salt, (key, vendor, product, scopes, docs, steps) in enumerate(ERP_VENDORS):
        register(
            ConnectorSpec(
                key=key,
                name=f"{vendor}（进销存/ERP，只读）",
                category="ERP",
                vendor=vendor,
                product=product,
                docs_url=docs,
                setup_steps=steps,
                verified=False,
                verify_note=(
                    "各版本（云版/本地部署）接口差异很大，字段名与是否含操作时间戳"
                    "必须按客户实际版本确认"
                ),
                max_evidence_grade=EvidenceGrade.B,
                provides_timestamps=False,
                metrics=["该环节处理单量", "差异单量", "该环节返工率"],
                known_limits=(
                    "多数中小企业部署只开放汇总视图或无操作时间戳的明细，"
                    "因此可给频次、耗时需靠补数表——ROI 只能给区间。"
                    "本地部署版常需客户 DBA 提供只读视图（L2 档）。"
                ),
                scopes=scopes,
                auth_hint=f"{vendor} 的只读接口凭据，或客户 DBA 提供的只读视图",
                description="拉取单据明细用于交叉核对，但通常拿不到操作时间戳",
            ),
            lambda s=salt, t=["采购入库", "销售出库", "供应商对账", "库存调拨"]: _erp_source(s, t),
        )


# ===========================================================================
# 电商（A 级：订单含时间戳）
# ===========================================================================
def _ecom_source(salt: int):
    def source(*, tenant: str, since: str | None, limit: int) -> list[dict[str, Any]]:
        rng = _rng(tenant, salt)
        rows = []
        oid = 900000
        for d in range(30):
            day = _BASE + timedelta(days=d)
            for _ in range(rng.randint(35, 60)):
                oid += 1
                created = day.replace(hour=rng.randint(8, 22), minute=rng.randint(0, 59))
                rows.append(
                    {
                        "order_no": f"SC{oid}",
                        "created_at": created.isoformat(timespec="seconds"),
                        "paid_at": (
                            created + timedelta(minutes=rng.randint(1, 40))
                        ).isoformat(timespec="seconds"),
                        "amount": round(rng.uniform(120, 8600), 2),
                        "invoice_required": "是" if rng.random() < 0.42 else "否",
                        "reconcile_mismatch": "是" if rng.random() < 0.11 else "否",
                    }
                )
                if len(rows) >= limit:
                    return rows
        return rows

    return source


ECOM_VENDORS = [
    (
        "ecom_youzan", "有赞", "有赞微商城",
        ["trade:query"],
        "https://doc.youzanyun.com/",
        [
            "在有赞云创建自用型应用，获取 client_id 与 client_secret",
            "只申请交易查询（只读）权限",
            "用 youzan.trades.sold.get 按时间范围拉取",
        ],
    ),
    (
        "ecom_weimob", "微盟", "微盟智慧零售",
        ["order:list"],
        "https://doc.weimob.com/",
        [
            "在微盟开放平台创建应用并获取凭据",
            "申请订单查询只读权限",
            "按门店与时间范围分页拉取",
        ],
    ),
    (
        "ecom_taobao", "淘宝/天猫", "淘宝开放平台（TOP）",
        ["trade.fullinfo.get"],
        "https://open.taobao.com/doc.htm",
        [
            "在淘宝开放平台创建应用并完成企业认证",
            "申请交易类只读 API 权限（需审核）",
            "用 taobao.trades.sold.get 增量拉取",
        ],
    ),
    (
        "ecom_shopify", "Shopify", "Shopify Admin API",
        ["read_orders", "read_customers"],
        "https://shopify.dev/docs/api/admin-rest/latest/resources/order",
        [
            "在店铺后台创建 Custom App",
            "只勾选 read_ 前缀的 Admin API scope",
            "用 Admin API 按 created_at 分页拉取",
        ],
    ),
]


def _register_ecom() -> None:
    for salt, (key, vendor, product, scopes, docs, steps) in enumerate(ECOM_VENDORS):
        register(
            ConnectorSpec(
                key=key,
                name=f"{vendor}（订单，只读）",
                category="电商",
                vendor=vendor,
                product=product,
                docs_url=docs,
                setup_steps=steps,
                verified=False,
                verify_note="交易类接口多需平台审核，字段名与审核要求按当前文档确认",
                max_evidence_grade=EvidenceGrade.A,
                provides_timestamps=True,
                metrics=["该环节处理单量", "开票单量", "对账差异率"],
                known_limits=(
                    "只覆盖该平台的线上订单；门店与电话下单需另取来源。"
                    "交易类 API 通常需要平台审核，申请周期可能较长。"
                ),
                scopes=scopes,
                auth_hint=f"{vendor} 开放平台的只读应用凭据",
                description="拉取订单明细，用于与对账/开票环节交叉核对",
            ),
            lambda s=salt: _ecom_source(s),
        )


def register_all_vendors() -> None:
    for fn in (
        _register_im,
        _register_approval,
        _register_tickets,
        _register_crm,
        _register_erp,
        _register_ecom,
    ):
        try:
            fn()
        except ValueError:
            # 重复导入时保持幂等
            pass


register_all_vendors()
