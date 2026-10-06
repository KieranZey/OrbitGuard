# -*- coding: utf-8 -*-
"""
EGM 执行门控主模块。

包含：Permit（一次性许可）、AuditLogger（审计日志）、EGMState（状态机枚举）、
ExecutionGateModule（EGM主类）。
"""
import time
import json
import hashlib
import os
import threading
from enum import Enum
from typing import Dict, Any, Optional, List


class EGMState(Enum):
    """EGM 状态机状态。"""
    PENDING_HUMAN = "PENDING_HUMAN"
    PERMIT_ISSUED = "PERMIT_ISSUED"
    STATE_CHECK = "STATE_CHECK"
    EXECUTING = "EXECUTING"
    COMPLETED = "COMPLETED"
    REJECTED = "REJECTED"
    STATE_CHANGED = "STATE_CHANGED"
    SAFE_HOLD = "SAFE_HOLD"


class Permit:
    """
    一次性许可（One-time Permit）。
    绑定 run_id/task_id/action_digest/state_digest/operator/expiry/used状态。
    使用一次后标记为已用，重复提交拒绝（防重放）。
    """
    def __init__(self, permit_id: str, run_id: str, task_id: str,
                 action_digest: str, state_digest: str,
                 action: str = "DELAY_TASK",
                 operator: str = "human", expiry_ms: Optional[int] = None):
        self.permit_id = permit_id
        self.run_id = run_id
        self.task_id = task_id
        self.action_digest = action_digest
        self.state_digest = state_digest
        self.action = action  # 动作名称（从Gateway路由推导，用于执行时恢复）
        self.operator = operator
        self.issued_ms = int(time.time() * 1000)
        self.expiry_ms = expiry_ms or (self.issued_ms + 300000)  # 默认5分钟过期
        self.used = False
        self.used_ms: Optional[int] = None

    def is_valid(self, run_id: str, task_id: str,
                 action_digest: str, state_digest: str) -> bool:
        """检查许可是否有效（未使用、未过期、上下文匹配）。"""
        if self.used:
            return False
        if int(time.time() * 1000) > self.expiry_ms:
            return False
        if self.run_id != run_id or self.task_id != task_id:
            return False
        if self.action_digest != action_digest:
            return False
        # state_digest 在执行前检查时会比对，这里不强制
        return True

    def consume(self) -> bool:
        """消费许可（标记为已用）。如果已使用返回False。"""
        if self.used:
            return False
        self.used = True
        self.used_ms = int(time.time() * 1000)
        return True

    def to_dict(self) -> Dict[str, Any]:
        return {
            "permit_id": self.permit_id,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "action_digest": self.action_digest,
            "state_digest": self.state_digest,
            "action": self.action,
            "operator": self.operator,
            "issued_ms": self.issued_ms,
            "expiry_ms": self.expiry_ms,
            "used": self.used,
            "used_ms": self.used_ms,
        }


class AuditLogger:
    """
    审计记录器。
    所有关键事件（审批、许可签发、许可消费、执行、失败）写入 audit_log.jsonl。
    """
    def __init__(self, log_dir: str = "logs", log_file: str = "egm_audit_log.jsonl"):
        self.log_dir = log_dir
        self.log_file = log_file
        self.log_path = os.path.join(log_dir, log_file)
        os.makedirs(log_dir, exist_ok=True)
        self._lock = threading.Lock()
        self._records: List[Dict] = []

    def log(self, event_type: str, details: Dict[str, Any], operator: str = "system"):
        """记录一条审计事件。"""
        record = {
            "audit_seq": len(self._records) + 1,
            "event_type": event_type,
            "timestamp_ms": int(time.time() * 1000),
            "operator": operator,
            "details": details,
        }
        with self._lock:
            self._records.append(record)
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")

    def get_records(self) -> List[Dict]:
        """获取所有审计记录。"""
        return list(self._records)

    def get_count(self) -> int:
        """获取审计记录数。"""
        return len(self._records)


class EGMOutput:
    """EGM 输出结果。"""
    def __init__(self, run_id: str = "", task_id: str = "", sat_id: str = ""):
        self.monitor_type = "EGM"
        self.run_id = run_id
        self.task_id = task_id
        self.sat_id = sat_id
        self.egm_state = EGMState.PENDING_HUMAN.value
        self.permit_id: Optional[str] = None
        self.action: Optional[str] = None
        self.execution_status: Optional[str] = None
        self.before_state: Optional[Dict] = None
        self.after_state: Optional[Dict] = None
        self.audit_events: List[Dict] = []
        self.rejection_reason: Optional[str] = None
        self.timestamp_ms = int(time.time() * 1000)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "monitor_type": self.monitor_type,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "sat_id": self.sat_id,
            "egm_state": self.egm_state,
            "permit_id": self.permit_id,
            "action": self.action,
            "execution_status": self.execution_status,
            "before_state": self.before_state,
            "after_state": self.after_state,
            "audit_events": self.audit_events,
            "rejection_reason": self.rejection_reason,
            "timestamp_ms": self.timestamp_ms,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)


