"""连接器数据必须**跨进程**可复现。

这个 bug 的形态：用 hash(tenant) 做随机种子。Python 默认对 str 哈希加盐
（PYTHONHASHSEED 随机），因此同一个客户在服务重启后会拿到完全不同的数据——
对一个承诺"每个数字都能回指证据"的诊断工具，这意味着结论会无故漂移。

单进程内测试永远发现不了，必须跨进程验证。
"""
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

SRC = str(Path(__file__).resolve().parents[1] / "src")

PROBE = textwrap.dedent(
    """
    from aiea.connectors import build_connector, list_specs
    from aiea.connectors.base import CredentialRef

    out = []
    for spec in list_specs():
        r = build_connector(
            spec.key, tenant="stable-tenant", credential=CredentialRef("p", "k", "s")
        ).pull(requested_tenant="stable-tenant")
        first = ""
        if r.rows:
            first = "|".join(str(v) for v in list(r.rows[0].values())[:3])
        out.append(f"{spec.key}:{r.row_count}:{first}")
    print("\\n".join(out))
    """
)


def _probe(hashseed: str) -> str:
    proc = subprocess.run(
        [sys.executable, "-c", PROBE],
        capture_output=True, text=True, timeout=120,
        env={"PYTHONPATH": SRC, "PYTHONHASHSEED": hashseed, "PATH": "/usr/bin:/bin"},
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


def test_same_tenant_same_data_across_hash_seeds():
    """核心断言：换 PYTHONHASHSEED 相当于模拟服务重启，数据必须一模一样。"""
    a = _probe("1")
    b = _probe("42")
    c = _probe("random")
    assert a == b, "同一客户跨进程数据不一致——种子依赖了随机化的 str hash"
    assert a == c


def test_different_tenants_still_differ_across_processes():
    """稳定不等于所有客户都拿到同一份数据。"""
    probe = textwrap.dedent(
        """
        from aiea.connectors import build_connector
        from aiea.connectors.base import CredentialRef

        for t in ("tenant-a", "tenant-b"):
            r = build_connector(
                "ticketing_readonly", tenant=t, credential=CredentialRef("p", "k", "s")
            ).pull(requested_tenant=t)
            print(t, r.row_count, r.rows[0]["category"])
        """
    )
    proc = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True, text=True, timeout=60,
        env={"PYTHONPATH": SRC, "PYTHONHASHSEED": "7", "PATH": "/usr/bin:/bin"},
    )
    assert proc.returncode == 0, proc.stderr
    lines = proc.stdout.strip().splitlines()
    assert lines[0] != lines[1], "不同客户应拿到不同数据"


def test_no_builtin_hash_in_connector_seeds():
    """静态防回归：连接器模块里不得再出现 hash(tenant) 这种不稳定种子。"""
    root = Path(__file__).resolve().parents[1] / "src" / "aiea" / "connectors"
    for path in root.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "hash(tenant)" not in text, (
            f"{path.name} 使用了 hash(tenant) 作为种子；"
            "Python 对 str 哈希加盐，跨进程不稳定，请改用 stable_seed()"
        )
