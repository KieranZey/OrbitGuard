# -*- coding: utf-8 -*-
"""
执行前置门控（ExecutionGate）—— P0 架构修复。

背景：
    原五模块编排链路是"Agent 先跑完所有工具 → Gateway 事后汇合判定 → EGM 事后模拟执行"。
    审计反复指出：mutation 工具（切母线、切太阳阵支路等）在判定之前已经真实执行，
    "执行门控"没有任何拦截能力。

本模块把 EGM 前移到执行路径上：

    mutation 工具在执行前，必须通过 ExecutionGate.authorize() 拿到一次性许可；
    BLOCK 路由 → 拒绝放行；
    SUSPEND 路由 → 拒绝放行（SUSPEND 意为"挂起待人工"，自动模式下不允许执行）；
    PERMIT 路由 → 由 EGM 签发一次性许可（Permit），执行时消费，重复使用即拒绝（防重放）；
    声明 requires_approval 的工具，还要求门控策略显式 APPROVE。

设计原则：
    1. 门控是"最外层防线"：先于工具真实执行，也先于故障注入钩子。
    2. 非侵入：TaskAgent 未接入 gate 时，行为与原来完全一致（gate=None 时零开销）。
    3. 可追溯：每次 authorize / deny / consume 都写入 EGM 审计日志，
       TaskAgent 同时发布 gate_decision 事件，全链路可查。

与事后复核（post-run review）的关系：
    编排器在 Agent 跑完后仍会做一次 Gateway/EGM 复核并归档审计——
    前置门控负责"拦住不该执行的"，事后复核负责"给已发生的事情留下可追溯的裁决记录"。
"""
import time
import hashlib
import json
from typing import Dict, Any, Optional, List

from rsm.monitor import RuntimeStabilityMonitor
from dtm.monitor import DecisionTrustMonitor
from gateway.gateway import DecisionGateway
from egm.egm import ExecutionGateModule, EGMState


class GateVerdict:
    """单次门控裁决结果。"""

    def __init__(self, approved: bool, route: str = "", reason: str = "",
                 permit_id: Optional[str] = None,
                 binding_digest: Optional[str] = None,
                 gateway_decision: Optional[Dict[str, Any]] = None):
        self.approved = approved
        self.route = route
        self.reason = reason
        self.permit_id = permit_id
        self.binding_digest = binding_digest
        self.gateway_decision = gateway_decision or {}
        self.timestamp_ms = int(time.time() * 1000)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "approved": self.approved,
            "route": self.route,
            "reason": self.reason,
            "permit_id": self.permit_id,
            "binding_digest": self.binding_digest,
            "timestamp_ms": self.timestamp_ms,
        }


