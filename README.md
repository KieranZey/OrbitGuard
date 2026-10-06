# OrbitGuard — 星载Agent全链路可信监护与执行门控框架

[![OrbitGuard CI](https://github.com/KieranZey/OrbitGuard/actions/workflows/tests.yml/badge.svg)](https://github.com/KieranZey/OrbitGuard/actions/workflows/tests.yml)

> 独立设计并实现包含故障注入测试床、运行稳定性监护（RSM）、输出可信度评估（DTM）、汇合判定（Gateway）、执行门控（EGM）的五模块端到端星载Agent可信监护系统。

## 项目定位

针对星上Agent应用场景，开展面向星载故障诊断与任务规划Agent的可信监护架构研究：
1. **保障Agent运行稳定性**：防止Agent死锁、无限推理、辐射扰动引发运行异常
2. **保障Agent输出决策可信性**：量化每一条诊断结论、任务重规划方案的可靠程度，给地面/星上操作员提供带风险标签的辅助建议
3. **严守辅助决策、人最终裁决的航天安全边界**：不追求完全自主执行

## 五模块架构

```
┌─────────────────────────────────────────────────────────────────┐
│              故障注入测试床 (FaultInjector)                        │
│      9类故障 × 多故障叠加 × 3档强度 × 4种噪声模式                 │
└──────────────────────────────┬──────────────────────────────────┘
                               │ 事件流 (8类结构化事件)
          ┌────────────────────┼────────────────────┐
          ▼                    ▼                    ▼
  ┌────────────────┐  ┌────────────────┐  ┌────────────────┐
  │  RSM 运行稳定性  │  │  DTM 输出可信度  │  │  (物理状态快照)  │
  │  5类异常检测     │  │  4类可信度检查   │  │                 │
  └───────┬────────┘  └───────┬────────┘  └───────┬────────┘
          └────────────────────┼────────────────────┘
                               ▼
                  ┌──────────────────────────┐
                  │   Gateway 汇合判定          │
                  │   C1-C5物理约束 + 三级路由   │
                  │   PERMIT / SUSPEND / BLOCK │
                  └──────────────┬─────────────┘
                                 ▼
                  ┌──────────────────────────┐
                  │   EGM 执行门控             │
                  │   人工审批 + 一次性许可      │
                  │   执行前状态检查 + 模拟执行   │
                  │   失败安全保持 + 审计记录     │
                  └──────────────────────────┘
```

## 模块详情

### 1. FaultInjector — 故障注入测试床
- **9类故障**：稳定性（infinite_loop, process_crash, no_output_timeout, memory_bloat, repetitive_calls）+ 可信性（wrong_diagnosis, inflated_confidence, missing_evidence, contradictory_sensors）
- **多故障叠加引擎**：支持同时注入2-3个故障，终结性故障互斥
- **故障强度分级**：每故障支持light/medium/heavy三档参数
- **真实遥测噪声模型**：高斯噪声、随机漂移、脉冲干扰、数据丢包
- **故障覆盖率量化报告**：22维故障空间，当前覆盖率45.5%（10维，含遥测噪声层实现的帧级丢包）
- **非侵入式架构**：装饰器+配置驱动，不改动Agent核心逻辑

### 2. RSM — 运行稳定性监护器
- **心跳检测**：超过阈值无心跳 → 进程崩溃
- **无进展检测**：uptime增长但step不变 → 死循环/活锁
- **工具超时检测**：tool_call后超时无tool_result → 无输出超时（NO_OUTPUT_TIMEOUT，按CPU特征与忙等死循环独立判别）
- **内存检测**：内存持续增长超阈值 → 内存泄漏
- **重复调用检测**：同一工具连续调用超N次 → 重复循环

### 3. DTM — 输出可信度监护器
- **结论一致性检查**：诊断结论是否有遥测证据支持
- **置信度校准检查**：置信度是否与证据强度匹配
- **证据完整性检查**：支持证据是否为空或数量不足
- **多源矛盾检查**：多传感器数据是否互相矛盾
- **trust_score量化**：0-1信任分 + risk_label（low/medium/high/critical）

### 4. Gateway — 汇合判定
- **C1-C5物理约束**：功率、热、碰撞、电池、辐射
- **三级路由**：BLOCK（critical故障/trust<0.5）→ SUSPEND（故障/trust<0.8/物理违规）→ PERMIT（全部通过）
- **遏制措施**：ISOLATE / TOOL_RESTRICT / PLANNING_BLOCK / NONE

### 5. EGM — 执行门控
- **执行前置门控**（`egm/gate.py`）：mutation 工具执行前必须通过 Gateway 裁决并持有一次性许可；BLOCK/SUSPEND 路由一律拒绝放行，被拦截的工具返回 GATED_DENIED 并写入审计
- **人工审批**：PERMIT/SUSPEND需人工确认（模拟审批结果）
- **一次性许可**：Permit对象，防重放，使用一次后失效，与具体动作绑定（binding_digest）
- **执行前状态检查**：状态变更则拒绝执行
- **模拟执行**：仅执行白名单内安全动作（DELAY_TASK/REJECT_TASK/SAFE_HOLD）
- **结果验证**：执行前后状态差异校验
- **审计记录**：所有关键事件（审批/许可签发/门控拦截/许可消费）写入审计日志

## 快速开始

### 环境要求
- Python 3.10+
- **零第三方依赖**（纯Python标准库）

### 运行故障注入测试床
```bash
cd OrbitGuard

# 正常模式
python main.py

# 注入指定故障
python main.py --fault process_crash
python main.py --fault wrong_diagnosis
python main.py --fault infinite_loop  # 会卡住，Ctrl+C退出

# 列出所有可用故障
python main.py --list-faults
```

### 运行四模块全链路
```bash
python orchestrator.py                          # 正常场景
python orchestrator.py --fault memory_bloat    # 注入故障
python orchestrator.py --fault wrong_diagnosis --fault inflated_confidence  # 多故障
```

### 运行五模块全链路（含EGM）
```bash
python five_module_orchestrator.py                          # 正常场景
python five_module_orchestrator.py --fault process_crash   # 严重故障→BLOCK→SAFE_HOLD
python five_module_orchestrator.py --approval REJECT       # 模拟人工拒绝
```

### 运行量化实验
```bash
python experiments/run_benchmark.py
```

### 运行全部测试
```bash
python tests/test_faults.py       # 故障注入测试（41项）
python tests/test_rsm.py          # RSM测试（22项）
python tests/test_dtm.py          # DTM测试（17项）
python tests/test_gateway.py      # Gateway测试（18项）
python tests/test_orchestrator.py # 四模块编排器测试（12项）
python tests/test_egm.py          # EGM测试（19项）
python tests/test_five_module.py  # 五模块编排器测试（11项）
python tests/test_gate.py         # 执行前置门控测试（9项）
# 总计：149项测试全部通过
```

### 查看可视化Dashboard
```bash
# 用浏览器打开 dashboard/index.html
```

## 项目结构

```
OrbitGuard/
├── main.py                          # 故障注入测试床入口
├── orchestrator.py                  # 四模块全链路编排器
├── five_module_orchestrator.py      # 五模块全链路编排器（含EGM）
├── config.json                      # 配置文件
├── ROADMAP.md                       # 八周五阶段规划
├── agent/                           # 简化版任务Agent
│   ├── task_agent.py               # 任务Agent主类（6步执行循环）
│   ├── telemetry.py                # 遥测数据源（21项物理量）
│   ├── diagnosis.py                # 模拟诊断引擎
│   ├── tools.py                    # 工具注册表（4个工具）
│   └── noise_model.py              # 遥测噪声模型（4种模式）
├── fault_injector/                  # 故障注入器
│   ├── injector.py                 # 故障注入器主类（装饰器+配置驱动）
│   └── coverage_report.py          # 故障覆盖率量化报告生成器
├── rsm/                             # 运行稳定性监护器
│   ├── monitor.py                  # RSM主类
│   └── detectors/                  # 5个检测器
├── dtm/                             # 输出可信度监护器
│   ├── monitor.py                  # DTM主类
│   └── checkers/                   # 4个检查器
├── gateway/                         # 汇合判定
│   └── gateway.py                  # Gateway主类（C1-C5+三级路由）
├── egm/                             # 执行门控
│   ├── egm.py                      # EGM主类（人工审批+一次性许可+状态机）
│   └── gate.py                     # ExecutionGate（执行前置门控：mutation工具执行前持许可裁决）
├── experiments/                     # 量化实验
│   └── run_benchmark.py            # 基准测试脚本
├── tests/                           # 自动化测试（149项）
├── docs/                            # 文档与实验报告
├── dashboard/                       # 可视化Dashboard
│   └── index.html                  # ECharts实时监控面板
└── logs/                            # 运行日志与审计记录
```

## 技术路线裁决

基于8篇论文的文献调研，确定技术路线为**独立分层形式化监护路线**：
- **底座**：GuardAgent架构（非侵入式监护流水线、代码化校验逻辑）
- **嫁接**：Glass Box物理约束校验模块（6类物理约束模板、LTL不变式范式）
- **否决**：文本过滤派（仅校验内容，无法覆盖动作逻辑）、内嵌Agent派（违反分区隔离规范）

## 量化实验结果

| 实验 | 结果 | 说明 |
|------|------|------|
| RSM稳定性故障检测率 | **100%（5/5）** | infinite_loop/process_crash/no_output_timeout/memory_bloat/repetitive_calls 全部检出 |
| DTM可信性故障检测率 | **100%（4/4）** | wrong_diagnosis/inflated_confidence/missing_evidence/contradictory_sensors 全部检出 |
| 平均检测率 | **100%（9/9）** | RSM 100% + DTM 100% |
| 复合故障检测率 | 100%（3/3组合） | 逐故障匹配（非OR口径）：每个注入故障都被对应检测器检出 |
| 噪声鲁棒性 | 完成率100%，误报率0-100% | 5种噪声模式下Agent均正常完成；none/gaussian_light/packet_loss零误报；spike 33.3%（脉冲干扰偶发触发多源矛盾）；gaussian_heavy(σ=20%)下遥测剧烈波动触发DTM多源矛盾检查，误报率100%（重噪本身应视为异常） |
| 监护处理开销 | 0.2ms（RSM+DTM+Gateway） | 占Agent执行时间0.04% |
| 故障空间覆盖率 | 45.5%（10/22维） | 22维故障空间定义，packet_loss 已计入 |
| 自动化测试 | 149项全部通过 | 8个测试文件 |
| 第三方依赖 | 0（纯Python标准库） | |

**检测率说明（诚实披露）：**
- `process_crash`：通过**独立看门狗线程+墙钟超时**检测，100%检出（P1架构改进已完成）
- `no_output_timeout`：通过**CPU特征独立判别**——工具挂起且 CPU 占用低（阻塞特征）判 NO_OUTPUT_TIMEOUT，挂起且 CPU 高（忙等特征）判 INFINITE_LOOP；执行方回报超时（tool_result 含 timed out）作为第二判据互相印证
- `memory_bloat`：通过**阶梯式持续内存分配**（每个资源采样周期分配50MB，上限400MB）+ MemoryMonitor绝对阈值(50MB)检测，100%检出；修复了Windows平台`_get_memory_mb()`返回0的bug（改用OpenProcess获取真实句柄）
- `inflated_confidence`：注入时若诊断为NO_FAULT，先生成假故障结论（SA_BRANCH_SHORT），再设高置信度(0.95-0.99)+少证据(<=1条)，触发DTM置信度校准检查
- `missing_evidence`：注入时若诊断为NO_FAULT，先生成假故障结论，再清空supporting_evidence，触发DTM证据完整性检查
- `contradictory_sensors`：注入bus_current=0（保持bus_voltage正常≈28V），触发DTM多源矛盾检查规则"电压正常但电流为0"

**执行前置门控（P0架构修复）说明：**
- 编排器把 RSM/DTM/Gate 接到**实时事件总线**上，mutation 工具在执行前用"此刻的累积监护状态 + 最新遥测 + 动作意图"向 Gate 请求许可
- 只有 PERMIT 路由由 EGM 签发一次性许可；BLOCK/SUSPEND 一律拒绝（自动模式严格态），被拦截的工具返回 GATED_DENIED，最终结果标记 TOOL_GATED
- 演示：`python five_module_orchestrator.py --fault wrong_diagnosis` → DTM trust 0.7 → SUSPEND → 切换太阳阵支路的 mutation 工具被拦截

## 许可证

MIT License