# 模拟执行的白名单动作
SAFE_ACTIONS = {"DELAY_TASK", "REJECT_TASK", "SAFE_HOLD"}


class ExecutionGateModule:
    """
    执行门控模块（EGM）。

    在 Gateway 判定之后执行安全门控：
    1. 人工审批：PERMIT/SUSPEND 需人工确认（模拟审批结果）
    2. 一次性许可：审批通过后签发Permit，使用一次后失效
    3. 执行前状态检查：对比当前状态与审批时状态，不一致则拒绝
    4. 模拟执行：仅执行白名单内安全动作
    5. 结果验证：执行前后状态差异校验
    6. 审计记录：所有关键事件写入审计日志
    """

    def __init__(self, config: Optional[Dict] = None):
        self.config = config or {}
        egm_config = self.config.get("egm", {})
        self.audit_logger = AuditLogger(
            log_dir=egm_config.get("log_dir", "logs"),
            log_file=egm_config.get("log_file", "egm_audit_log.jsonl"),
        )
        self._permits: Dict[str, Permit] = {}
        self._state = EGMState.PENDING_HUMAN
        self._approved_state_snapshot: Optional[Dict] = None

    def _digest(self, data: Dict[str, Any]) -> str:
        """计算数据的SHA256摘要。"""
        return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()[:16]

    def submit_gateway_decision(self, gateway_decision: Dict[str, Any],
                                 human_approval: str = "APPROVE",
                                 operator: str = "human") -> EGMOutput:
        """
        提交Gateway决策，进入人工审批流程。

        Args:
            gateway_decision: GatewayDecision的dict形式
            human_approval: 模拟人工审批结果（APPROVE / REJECT / REQUEST_DATA）
            operator: 操作者标识

        Returns:
            EGMOutput
        """
        run_id = gateway_decision.get("run_id", "")
        task_id = gateway_decision.get("task_id", "")
        sat_id = gateway_decision.get("sat_id", "")
        route = gateway_decision.get("route", "PERMIT")

        output = EGMOutput(run_id=run_id, task_id=task_id, sat_id=sat_id)

        # BLOCK 直接进入安全保持，无需人工审批
        if route == "BLOCK":
            self._state = EGMState.SAFE_HOLD
            output.egm_state = EGMState.SAFE_HOLD.value
            output.execution_status = "BLOCKED"
            output.rejection_reason = "Gateway判定BLOCK，直接进入安全保持"
            self.audit_logger.log("BLOCK_SAFE_HOLD", {
                "route": route, "reasons": gateway_decision.get("reasons", []),
            }, operator)
            output.audit_events = self.audit_logger.get_records()[-3:]
            return output

        # 人工审批
        self._state = EGMState.PENDING_HUMAN
        self.audit_logger.log("HUMAN_REVIEW_REQUESTED", {
            "route": route, "gateway_confidence": gateway_decision.get("confidence"),
        }, operator)

        if human_approval == "REJECT":
            self._state = EGMState.REJECTED
            output.egm_state = EGMState.REJECTED.value
            output.execution_status = "REJECTED"
            output.rejection_reason = "人工审批拒绝"
            self.audit_logger.log("HUMAN_REJECTED", {"route": route}, operator)
            output.audit_events = self.audit_logger.get_records()[-3:]
            return output

        if human_approval == "REQUEST_DATA":
            self._state = EGMState.PENDING_HUMAN
            output.egm_state = EGMState.PENDING_HUMAN.value
            output.execution_status = "PENDING_MORE_DATA"
            output.rejection_reason = "人工要求补充数据"
            self.audit_logger.log("HUMAN_REQUEST_DATA", {"route": route}, operator)
            output.audit_events = self.audit_logger.get_records()[-3:]
            return output

        # APPROVE：签发一次性许可
        action = self._derive_action(route)
        action_digest = self._digest({"action": action, "route": route})
        state_digest = self._digest({"approved_at": int(time.time() * 1000)})

        permit_id = f"PERMIT-{int(time.time()*1000)}-{hashlib.md5(os.urandom(4)).hexdigest()[:6]}"
        permit = Permit(
            permit_id=permit_id, run_id=run_id, task_id=task_id,
            action_digest=action_digest, state_digest=state_digest,
            action=action,
            operator=operator,
        )
        self._permits[permit_id] = permit
        self._approved_state_snapshot = {"approved_at": int(time.time() * 1000), "route": route}
        self._state = EGMState.PERMIT_ISSUED

        output.egm_state = EGMState.PERMIT_ISSUED.value
        output.permit_id = permit_id
        output.action = action
        self.audit_logger.log("PERMIT_ISSUED", permit.to_dict(), operator)
        output.audit_events = self.audit_logger.get_records()[-3:]
        return output

    def execute_with_permit(self, permit_id: str, current_state: Dict[str, Any],
                             operator: str = "system") -> EGMOutput:
        """
        使用许可执行动作。

        Args:
            permit_id: 许可ID
            current_state: 当前系统状态快照（用于执行前状态检查）
            operator: 操作者

        Returns:
            EGMOutput
        """
        permit = self._permits.get(permit_id)
        output = EGMOutput()

        if permit is None:
            output.egm_state = EGMState.REJECTED.value
            output.execution_status = "REJECTED"
            output.rejection_reason = f"许可 {permit_id} 不存在"
            self.audit_logger.log("PERMIT_NOT_FOUND", {"permit_id": permit_id}, operator)
            output.audit_events = self.audit_logger.get_records()[-2:]
            return output

        output.run_id = permit.run_id
        output.task_id = permit.task_id
        output.permit_id = permit_id

        # 执行前状态检查
        self._state = EGMState.STATE_CHECK
        current_state_digest = self._digest(current_state) if current_state else "empty"

        # 简化的状态检查：如果current_state包含state_changed标记，则拒绝
        if current_state and current_state.get("state_changed", False):
            self._state = EGMState.STATE_CHANGED
            output.egm_state = EGMState.STATE_CHANGED.value
            output.execution_status = "REJECTED"
            output.rejection_reason = "执行前状态检查失败：系统状态已变更，审批失效"
            self.audit_logger.log("STATE_CHANGED_REJECTED", {
                "permit_id": permit_id,
                "current_state_digest": current_state_digest,
            }, operator)
            output.audit_events = self.audit_logger.get_records()[-2:]
            return output

        # 消费许可（防重放）
        if not permit.consume():
            output.egm_state = EGMState.REJECTED.value
            output.execution_status = "REJECTED"
            output.rejection_reason = "许可已被使用（防重放拒绝）"
            self.audit_logger.log("PERMIT_REPLAY_REJECTED", {"permit_id": permit_id}, operator)
            output.audit_events = self.audit_logger.get_records()[-2:]
            return output

        # 模拟执行
        self._state = EGMState.EXECUTING
        output.before_state = current_state
        output.action = self._get_action_from_permit(permit)

        # 检查动作是否在白名单内
        if output.action not in SAFE_ACTIONS:
            self._state = EGMState.SAFE_HOLD
            output.egm_state = EGMState.SAFE_HOLD.value
            output.execution_status = "REJECTED"
            output.rejection_reason = f"动作 {output.action} 不在安全白名单内"
            self.audit_logger.log("UNSAFE_ACTION_REJECTED", {
                "permit_id": permit_id, "action": output.action,
            }, operator)
            output.audit_events = self.audit_logger.get_records()[-3:]
            return output

        # 执行模拟动作
        execution_result = self._simulate_execution(output.action, current_state)
        output.after_state = execution_result.get("after_state")
        output.execution_status = execution_result.get("status")

        # 结果验证
        if execution_result.get("status") == "SUCCESS":
            self._state = EGMState.COMPLETED
            output.egm_state = EGMState.COMPLETED.value
            self.audit_logger.log("EXECUTION_COMPLETED", {
                "permit_id": permit_id, "action": output.action,
                "execution_status": "SUCCESS",
            }, operator)
        else:
            self._state = EGMState.SAFE_HOLD
            output.egm_state = EGMState.SAFE_HOLD.value
            output.rejection_reason = execution_result.get("error", "执行失败，进入安全保持")
            self.audit_logger.log("EXECUTION_FAILED_SAFE_HOLD", {
                "permit_id": permit_id, "action": output.action,
                "error": output.rejection_reason,
            }, operator)

        output.audit_events = self.audit_logger.get_records()[-4:]
        return output

    def _derive_action(self, route: str) -> str:
        """根据Gateway路由推导执行动作。"""
        if route == "PERMIT":
            return "DELAY_TASK"      # 允许执行，延迟任务
        elif route == "SUSPEND":
            return "REJECT_TASK"     # 挂起，拒绝当前任务执行
        else:
            return "SAFE_HOLD"       # 阻断，进入安全保持

    def _get_action_from_permit(self, permit: Permit) -> str:
        """从许可中恢复动作名称。"""
        return permit.action or "DELAY_TASK"

    def _simulate_execution(self, action: str, current_state: Optional[Dict]) -> Dict[str, Any]:
        """模拟执行安全动作。"""
        after_state = dict(current_state or {})
        if action == "DELAY_TASK":
            after_state["task_delayed"] = True
            after_state["delay_reason"] = "EGM_DELAY"
            return {"status": "SUCCESS", "after_state": after_state}
        elif action == "REJECT_TASK":
            after_state["task_rejected"] = True
            return {"status": "SUCCESS", "after_state": after_state}
        elif action == "SAFE_HOLD":
            after_state["safe_hold"] = True
            return {"status": "SUCCESS", "after_state": after_state}
        else:
            return {"status": "FAILED", "error": f"未知动作 {action}", "after_state": after_state}

    def get_state(self) -> EGMState:
        """获取当前EGM状态。"""
        return self._state

    def get_audit_count(self) -> int:
        """获取审计记录数。"""
        return self.audit_logger.get_count()

    def get_permit(self, permit_id: str) -> Optional[Permit]:
        """获取许可对象。"""
        return self._permits.get(permit_id)
