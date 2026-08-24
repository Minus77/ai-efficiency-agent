#!/usr/bin/env python3
"""本地启动入口。

用法：
    python run.py            # 启动 Web 交付物界面（默认 http://127.0.0.1:8848）
    python run.py --seed     # 只重新生成预置客户数据并跑一次诊断，不起服务
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))


def main() -> None:
    parser = argparse.ArgumentParser(description="中小企业 AI 提效场景识别 Agent")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8848)
    parser.add_argument("--seed", action="store_true", help="只跑一次预置诊断并落盘")
    parser.add_argument(
        "--llm",
        action="store_true",
        help="S5 反评审与洞察改由 judge 档模型现场生成（需 AIEA_API_KEY）",
    )
    args = parser.parse_args()

    llm = None
    if args.llm:
        from aiea.llm import LLMClient

        llm = LLMClient()
        print("→ 已启用模型生成：反评审与洞察将由 judge 档模型现场产出")

    if args.seed:
        from aiea.seed import run_seed_diagnosis

        report = run_seed_diagnosis(llm=llm)
        sc = report["scorecard"]
        print(f"客户：{report['client']['client_name']}（{report['delivery_form']}）")
        print(f"场景：{len(report['cards'])} 张子卡 / {len(report['parents'])} 个父场景")
        print(f"证据等级分布：{sc['grade_distribution']}")
        print(f"作业形态分布：{sc['work_form_distribution']}")
        print(f"证据可追溯率：{sc['evidence_traceability']:.0%}")
        print(f"已量化 {sc['scenarios_quantified']} 个 / 仅方向 {sc['scenarios_direction_only']} 个")
        print(f"落盘位置：workspace/minghui/")
        return

    import uvicorn

    from aiea.api import create_app

    print(f"→ 打开 http://{args.host}:{args.port} 查看诊断交付物")
    uvicorn.run(
        create_app(use_llm=args.llm), host=args.host, port=args.port, log_level="warning"
    )


if __name__ == "__main__":
    main()
