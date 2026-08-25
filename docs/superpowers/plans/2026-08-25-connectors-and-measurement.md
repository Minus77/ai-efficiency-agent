# L1 只读连接器 + 效果衡量 — 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development 或 executing-plans。

**Goal:** 补上架构文档 §4 的 L1 只读 API 双轨，并借此实现 §19.4 的效果衡量
（改造前基线 + 改造后复测），把「一次性诊断」变成「可持续追踪」。

**问题定位：**
1. 现状只有 L0 手工导入。客户每次都要人工导出，且拿不到连续数据。
2. **更关键**：§19.4 要求业务指标必须有改造前基线，一次性 CSV 给不了。
   没有连接器 → 没有周期性采集 → 无法证明 AI 改造真的有效果。

**Spec:** `架构设计.md` §4（数据接入双轨）、§19.4（业务指标）、§9.3（outcome_record 三类信号）

## Global Constraints

- **全部连接器只读，无一例外**（§4）。写操作只落 `workspace/`，只读断言在框架层强制。
- 凭据永不进上下文、永不进日志（§13.3）。连接器配置只存引用，不存明文。
- L1 拉取的数据同样过 `guardrails` 注入检测；客户原始数据不进任何向量库（§8.1）。
- 每次拉取强制带 tenant 过滤；跨租户拉取一律 `denied`。
- L1 与 L0 冲突时走 §3.1 固定裁决序，偏差 > 30% 标注冲突转人工，**不取均值**。
- 效果衡量只用强直接关联的过程指标；**无改造前基线的一律不采信**（§19.4）。
- 不采信经营结果指标（营收/利润率）——归因不可能。

---

### Task 1: 连接器框架（`connectors/base.py`）
**Files:** Create `src/aiea/connectors/__init__.py`, `src/aiea/connectors/base.py`, Test `tests/test_connectors_base.py`
**Interfaces:** Produces `Connector` 协议、`ConnectorSpec`、`PullResult`、`register`/`get_connector`、`CredentialRef`
- [ ] 失败测试：写方法一律 `denied`；凭据只存引用（明文不出现在 `to_dict()`）；
      能力声明含 `provides_timestamps`/`metrics`；速率配额超限返回结构化错误；
      跨 tenant 拉取被拒；拉取结果过注入检测
- [ ] 实现 → 通过 → commit

### Task 2: 预置连接器（5 个）
**Files:** Create `src/aiea/connectors/ticketing.py`, `crm.py`, `im.py`, `erp.py`, `ecommerce.py`, Test `tests/test_connectors_preset.py`
- [ ] 每个连接器声明：能拉什么字段、能算什么指标、时间戳可得性、已知边界
- [ ] 内置**可复现的演示数据源**（固定种子），让"没有真实客户系统"时也能跑通全链路
- [ ] 失败测试：工单连接器返回含时间戳的记录；IM 连接器明确声明无法批量导出（对应真实边界）；
      ERP 只能给汇总（对应 B/C 级上限）；每个连接器的 `capabilities()` 都能被 derive 消费
- [ ] 实现 → 通过 → commit

### Task 3: 接入诊断（L1 → ParsedMaterial → 交叉互校）
**Files:** Modify `src/aiea/intake.py`, `src/aiea/diagnose.py`, Test `tests/test_connector_intake.py`
- [ ] 失败测试：L1 拉取结果能转成 `ParsedMaterial` 并被 `derive_scenarios` 消费；
      L1 与 L0 同一活动偏差 35% → 台账标 `conflict` 且 `requires_human`；
      L1 证据类型为 `system_data`（A 级）；L1 拉取失败不阻断诊断，降级为缺口
- [ ] 实现 → 通过 → commit

### Task 4: 效果衡量（`measure.py`）
**Files:** Create `src/aiea/measure.py`, Test `tests/test_measure.py`
**Interfaces:** Produces `capture_baseline(...)`, `measure_effect(...)`, `EffectReport`
- [ ] 失败测试：无改造前基线 → 返回 `insufficient_data` 且拒绝给"改善率"；
      有基线 + 后测 → 给区间与置信度；仅采信过程指标（传入营收类指标被拒）；
      样本量过小时标注不确定；改善方向与场景预期不符时标记异常
- [ ] 实现 → 通过 → commit

### Task 5: API + 前端
**Files:** Modify `src/aiea/api.py`, `web/*`, Test `tests/test_api_connectors.py`
- [ ] 连接器管理页：可选连接器列表、连接（填只读凭据引用）、同步、能力与边界说明
- [ ] 效果衡量页：基线 vs 后测对比，无基线时明确说"测不了"
- [ ] 失败测试：凭据不回显；同步返回拉取条数与证据等级；跨租户访问 404
- [ ] 实现 → 通过 → commit

### Task 6: 端到端与文档
**Files:** Test `tests/test_e2e_connectors.py`, Modify `README.md`
- [ ] 全链路：连接 → 同步 → 诊断 → 记基线 → 复测 → 出效果对比
- [ ] 断言：连接器数据改变则结论改变；无基线时效果页不给数字
- [ ] Playwright 验证 + README 更新
