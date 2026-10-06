# OrbitGuard 论文框架

## 题目（候选）
1. OrbitGuard: A Five-Module Trustworthy Monitoring and Execution Gating Framework for On-Orbit Agents
2. 面向星载Agent的全链路可信监护与执行门控框架研究
3. 融合物理约束校验与多因子置信度的星载Agent运行时监护架构

## 摘要

针对星载故障诊断与任务规划Agent在空间辐射环境下面临的运行不稳定与输出决策不可信问题，本文提出OrbitGuard——一个五模块端到端可信监护与执行门控框架。该框架包含故障注入测试床（支持9类故障、多故障叠加、3档强度分级、4种传感器噪声模式）、运行稳定性监护器（RSM，5类异常检测）、输出可信度评估器（DTM，4类可信度检查，输出0-1信任分）、汇合判定器（Gateway，C1-C5物理约束检查与PERMIT/SUSPEND/BLOCK三级路由）、执行门控器（EGM，人工审批、一次性许可防重放、执行前状态检查、失败安全保持与审计记录）。系统采用独立分层形式化监护架构，以GuardAgent为底座嫁接Glass Box物理约束校验模块，严守辅助决策、人最终裁决的航天安全边界。实验表明，DTM可信性故障检测率达100%，监护处理开销<1ms，系统零第三方依赖，138项自动化测试全部通过。

**关键词**：星载Agent；可信监护；故障注入；运行时验证；执行门控；形式化校验

## 1. 引言

### 1.1 研究背景
- 星载Agent在自主故障诊断与任务规划中的应用趋势
- 空间辐射环境（单粒子翻转SEU）对Agent运行稳定性的威胁
- Agent输出决策的可信性问题：错误诊断、置信度虚高、证据缺失

### 1.2 问题定义
- 运行稳定性问题：死锁、无限推理、进程崩溃、内存泄漏
- 决策可信性问题：结论与证据不一致、置信度校准偏差、多源数据矛盾
- 航天安全边界：不追求完全自主，必须人最终裁决

### 1.3 本文贡献
1. 提出五模块全链路可信监护架构（FaultInjector→RSM→DTM→Gateway→EGM）
2. 设计配置驱动、非侵入式故障注入框架，支持9类故障×多故障叠加×强度分级×噪声模式
3. 实现C1-C5物理约束校验与三级路由判定，融合形式化验证与规则引擎
4. 设计一次性许可防重放与执行前状态检查的安全门控机制
5. 完成量化实验与138项自动化测试验证

## 2. 相关工作

### 2.1 Agent监护与安全
- GuardAgent: Safeguard LLM Agents by a Guard Agent via Knowledge-Enabled Reasoning
- GUARDIAN: Safeguarding LLM Multi-Agent Collaborations with Temporal Graph Modeling
- Shielding Agents via Verifiable Safety Policy Reasoning
- Safeguarding AI Agents: Developing and Analyzing Safety Architectures

### 2.2 星载形式化验证
- Glass Box at Orbit: A Constitutional AI Verification Framework for Trustworthy Autonomous CubeSat Intelligence
- 宪法式运行时验证，硬约束违规拦截率100%，验证复杂度O(N_c)

### 2.3 置信度量化与不确定性
- FaMSeC: A Factor-Based Framework for Decision-Making Competency Self-Assessment
- KNOWNO: Robots That Ask For Help — Uncertainty Alignment for Large Language Model Planners
- 多Agent信任框架（空间域感知）

### 2.4 现有方案不足
- 文本过滤派：仅校验内容，无法覆盖动作逻辑与物理边界
- 内嵌Agent派：违反航天软件分区隔离规范，单粒子翻转易引发连带失效
- Glass Box：仅覆盖物理硬约束，缺失运行态异常防护与决策置信度量化

## 3. 方法

### 3.1 整体架构
- 五模块流水线设计
- 事件驱动架构（8类结构化事件）
- 独立分层形式化监护路线

