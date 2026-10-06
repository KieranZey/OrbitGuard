# -*- coding: utf-8 -*-
"""
EGM（Execution Gate Module）执行门控模块。

在 Gateway 汇合判定之后，执行最终的安全门控：
1. 人工审批（Human Review）：PERMIT/SUSPEND 需人工确认
2. 一次性许可（One-time Permit）：防重放，使用一次后失效
3. 执行前状态检查（Pre-execution State Check）：状态变更则拒绝执行
4. 模拟执行（Simulated Execution）：仅执行白名单内安全动作
5. 结果验证（Result Verification）：执行前后状态差异校验
6. 审计记录（Audit Log）：所有关键事件写入审计日志

状态机：
  PENDING_HUMAN → (审批通过) → PERMIT_ISSUED → (执行请求) → STATE_CHECK
    → (通过) → EXECUTING → (成功) → COMPLETED
    → (失败) → SAFE_HOLD
  任何阶段状态变更 → STATE_CHANGED（拒绝执行）
"""
from .egm import ExecutionGateModule, EGMOutput, Permit, AuditLogger, EGMState

__all__ = ["ExecutionGateModule", "EGMOutput", "Permit", "AuditLogger", "EGMState"]
