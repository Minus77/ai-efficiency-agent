"""术语表：界面上每个专有名词都要能解释清楚（§12.1 绝不暴露内部术语）。

架构文档 §12.1 的原话是"用户看到的是『我想了解下你们客服每天大概处理多少条咨询』，
不是『metric_probe 需要 baseline_minutes 参数』"。

但完全不用术语也不现实——「证据等级」「作业形态」这些是交付物的骨架，
删掉就没法表达。折中方案：术语保留，但每个都必须有解释，且界面可查。
"""
import pytest

from aiea.glossary import (
    GLOSSARY,
    INTERNAL_ONLY,
    Term,
    explain,
    grade_scale,
    lookup,
    work_form_scale,
    difficulty_scale,
)


# ---------------------------- 结构 ----------------------------
def test_every_term_has_plain_explanation():
    assert len(GLOSSARY) >= 25
    for key, term in GLOSSARY.items():
        assert term.label, key
        assert term.plain, f"{key} 缺少一句话解释"
        # 解释本身不能再用术语堆砌
        assert len(term.plain) <= 120, f"{key} 的解释过长，说不清就是没想清"


def test_terms_explain_why_it_matters():
    """光解释"是什么"不够，要说明"为什么值得看"。"""
    for key, term in GLOSSARY.items():
        assert term.why, f"{key} 缺少『为什么重要』"


def test_lookup_is_case_and_alias_tolerant():
    assert lookup("证据等级") is not None
    assert lookup("AS_OF") is not None
    assert lookup("as_of") is not None, "大小写不敏感"
    assert lookup("不存在的词") is None


def test_explain_returns_dict_for_frontend():
    d = explain("折现")
    assert d is not None
    assert set(d) >= {"key", "label", "plain", "why"}


# ---------------------------- 内部术语必须标记 ----------------------------
def test_internal_terms_are_listed_for_lint():
    """这些词绝不能出现在界面上，列出来供检查用。"""
    for t in ("insufficient_data", "no_grounding", "schema", "tenant", "slug", "playbook"):
        assert t in INTERNAL_ONLY, f"{t} 应登记为内部术语"


def test_internal_terms_are_not_in_glossary():
    """术语表是给用户看的，不该收录内部实现词。"""
    for t in INTERNAL_ONLY:
        assert t not in GLOSSARY, f"{t} 是内部术语，不该进用户术语表"


# ---------------------------- 分级标准 ----------------------------
def test_grade_scale_covers_abc_with_criteria_and_consequence():
    scale = grade_scale()
    assert [s["grade"] for s in scale] == ["A", "B", "C"]
    for s in scale:
        assert s["criteria"], "必须写明什么材料够这一级"
        assert s["output"], "必须写明这一级能给什么形式的结论"
        assert s["example"], "要有具体例子，否则用户对不上自己的情况"


def test_grade_scale_states_output_restrictions_explicitly():
    scale = {s["grade"]: s for s in grade_scale()}
    assert "点估" in scale["A"]["output"]
    assert "区间" in scale["B"]["output"]
    # C 级最关键：必须明说不给金额
    assert "不给" in scale["C"]["output"] or "无金额" in scale["C"]["output"]


def test_work_form_scale_explains_discount():
    scale = work_form_scale()
    assert len(scale) == 3
    keys = {s["key"] for s in scale}
    assert keys == {"continuous", "batch", "fragmented"}
    for s in scale:
        assert s["criteria"]
        assert s["discount"] is not None
        assert s["why"]
    frag = next(s for s in scale if s["key"] == "fragmented")
    assert frag["discount"] == 0
    batch = next(s for s in scale if s["key"] == "batch")
    assert batch["discount"] == 100


def test_difficulty_scale_explains_seven_dimensions():
    scale = difficulty_scale()
    assert len(scale["dimensions"]) == 7
    for d in scale["dimensions"]:
        assert d["name"]
        assert d["weight"] > 0
        assert d["plain"], "每一维都要说明看的是什么"
    assert abs(sum(d["weight"] for d in scale["dimensions"]) - 1.0) < 0.01
    # 1-5 分的含义必须写明，否则"难度 2.02"是天书
    assert scale["range"]
    assert "1" in scale["range"] and "5" in scale["range"]


