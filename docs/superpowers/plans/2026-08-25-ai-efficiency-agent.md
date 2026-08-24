# 中小企业 AI 提效场景识别 Agent Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax.

**Goal:** 实现架构设计 v1.2 的 Phase 1–3 最小可用系统 + Phase 4 反馈入口，并预置一个完整的真实感客户诊断案例可视化交付。

**Architecture:** Plan-and-Execute（S0–S5）骨架 + 局部 ReAct；工具层挡幻觉（纯函数 ROI、`insufficient_data` 一等公民）；文件工作区为唯一真相源；FastAPI 提供只读交付物 API + 零构建静态前端。

**Tech Stack:** Python 3.13、FastAPI、Uvicorn、Pydantic v2、pytest、OpenAI 兼容 LLM（api.wenwen-ai.com）、原生 ESM 前端

**Spec:** `docs/superpowers/specs/2026-08-25-ai-efficiency-agent-design.md`

## Global Constraints

- 证据分级口径、ROI 公式、`insufficient_data`/`no_grounding` 语义、只读边界、MAX_STEPS、熔断 = **冻结区**，代码中集中于 `config.FROZEN` 并有测试断言不可被运行时修改
- 全部连接器只读；写操作只落 `workspace/`
- 单阶段 `MAX_STEPS=20`；反评审 `MAX_ROUNDS=3`；`session_limit_usd` / `hour_limit_usd` 双档熔断
- 冲突偏差 > 30% → 标注冲突 + 转人工，禁止取均值
- C 级证据不得输出任何金额；专家判断区模板层禁止金额字段
- LLM：`base_url=https://api.wenwen-ai.com/v1`，主 `claude-sonnet-4-5`，judge `claude-opus-4-5-20251101`
- 客户原始数据不进任何知识库索引

---

### Task 1: 契约与冻结区（`config.py`、`models.py`）
**Files:** Create `src/aiea/config.py`, `src/aiea/models.py`, Test `tests/test_models.py`
- [ ] Step 1: 写失败测试：`Grade` 枚举含 A/B/C；`FROZEN` 含 6 项键；`ToolResult` 支持 `code="insufficient_data"` 且 `ok=True`
- [ ] Step 2: 运行确认失败（ModuleNotFoundError）
- [ ] Step 3: 实现枚举、`ToolResult`、`Evidence`、`TaskCard`、`ParentScenario`、`ROIResult`
- [ ] Step 4: 运行通过
- [ ] Step 5: commit

### Task 2: ROI 纯函数（`roi.py`）
**Files:** Create `src/aiea/roi.py`, Test `tests/test_roi.py`
**Interfaces:** Produces `roi_estimate(**kwargs) -> ROIResult`, `discount_factor(work_form)`, `aggregate_dedup(cards)`
- [ ] Step 1: 失败测试：缺 `hourly_cost_range` → `code="invalid_params"` 且 `next_action` 指向 `metric_probe`；真碎片 → 折现 0；C 级 → `amount is None`；依赖场景汇总先去重再相加且展示差额
- [ ] Step 2: 运行确认失败
- [ ] Step 3: 实现纯函数（无 IO、无随机、无 LLM）
- [ ] Step 4: 运行通过
- [ ] Step 5: commit

### Task 3: 证据裁决（`evidence.py`）
**Files:** Create `src/aiea/evidence.py`, Test `tests/test_evidence.py`
**Interfaces:** Produces `grade_of(source_type, cross_checked, has_objective_trace)`, `adjudicate(claims)`, `judge_work_form(timestamps)`
- [ ] Step 1: 失败测试：纯自述多方互证只能到 C；偏差 35% → conflict + requires_human 且无 `mean`；同窗聚集 40 分钟 → `batch` 且折现 100%
- [ ] Step 2: 确认失败 → Step 3 实现 → Step 4 通过 → Step 5 commit

### Task 4: 七维可行性（`feasibility.py`）
**Files:** Create `src/aiea/feasibility.py`, Test `tests/test_feasibility.py`
- [ ] 失败测试：返回 7 个分项 + `missing` 列表，且**不返回单一总分字段** → 实现 → 通过 → commit

### Task 5: 七层护栏（`guardrails.py`）
**Files:** Create `src/aiea/guardrails.py`, Test `tests/test_guardrails.py`
- [ ] 失败测试：附件含"忽略以上指令"被标 `injection_suspected` 且内容降级为不可信；手机号/邮箱被脱敏；跨 tenant 检索被 `denied`；结论强度超证据等级被守护层否决 → 实现 → 通过 → commit

### Task 6: 三库检索（`knowledge.py`）
**Files:** Create `src/aiea/knowledge.py`, `seed/knowledge/*.json`, Test `tests/test_knowledge.py`
- [ ] 失败测试：命中为空 → `no_grounding`；方法论库与基准库永不混检；含行业+规模+数值的候选条目**去具体化检验**被拒 → 实现 → 通过 → commit

### Task 7: 观测与简报（`telemetry.py`）
**Files:** Create `src/aiea/telemetry.py`, Test `tests/test_telemetry.py`
- [ ] 失败测试：span 字段命名符合 `gen_ai.*`；`brief()` 输出自然语言且不含内部术语（断言不出现 `insufficient_data`/`metric_probe`） → 实现 → 通过 → commit

### Task 8: LLM 客户端与熔断（`llm.py`）
**Files:** Create `src/aiea/llm.py`, Test `tests/test_llm.py`
- [ ] 失败测试：注入 fake transport 断言 base_url/model 正确、成本累计、超 `session_limit_usd` 抛熔断挂起而非静默降级 → 实现 → 通过 → commit

### Task 9: 工具层 15 件（`tools/`）
**Files:** Create `src/aiea/tools/__init__.py` 等，Test `tests/test_tools.py`
- [ ] 失败测试：`metric_probe` 无数据返回 `insufficient_data` 且带 `source`/`sample_size`；`taskcard_upsert` 无证据引用被拒写；`insight_propose` 输出含金额时被拒；`report_render` 未过门禁场景标灰不入正文 → 实现 → 通过 → commit

### Task 10: S0–S5 编排（`pipeline.py`、`workspace.py`）
**Files:** Create `src/aiea/pipeline.py`, `src/aiea/workspace.py`, Test `tests/test_pipeline.py`
- [ ] 失败测试：证据不足场景无法进入 S4；`MAX_STEPS` 超限挂起；读盘可重建状态；反评审只拿到任务卡与台账（断言入参不含主 Agent 推理链） → 实现 → 通过 → commit

### Task 11: 预置客户场景（`seed.py` + 素材）
**Files:** Create `src/aiea/seed.py`, `seed/clients/minghui/*`
- [ ] 生成 612 条工单、340 条对账修改记录、1,486 条订单、3 份纪要、补数表、注入探针附件；跑完整诊断落盘；断言 8 张子场景卡覆盖三种作业形态与 A/B/C 三级 + 1 条冲突 → commit

### Task 12: API 与前端（`api.py`、`web/`）
**Files:** Create `src/aiea/api.py`, `web/index.html`, `web/styles.css`, `web/app.js`, Test `tests/test_api.py`
- [ ] 失败测试：`/api/report` 返回八视图数据；专家判断响应体内**不含金额字段**；反馈 POST 角色缺失被拒 → 实现（frontend-design skill 定调） → 通过 → commit

### Task 13: 评测骨架与端到端验收
**Files:** Create `evals/golden/*.json`, `evals/failure_replays/`, Test `tests/test_e2e.py`
- [ ] 端到端断言：证据可追溯率 100%、C 级零金额、注入 0 逃逸、六维记分卡输出 → commit
