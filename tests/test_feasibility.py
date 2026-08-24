"""Task 4：七维 rubric，返回分项 + 缺失项，不返回单一总分（§6、§11.2）。"""
import pytest

from aiea.feasibility import DIMENSIONS, feasibility_score


def test_seven_dimensions_are_fixed():
    assert len(DIMENSIONS) == 7
    assert "数据可得性" in DIMENSIONS
    assert "合规风险" in DIMENSIONS


def test_returns_breakdown_not_a_single_verdict_score():
    r = feasibility_score(card_id="s-01", scores={k: 3.0 for k in DIMENSIONS})
    fs = r.data["feasibility"]
    assert set(fs.dimensions) == set(DIMENSIONS)
    assert fs.missing == []
    # 有加权难度供矩阵定位，但不得叫"总分"，且必须同时带分项
    assert fs.weighted_difficulty is not None
    assert not hasattr(fs, "total_score")


def test_missing_dimensions_are_reported_not_guessed():
    partial = {k: 3.0 for k in list(DIMENSIONS)[:5]}
    fs = feasibility_score(card_id="s-01", scores=partial).data["feasibility"]
    assert len(fs.missing) == 2
    assert fs.weighted_difficulty is None, "缺维度时不得合成难度值"


def test_out_of_range_score_is_rejected():
    r = feasibility_score(card_id="s-01", scores={**{k: 3.0 for k in DIMENSIONS}, "合规风险": 9.0})
    assert r.ok is False
    assert "1–5" in r.next_action or "1-5" in r.next_action
