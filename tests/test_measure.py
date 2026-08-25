"""Task 4：效果衡量（§19.4 业务指标、§9.3 三类信号）。

最硬的一条：**无改造前基线的指标一律不采信**。
没有基线的"改善"无法证明是改造带来的——这是整个衡量机制的地基。
"""
import pytest

from aiea.clients import ClientRegistry
from aiea.measure import (
    ALLOWED_METRICS,
    BANNED_METRICS,
    capture_baseline,
    list_baselines,
    measure_effect,
)
from aiea.models import ResultCode


@pytest.fixture
def client(tmp_path):
    reg = ClientRegistry(root=tmp_path)
    c = reg.create(name="衡量客户", industry="建材分销", headcount=70, departments=["客服"])
    return tmp_path, c.slug


TS_BEFORE = [f"2026-07-{d:02d}T09:{m:02d}:00" for d in range(1, 11) for m in (0, 12, 27, 41)]
TS_AFTER = [f"2026-09-{d:02d}T09:{m:02d}:00" for d in range(1, 11) for m in (0, 20)]


# ---------------------------- 指标白名单 ----------------------------
def test_only_process_metrics_are_allowed():
    assert "该环节处理时长" in ALLOWED_METRICS
    assert "该环节处理单量" in ALLOWED_METRICS
    assert "该环节返工率" in ALLOWED_METRICS


def test_business_outcome_metrics_are_banned():
    for m in ("营收", "利润率", "人力成本占比"):
        assert m in BANNED_METRICS


def test_capture_rejects_business_outcome_metric(client):
    root, slug = client
    r = capture_baseline(
        root=root, slug=slug, card_id="s-01", metric="营收",
        value=120000.0, sample_size=30, source="ERP",
    )
    assert r["ok"] is False
    assert "营收" in r["note"] or "经营结果" in r["note"]
    assert "过程指标" in r["next_action"]


def test_capture_rejects_unknown_metric(client):
    root, slug = client
    r = capture_baseline(
        root=root, slug=slug, card_id="s-01", metric="随便一个指标",
        value=1.0, sample_size=10, source="x",
    )
    assert r["ok"] is False


# ---------------------------- 无基线不给改善率 ----------------------------
def test_measure_without_baseline_returns_insufficient_data(client):
    root, slug = client
    r = measure_effect(
        root=root, slug=slug, card_id="s-01", metric="该环节处理时长",
        value=18.0, sample_size=40, source="工单只读 API",
    )
    assert r["code"] == ResultCode.INSUFFICIENT_DATA.value
    assert r["improvement_pct"] is None, "无基线不得给出任何改善率"
    assert r["direction"] == "无法判断"
    assert "基线" in r["note"]


def test_baseline_then_measure_gives_improvement(client):
    root, slug = client
    b = capture_baseline(
        root=root, slug=slug, card_id="s-01", metric="该环节处理时长",
        value=30.0, sample_size=40, source="工单只读 API（改造前）",
    )
    assert b["ok"] is True
    r = measure_effect(
        root=root, slug=slug, card_id="s-01", metric="该环节处理时长",
        value=18.0, sample_size=44, source="工单只读 API（改造后）",
    )
    assert r["code"] == ResultCode.OK.value
    assert r["improvement_pct"] == pytest.approx(40.0, abs=0.1)
    assert r["direction"] == "改善"
    assert r["baseline"]["value"] == 30.0


def test_regression_is_reported_as_regression(client):
    root, slug = client
    capture_baseline(root=root, slug=slug, card_id="s-02", metric="该环节处理时长",
                     value=20.0, sample_size=40, source="基线")
    r = measure_effect(root=root, slug=slug, card_id="s-02", metric="该环节处理时长",
                       value=26.0, sample_size=40, source="后测")
    assert r["direction"] == "退步"
    assert r["improvement_pct"] < 0


def test_higher_is_better_metric_direction_is_inverted(client):
    """处理单量升高是好事，处理时长升高是坏事——方向不能一刀切。"""
    root, slug = client
    capture_baseline(root=root, slug=slug, card_id="s-03", metric="该环节处理单量",
                     value=100.0, sample_size=30, source="基线")
    r = measure_effect(root=root, slug=slug, card_id="s-03", metric="该环节处理单量",
                       value=130.0, sample_size=30, source="后测")
    assert r["direction"] == "改善"
    assert r["improvement_pct"] > 0


# ---------------------------- 样本量与不确定性 ----------------------------
def test_small_sample_is_flagged_as_uncertain(client):
    root, slug = client
    capture_baseline(root=root, slug=slug, card_id="s-04", metric="该环节处理时长",
                     value=30.0, sample_size=4, source="基线")
    r = measure_effect(root=root, slug=slug, card_id="s-04", metric="该环节处理时长",
                       value=18.0, sample_size=3, source="后测")
    assert r["low_confidence"] is True
    assert "样本" in r["note"]