class ExecutionGate:
    """
    执行前置门控。

    组合 RSM + DTM + Gateway + EGM：
        authorize() 用"当前累积监护状态 + 最新遥测 + 动作意图"跑一次 Gateway 裁决，
        只有 PERMIT 路由才由 EGM 签发一次性许可；许可与动作绑定（binding_digest），
        执行时 consume() 消费，重复消费（重放）返回 False。
    """

    def __init__(self, rsm: RuntimeStabilityMonitor, dtm: DecisionTrustMonitor,
                 gateway: DecisionGateway, egm: ExecutionGateModule,
                 human_approval: str = "REJECT"):
        self.rsm = rsm
        self.dtm = dtm
        self.gateway = gateway
        self.egm = egm
        # 对 requires_approval 工具的模拟人工审批策略（APPROVE / REJECT）
        self.human_approval = human_approval if human_approval in ("APPROVE", "REJECT") else "REJECT"
        self._latest_telemetry: Optional[Dict[str, Any]] = None
        self._issued: Dict[str, Dict[str, Any]] = {}  # permit_id -> {"permit","binding","used"}
        self._decisions: List[GateVerdict] = []
        self._lock = __import__("threading").Lock()

    # ------------------------------------------------------------------
    # 事件观察（由编排器的事件总线实时喂入，维护最新遥测快照）
    # ------------------------------------------------------------------
    def observe(self, event: Dict[str, Any]):
        """实时事件观察：仅跟踪遥测快照，供 authorize() 做物理约束判定。"""
        if event.get("event_type") == "telemetry":
            self._latest_telemetry = event

    # ------------------------------------------------------------------
    # 门控主流程
    # ------------------------------------------------------------------
    @staticmethod
    def binding_digest_for(tool_call_event: Dict[str, Any]) -> str:
        """动作绑定摘要：许可与具体动作实例绑定，防止许可被挪用于其他动作。"""
        payload = {
            "tool_name": tool_call_event.get("tool_name", ""),
            "args": tool_call_event.get("args"),
            "call_id": tool_call_event.get("call_id", ""),
            "power_delta_w": tool_call_event.get("power_delta_w", 0),
            "thermal_delta_c": tool_call_event.get("thermal_delta_c", 0),
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]

    def authorize(self, tool_call_event: Dict[str, Any]) -> GateVerdict:
        """
        对一次工具调用做前置门控。

        Args:
            tool_call_event: 已发布的 tool_call 事件（含动作意图元数据）。

        Returns:
            GateVerdict；approved=False 时调用方必须拒绝执行。
        """
        binding_digest = self.binding_digest_for(tool_call_event)
        action_intent = {
            "tool_name": tool_call_event.get("tool_name", ""),
            "power_delta_w": tool_call_event.get("power_delta_w", 0),
            "thermal_delta_c": tool_call_event.get("thermal_delta_c", 0),
            "is_reversible": tool_call_event.get("is_reversible", True),
            "requires_approval": tool_call_event.get("requires_approval", False),
            "action_category": tool_call_event.get("action_category", ""),
            "is_mutation": tool_call_event.get("is_mutation", False),
        }

        # 1. 需要人工审批的工具：策略必须是 APPROVE 才继续，否则直接拒绝
        if action_intent.get("requires_approval") and self.human_approval != "APPROVE":
            return self._deny("SUSPEND", binding_digest,
                              f"工具 {action_intent['tool_name']} 声明 requires_approval，"
                              f"当前人工审批策略={self.human_approval}")

        # 2. 用当前累积监护状态 + 最新遥测 + 动作意图做 Gateway 裁决
        rsm_res = self.rsm.get_last_result()
        dtm_res = self.dtm.get_last_result()
        gateway_decision = self.gateway.decide(
            rsm_result=rsm_res.to_dict() if rsm_res else None,
            dtm_result=dtm_res.to_dict() if dtm_res else None,
            telemetry=self._latest_telemetry,
            action_intent=action_intent,
        )

        # 3. 三级路由 → 门控裁决
        if gateway_decision.route == "BLOCK":
            return self._deny(gateway_decision.route, binding_digest,
                              "Gateway 判定 BLOCK：" + "; ".join(gateway_decision.reasons[:2]),
                              gateway_decision)
        if gateway_decision.route == "SUSPEND":
            # SUSPEND = 挂起待人工。自动模式（人工审批策略=REJECT）拒绝放行；
            # 仅当人工审批策略=APPROVE（模拟操作员显式确认放行）时才继续签发许可。
            if self.human_approval != "APPROVE":
                return self._deny(gateway_decision.route, binding_digest,
                                  "Gateway 判定 SUSPEND（需人工审批），当前人工审批策略=REJECT，"
                                  "自动模式拒绝放行：" + "; ".join(gateway_decision.reasons[:2]),
                                  gateway_decision)

        # 4. PERMIT（或人工批准放行的 SUSPEND）：由 EGM 签发一次性许可
        egm_output = self.egm.submit_gateway_decision(
            gateway_decision=gateway_decision.to_dict(),
            human_approval="APPROVE",
        )
        if egm_output.egm_state != EGMState.PERMIT_ISSUED.value or not egm_output.permit_id:
            return self._deny(gateway_decision.route, binding_digest,
                              f"EGM 未签发许可：{egm_output.rejection_reason or egm_output.egm_state}",
                              gateway_decision)

        permit = self.egm.get_permit(egm_output.permit_id)
        if permit is None:
            return self._deny(gateway_decision.route, binding_digest,
                              f"EGM 许可对象缺失：{egm_output.permit_id}",
                              gateway_decision)

        with self._lock:
            self._issued[permit.permit_id] = {
                "permit": permit,
                "binding": binding_digest,
                "used": False,
            }

        self.egm.audit_logger.log("GATE_AUTHORIZED", {
            "tool_name": action_intent["tool_name"],
            "route": gateway_decision.route,
            "permit_id": permit.permit_id,
            "binding_digest": binding_digest,
        }, operator="gate")

        verdict = GateVerdict(
            approved=True,
            route=gateway_decision.route,
            reason="PERMIT：EGM 已签发一次性许可",
            permit_id=permit.permit_id,
            binding_digest=binding_digest,
            gateway_decision=gateway_decision.to_dict(),
        )
        with self._lock:
            self._decisions.append(verdict)
        return verdict

    def consume(self, permit_id: str, binding_digest: str = "") -> bool:
        """
        消费一次性许可（工具真实执行时调用）。

        返回 False 的情况：
            - permit_id 未知；
            - 许可已被消费（重放攻击）；
            - binding_digest 与签发时绑定的动作不匹配（许可被挪用）。
        """
        with self._lock:
            entry = self._issued.get(permit_id)
            if entry is None:
                self.egm.audit_logger.log("GATE_CONSUME_REJECTED",
                                          {"permit_id": permit_id, "reason": "permit_not_found"},
                                          operator="gate")
                return False
            if entry["used"]:
                self.egm.audit_logger.log("GATE_CONSUME_REJECTED",
                                          {"permit_id": permit_id, "reason": "replay"},
                                          operator="gate")
                return False
            if binding_digest and entry["binding"] != binding_digest:
                self.egm.audit_logger.log("GATE_CONSUME_REJECTED",
                                          {"permit_id": permit_id, "reason": "binding_mismatch",
                                           "expected": entry["binding"], "got": binding_digest},
                                          operator="gate")
                return False
            entry["used"] = True
            permit = entry["permit"]
        # EGM 层的 Permit 同步消费（其自身也有防重放语义）
        if not permit.consume():
            return False
        self.egm.audit_logger.log("GATE_PERMIT_CONSUMED",
                                  {"permit_id": permit_id, "binding_digest": binding_digest},
                                  operator="gate")
        return True

    # ------------------------------------------------------------------
    # 辅助
    # ------------------------------------------------------------------
    def _deny(self, route: str, binding_digest: str, reason: str,
              gateway_decision=None) -> GateVerdict:
        self.egm.audit_logger.log("GATE_DENIED", {
            "route": route,
            "binding_digest": binding_digest,
            "reason": reason,
        }, operator="gate")
        verdict = GateVerdict(
            approved=False,
            route=route,
            reason=reason,
            binding_digest=binding_digest,
            gateway_decision=gateway_decision.to_dict() if hasattr(gateway_decision, "to_dict") else {},
        )
        with self._lock:
            self._decisions.append(verdict)
        return verdict

    def get_summary(self) -> Dict[str, Any]:
        with self._lock:
            decisions = list(self._decisions)
        authorized = sum(1 for d in decisions if d.approved)
        denied = [d.to_dict() for d in decisions if not d.approved]
        return {
            "total_checked": len(decisions),
            "authorized": authorized,
            "denied": len(denied),
            "denials": denied,
        }

    def get_issued_permits(self) -> Dict[str, Dict[str, Any]]:
        with self._lock:
            return {
                pid: {"binding": e["binding"], "used": e["used"]}
                for pid, e in self._issued.items()
            }
