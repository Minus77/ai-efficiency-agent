"""AIEA_WORKSPACE 必须真正生效。

这个 bug 的形态很典型：config.py 定义了环境变量，但各处默认值硬写成字符串
"workspace"，从未读取该配置。结果是"我明明指定了目录，数据却写进了仓库"。
"""
import os
from pathlib import Path

import pytest

from aiea.config import Settings


def test_settings_reads_workspace_env(monkeypatch, tmp_path):
    monkeypatch.setenv("AIEA_WORKSPACE", str(tmp_path))
    assert Settings().workspace_root == str(tmp_path)


def test_default_workspace_root_helper_exists():
    """需要一个统一的取值入口，避免各处再各写一遍默认字符串。"""
    from aiea.config import default_workspace_root

    assert default_workspace_root()


def test_default_workspace_root_follows_env(monkeypatch, tmp_path):
    from aiea.config import default_workspace_root

    monkeypatch.setenv("AIEA_WORKSPACE", str(tmp_path / "custom"))
    assert Path(default_workspace_root()) == tmp_path / "custom"


def test_create_app_honours_env_when_root_omitted(monkeypatch, tmp_path):
    """不传 root 时必须落在 AIEA_WORKSPACE，而不是仓库里的 workspace/。

    客户名用随机后缀：slug 由名称哈希而来，固定名字会和历史残留目录撞车，
    让这条断言变成"取决于仓库当前状态"而非取决于代码行为。
    """
    import uuid

    monkeypatch.setenv("AIEA_WORKSPACE", str(tmp_path))
    from fastapi.testclient import TestClient

    from aiea.api import create_app

    name = "环境变量客户-" + uuid.uuid4().hex[:8]
    app = TestClient(create_app())
    r = app.post("/api/clients", json={"name": name, "industry": "零售", "headcount": 50})
    assert r.status_code == 200
    slug = r.json()["slug"]
    assert (tmp_path / slug / "client.json").exists(), "客户应落在环境变量指定的目录"
    assert not (Path("workspace") / slug).exists(), "绝不应写进仓库默认目录"


def test_run_diagnosis_honours_env_when_root_omitted(monkeypatch, tmp_path):
    monkeypatch.setenv("AIEA_WORKSPACE", str(tmp_path))
    from aiea.clients import ClientRegistry
    from aiea.diagnose import DiagnosisNotReady, run_diagnosis
    from aiea.intake import save_material

    reg = ClientRegistry(root=tmp_path)
    c = reg.create(name="环境诊断客户", industry="零售", headcount=50, departments=["客服"])
    rows = ["id,created_at,first_response_at,category"] + [
        f"A{i},2026-03-12T09:{i % 55:02d}:00,2026-03-12T09:{(i + 3) % 58:02d}:00,咨询"
        for i in range(30)
    ]
    save_material(root=tmp_path, slug=c.slug, filename="t.csv", content="\n".join(rows).encode("utf-8"))

    # 不传 root：应读环境变量而非硬编码目录
    report = run_diagnosis(tenant=c.slug)
    assert report["cards"]
    assert (tmp_path / c.slug / "REPORT.json").exists()


def test_client_registry_default_root_follows_env(monkeypatch, tmp_path):
    monkeypatch.setenv("AIEA_WORKSPACE", str(tmp_path))
    from aiea.clients import ClientRegistry

    assert Path(ClientRegistry().root) == tmp_path
