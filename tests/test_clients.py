"""Task 1：客户注册表（多租户建档、列表、切换、删除）。

纪律：slug 直接参与文件路径，必须防路径穿越——这是最容易被忽略的注入面。
"""
import pytest

from aiea.clients import ClientRegistry, slugify


@pytest.fixture
def reg(tmp_path):
    return ClientRegistry(root=tmp_path)


def test_slugify_handles_chinese_names():
    assert slugify("明辉家居建材有限公司")
    assert slugify("ABC Trading Co.") == "abc-trading-co"


def test_slugify_rejects_path_traversal():
    # slug 直接进文件路径，必须消毒
    for bad in ("../etc", "../../root", "a/b", "..", ".", "/abs"):
        s = slugify(bad)
        assert ".." not in s
        assert "/" not in s
        assert s not in ("", ".")


def test_create_client_returns_profile_with_slug(reg):
    p = reg.create(name="张记建材", industry="建材分销", headcount=60, departments=["客服", "财务"])
    assert p.slug
    assert p.name == "张记建材"
    assert p.headcount == 60
    assert p.status == "draft"
    assert (reg.root / p.slug / "client.json").exists()


def test_duplicate_name_gets_distinct_slug(reg):
    a = reg.create(name="同名公司", industry="零售", headcount=30, departments=["客服"])
    b = reg.create(name="同名公司", industry="零售", headcount=30, departments=["客服"])
    assert a.slug != b.slug


def test_list_includes_created_clients(reg):
    reg.create(name="甲公司", industry="零售", headcount=40, departments=["客服"])
    reg.create(name="乙公司", industry="制造", headcount=90, departments=["财务"])
    slugs = {c.slug for c in reg.list()}
    assert len(slugs) == 2


def test_get_unknown_client_returns_none(reg):
    assert reg.get("does-not-exist") is None


def test_get_rejects_traversal_lookup(reg):
    assert reg.get("../../etc") is None


def test_headcount_out_of_admission_range_is_flagged_not_blocked(reg):
    # §17.2：20–200 人。超范围不是不能做，但必须标注"范围外，基准参考有限"
    small = reg.create(name="小微", industry="零售", headcount=8, departments=["客服"])
    assert small.out_of_scope is True
    assert "范围外" in small.scope_note
    ok = reg.create(name="正常", industry="零售", headcount=86, departments=["客服"])
    assert ok.out_of_scope is False


def test_update_status_after_probe_and_diagnosis(reg):
    p = reg.create(name="丙公司", industry="零售", headcount=50, departments=["客服"])
    reg.update(p.slug, status="materials", reachable_grade="A", delivery_form="完整诊断")
    got = reg.get(p.slug)
    assert got.status == "materials"
    assert got.reachable_grade == "A"
    assert got.delivery_form == "完整诊断"


def test_delete_removes_workspace(reg):
    p = reg.create(name="待删", industry="零售", headcount=50, departments=["客服"])
    path = reg.root / p.slug
    assert path.exists()
    assert reg.delete(p.slug) is True
    assert not path.exists()
    assert reg.get(p.slug) is None


def test_delete_refuses_traversal(reg):
    assert reg.delete("../..") is False


def test_material_count_and_diagnosis_flag_are_tracked(reg):
    p = reg.create(name="丁公司", industry="零售", headcount=50, departments=["客服"])
    got = reg.get(p.slug)
    assert got.material_count == 0
    assert got.has_report is False