def test_adequate_sample_is_not_flagged(client):
    root, slug = client
    capture_baseline(root=root, slug=slug, card_id="s-05", metric="该环节处理时长",
                     value=30.0, sample_size=60, source="基线")
    r = measure_effect(root=root, slug=slug, card_id="s-05", metric="该环节处理时长",
                       value=18.0, sample_size=55, source="后测")
    assert r["low_confidence"] is False


# ---------------------------- 从时间戳自动算基线 ----------------------------
def test_baseline_can_be_derived_from_connector_timestamps(client):
    root, slug = client
    r = capture_baseline(
        root=root, slug=slug, card_id="s-06", metric="该环节处理单量",
        timestamps=TS_BEFORE, source="工单只读 API",
    )
    assert r["ok"] is True
    assert r["value"] == len(TS_BEFORE)
    assert r["sample_size"] == len(TS_BEFORE)


def test_measure_from_timestamps_compares_counts(client):
    root, slug = client
    capture_baseline(root=root, slug=slug, card_id="s-07", metric="该环节处理单量",
                     timestamps=TS_BEFORE, source="改造前")
    r = measure_effect(root=root, slug=slug, card_id="s-07", metric="该环节处理单量",
                       timestamps=TS_AFTER, source="改造后")
    assert r["code"] == ResultCode.OK.value
    assert r["measured"]["value"] == len(TS_AFTER)


def test_capture_requires_value_or_timestamps(client):
    root, slug = client
    r = capture_baseline(root=root, slug=slug, card_id="s-08", metric="该环节处理时长", source="x")
    assert r["ok"] is False
    assert "value" in r["next_action"] or "时间戳" in r["next_action"]


# ---------------------------- 持久化与列表 ----------------------------
def test_baselines_are_persisted_per_client(client, tmp_path):
    root, slug = client
    capture_baseline(root=root, slug=slug, card_id="s-01", metric="该环节处理时长",
                     value=30.0, sample_size=40, source="基线")
    items = list_baselines(root=root, slug=slug)
    assert len(items) == 1
    assert items[0]["card_id"] == "s-01"
    assert (root / slug / "baselines.json").exists()


def test_baseline_is_immutable_new_capture_creates_version(client):
    """基线不可变：重复记录产生新版本并保留前一版，便于复议。"""
    root, slug = client
    capture_baseline(root=root, slug=slug, card_id="s-01", metric="该环节处理时长",
                     value=30.0, sample_size=40, source="第一次")
    capture_baseline(root=root, slug=slug, card_id="s-01", metric="该环节处理时长",
                     value=28.0, sample_size=45, source="第二次")
    items = list_baselines(root=root, slug=slug)
    assert len(items) == 2, "旧基线必须保留，不能被覆盖"
    versions = [i["version"] for i in items]
    assert versions == [1, 2]
    # 后测应对齐最新版本
    r = measure_effect(root=root, slug=slug, card_id="s-01", metric="该环节处理时长",
                       value=14.0, sample_size=45, source="后测")
    assert r["baseline"]["value"] == 28.0


def test_unknown_client_is_rejected(tmp_path):
    r = capture_baseline(root=tmp_path, slug="nope", card_id="s-01",
                        metric="该环节处理时长", value=1.0, sample_size=5, source="x")
    assert r["ok"] is False


# ---------------------------- 汇总必须自带对比所需字段 ----------------------------
def test_effect_summary_measurements_carry_baseline_value(client):
    """前端要画"基线 vs 后测"，汇总里必须直接带上基线值。

    只存 baseline_id 会让渲染层要么再查一次、要么渲染成 0——
    后者是静默错误：表上显示"基线 0"，看起来像改善了 100%。
    """
    from aiea.measure import effect_summary

    root, slug = client
    capture_baseline(root=root, slug=slug, card_id="s-01", metric="该环节处理时长",
                     value=30.0, sample_size=40, source="基线")
    measure_effect(root=root, slug=slug, card_id="s-01", metric="该环节处理时长",
                   value=18.0, sample_size=44, source="后测")

    m = effect_summary(root=root, slug=slug)["measurements"][0]
    assert m["baseline_value"] == 30.0
    assert m["measured_value"] == 18.0
    assert m["baseline_id"]


def test_effect_summary_survives_missing_baseline_reference(client):
    """基线文件被清空等异常情况下，不能崩，也不能把缺失渲染成 0。"""
    from aiea.measure import effect_summary

    root, slug = client
    capture_baseline(root=root, slug=slug, card_id="s-02", metric="该环节处理时长",
                     value=20.0, sample_size=30, source="基线")
    measure_effect(root=root, slug=slug, card_id="s-02", metric="该环节处理时长",
                   value=15.0, sample_size=30, source="后测")
    (root / slug / "baselines.json").write_text("[]", encoding="utf-8")

    m = effect_summary(root=root, slug=slug)["measurements"][0]
    assert m["baseline_value"] is None, "查不到基线应为 None，不得退化成 0"