### 3.2 FaultInjector — 故障注入测试床
- 9类故障分类学（稳定性5类+可信性4类）
- 多故障叠加引擎（终结性故障互斥）
- 故障强度分级（light/medium/heavy）
- 真实遥测噪声模型（高斯/漂移/脉冲/丢包）
- 非侵入式装饰器架构
- GroundTruthLogger（运行时不可见，保证评估公正性）

### 3.3 RSM — 运行稳定性监护器
- 心跳检测（HeartbeatMonitor）
- 无进展检测（StallMonitor）
- 工具超时检测（ToolTimeoutMonitor）
- 内存检测（MemoryMonitor，滑动窗口增长趋势）
- 重复调用检测（RepetitiveCallMonitor）
- 统一RSMResult输出

### 3.4 DTM — 输出可信度监护器
- 结论一致性检查（故障码→遥测验证规则映射）
- 置信度校准检查（高置信度+低证据→虚高）
- 证据完整性检查
- 多源矛盾检查（电压-电流矛盾、功率计算矛盾、温度矛盾）
- trust_score量化与risk_label

### 3.5 Gateway — 汇合判定
- C1功率约束、C2热约束、C3碰撞约束、C4电池约束、C5辐射约束
- 三级路由判定逻辑（BLOCK>SUSPEND>PERMIT）
- 遏制措施（ISOLATE/TOOL_RESTRICT/PLANNING_BLOCK）

### 3.6 EGM — 执行门控
- 人工审批流程（APPROVE/REJECT/REQUEST_DATA）
- 一次性许可（Permit对象，防重放，5分钟过期）
- 执行前状态检查（状态变更拒绝执行）
- 模拟执行（白名单安全动作）
- 结果验证（执行前后状态差异）
- 审计记录（jsonl格式）
- 状态机（PENDING_HUMAN→PERMIT_ISSUED→STATE_CHECK→EXECUTING→COMPLETED/SAFE_HOLD）

## 4. 实验

### 4.1 实验设置
- 环境：Python 3.13，零第三方依赖
- 故障注入：9类故障，每类5次实验
- 评估指标：检测率、稳定率、处理延迟

### 4.2 单故障检测率
- DTM可信性故障：100%（4/4）
- RSM稳定性故障：短实验窗口下需更敏感阈值
- 分析与讨论

### 4.3 复合故障检测率
- 3种复合故障组合，检测率100%

### 4.4 噪声鲁棒性
- 5种噪声模式（无噪声/高斯轻/高斯重/脉冲/丢包），稳定率100%

### 4.5 性能开销
- Agent执行：~500ms
- RSM+DTM处理：<1ms
- Gateway判定：<1ms
- 监护总开销：<1ms（占Agent执行时间0.2%）

### 4.6 故障空间覆盖率
- 22维故障空间，当前覆盖9维（40.9%）
- 13项空白区域及扩展建议

## 5. 结论与未来工作

### 5.1 结论
- 提出并实现了五模块全链路可信监护框架
- 验证了独立分层形式化监护路线的可行性
- 零第三方依赖，适合星载嵌入式环境部署

### 5.2 未来工作
- 扩展故障空间覆盖率（CPU耗尽、状态死锁、证据矛盾等）
- RSM稳定性检测器的自适应阈值优化
- 真实星载硬件部署与在轨验证
- 与LLM-based Agent的集成（当前为规则驱动简化Agent）
- 跨进程LTL状态持久化

## 参考文献

[1] GuardAgent: Safeguard LLM Agents by a Guard Agent via Knowledge-Enabled Reasoning
[2] GUARDIAN: Safeguarding LLM Multi-Agent Collaborations with Temporal Graph Modeling
[3] Shielding Agents via Verifiable Safety Policy Reasoning
[4] Safeguarding AI Agents: Developing and Analyzing Safety Architectures
[5] Glass Box at Orbit: A Constitutional AI Verification Framework for Trustworthy Autonomous CubeSat Intelligence
[6] A Factor-Based Framework for Decision-Making Competency Self-Assessment (FaMSeC)
[7] KNOWNO: Robots That Ask For Help — Uncertainty Alignment for Large Language Model Planners
[8] A multi-agent trust framework for fusing subjective opinions with imperfect understanding in space domain awareness