def test_difficulty_scale_matches_actual_rubric():
    """术语表里的维度与权重必须与 feasibility.py 一致，否则界面在骗人。"""
    from aiea.feasibility import DIMENSIONS

    scale = difficulty_scale()
    assert {d["name"] for d in scale["dimensions"]} == set(DIMENSIONS)
    for d in scale["dimensions"]:
        assert abs(d["weight"] - DIMENSIONS[d["name"]]) < 1e-9


# ---------------------------- 覆盖界面实际用到的词 ----------------------------
@pytest.mark.parametrize("term", [
    "证据等级", "作业形态", "折现", "点估", "区间", "回本周期",
    "证据台账", "反评审", "经验判断", "缺口", "优先级矩阵",
    "过程指标", "基线", "后测", "交付形态", "可追溯率",
    "AS_OF", "ROI", "L0", "L1", "补数表", "多方交叉",
])
def test_ui_terms_are_all_covered(term):
    assert lookup(term) is not None, f"界面用到「{term}」但术语表没有"


# ---------------------------- 分组 ----------------------------
def test_groups_cover_every_term_exactly_once():
    """分组是参考页的目录。漏一个词，那个词就永远不会出现在界面上。"""
    from aiea.glossary import GROUPS

    listed: list[str] = []
    for _name, _intro, keys in GROUPS:
        listed.extend(keys)

    assert len(listed) == len(set(listed)), "有术语被分到两个组里"
    assert set(listed) == set(GLOSSARY), (
        f"分组与术语表不一致：漏了 {set(GLOSSARY) - set(listed)}，多了 {set(listed) - set(GLOSSARY)}"
    )


def test_every_group_has_intro():
    from aiea.glossary import GROUPS

    for name, intro, keys in GROUPS:
        assert name and intro, f"{name} 缺少组说明"
        assert keys, f"{name} 是空组"


def test_grouped_terms_shape_for_frontend():
    from aiea.glossary import grouped_terms

    groups = grouped_terms()
    assert len(groups) >= 6
    for g in groups:
        assert g["group"] and g["intro"] and g["terms"]
        for t in g["terms"]:
            assert t["key"] and t["label"] and t["plain"] and t["why"]


# ---------------------------- ROI 三档与严重度 ----------------------------
def test_tier_scale_explains_what_differs_between_tiers():
    """「保守/中性/乐观」不说明差在哪，读者会以为是三个人拍的三个数。"""
    from aiea.glossary import tier_scale

    scale = tier_scale()
    assert [s["tier"] for s in scale] == ["保守", "中性", "乐观"]
    for s in scale:
        assert s["criteria"], f"{s['tier']} 档未写明判定口径"
        assert s["why"], f"{s['tier']} 档未写明为什么这么定"
    assert scale[0]["uplift"] == 1.0, "保守档不得叠加任何额外增益"
    # 乐观档必须写明证据门槛，否则客户会拿它做预算
    assert "A" in scale[2]["criteria"]


def test_tier_uplifts_match_roi_module():
    """系数只能有一处真值。术语表另写一份，改了 roi.py 界面就在骗人。"""
    from aiea.glossary import tier_scale
    from aiea.roi import _NEUTRAL_UPLIFT, _OPTIMISTIC_UPLIFT

    scale = {s["tier"]: s for s in tier_scale()}
    assert scale["中性"]["uplift"] == _NEUTRAL_UPLIFT
    assert scale["乐观"]["uplift"] == _OPTIMISTIC_UPLIFT


def test_severity_scale_states_criteria_and_action():
    """「严重度 高」如果不说明该怎么办，等于只是吓人。"""
    from aiea.glossary import severity_scale

    scale = severity_scale()
    assert [s["level"] for s in scale] == ["高", "中", "低"]
    for s in scale:
        assert s["criteria"], f"严重度 {s['level']} 未写明判定标准"
        assert s["action"], f"严重度 {s['level']} 未写明应对动作"


def test_severity_levels_match_agents_module():
    """界面上的档位必须与反评审实际会产出的档位一致。"""
    from aiea.agents import _SEVERITIES
    from aiea.glossary import severity_scale

    assert {s["level"] for s in severity_scale()} == set(_SEVERITIES)
