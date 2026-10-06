# -*- coding: utf-8 -*-
"""
Gateway 汇合判定器。

C1-C5 物理约束：
  C1 功率约束：power_consumption_w < power_generation_w * safety_factor
  C2 热约束：temp_obc < max_temp_obc, temp_battery < max_temp_battery
  C3 碰撞约束：collision_risk < max_collision_risk
  C4 电池约束：battery_soc > min_battery_soc
  C5 辐射约束：seu_rate < max_seu_rate

三级路由：
  BLOCK：RSM检测到critical故障 或 DTM trust_score < 0.5
  SUSPEND：RSM检测到故障 或 DTM trust_score < 0.8 或 C1-C5有违规
  PERMIT：所有检查通过
"""
import time
import json
from typing import Dict, Any, Optional, List


# 物理约束默认阈值
DEFAULT_CONSTRAINTS = {
    "C1_power": {
        "name": "功率约束",
        "safety_factor": 0.95,  # 功耗必须小于发电*0.95（遥测比值约0.93，留余量）
        "description": "power_consumption_w < power_generation_w * safety_factor",
    },
    "C2_thermal": {
        "name": "热约束",
        "max_temp_obc": 60.0,
        "max_temp_battery": 45.0,
        "description": "temp_obc < 60°C, temp_battery < 45°C",
    },
    "C3_collision": {
        "name": "碰撞约束",
        "max_collision_risk": 1e-4,
        "description": "collision_risk < 1e-4",
    },
    "C4_battery": {
        "name": "电池约束",
        "min_battery_soc": 20.0,
        "description": "battery_soc > 20%",
    },
    "C5_radiation": {
        "name": "辐射约束",
        "max_seu_rate": 10.0,  # SEU/天
        "description": "seu_rate < 10 SEU/day",
    },
}


class PhysicalCheckResult:
    """单个物理约束检查结果。"""
    def __init__(self, constraint_id: str, passed: bool,
                 actual_value: Optional[float] = None,
                 threshold: Optional[str] = None,
                 description: str = ""):
        self.constraint_id = constraint_id
        self.passed = passed
        self.actual_value = actual_value
        self.threshold = threshold
        self.description = description

    def to_dict(self) -> Dict[str, Any]:
        return {
            "constraint_id": self.constraint_id,
            "passed": self.passed,
            "actual_value": self.actual_value,
            "threshold": self.threshold,
            "description": self.description,
        }


class GatewayDecision:
    """
    Gateway 汇合判定结果。
    供 EGM 执行门控使用。
    """
    def __init__(self, run_id: str = "", task_id: str = "", sat_id: str = ""):
        self.monitor_type = "GATEWAY"
        self.run_id = run_id
        self.task_id = task_id
        self.sat_id = sat_id
        self.route = "PERMIT"  # PERMIT / SUSPEND / BLOCK
        self.containment = "NONE"  # NONE / TOOL_RESTRICT / PLANNING_BLOCK / ISOLATE
        self.reasons: List[str] = []
        self.confidence = 1.0
        self.physical_checks: Dict[str, Dict] = {}
        self.rsm_summary: Optional[Dict] = None
        self.dtm_summary: Optional[Dict] = None
        self.timestamp_ms = int(time.time() * 1000)

    def add_reason(self, reason: str):
        """添加判定理由。"""
        if reason not in self.reasons:
            self.reasons.append(reason)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "monitor_type": self.monitor_type,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "sat_id": self.sat_id,
            "route": self.route,
            "containment": self.containment,
            "reasons": self.reasons,
            "confidence": round(self.confidence, 4),
            "physical_checks": self.physical_checks,
            "rsm_summary": self.rsm_summary,
            "dtm_summary": self.dtm_summary,
            "timestamp_ms": self.timestamp_ms,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)


class DecisionGateway:
    """
    汇合判定器（Gateway）。

    输入：RSMResult + DTMResult + 物理状态快照（telemetry）
    输出：GatewayDecision（PERMIT/SUSPEND/BLOCK）

    判定逻辑：
      1. 执行 C1-C5 物理约束检查
      2. 综合 RSM 和 DTM 结果
      3. 按优先级判定路由：BLOCK > SUSPEND > PERMIT
    """

    def __init__(self, config: Optional[Dict] = None):
        self.config = config or {}
        gw_config = self.config.get("gateway", {})
        self.constraints = gw_config.get("constraints", DEFAULT_CONSTRAINTS)
        # DTM 阈值
        self.dtm_block_threshold = gw_config.get("dtm_block_threshold", 0.5)
        self.dtm_suspend_threshold = gw_config.get("dtm_suspend_threshold", 0.8)

    def check_physical_constraints(self, telemetry: Dict[str, Any]) -> List[PhysicalCheckResult]:
        """
        执行 C1-C5 物理约束检查。
        telemetry 可以是完整的 telemetry 事件或 telemetry 字段内容。
        """
        # 兼容两种输入：直接是遥测数据，或包含 telemetry 字段的事件
        if "telemetry" in telemetry and isinstance(telemetry["telemetry"], dict):
            data = telemetry["telemetry"]
        else:
            data = telemetry

        results = []

        # C1 功率约束
        c1 = self.constraints.get("C1_power", {})
        power_cons = data.get("power_consumption_w", 0)
        power_gen = data.get("power_generation_w", 0)
        safety_factor = c1.get("safety_factor", 0.95)
        if power_gen > 0:
            c1_passed = power_cons < power_gen * safety_factor
        else:
            c1_passed = True  # 无发电数据时跳过
        results.append(PhysicalCheckResult(
            "C1_power", c1_passed,
            actual_value=power_cons,
            threshold=f"< {power_gen * safety_factor:.1f}W",
            description=c1.get("description", ""),
        ))

        # C2 热约束
        c2 = self.constraints.get("C2_thermal", {})
        temp_obc = data.get("temp_obc", 25.0)
        temp_bat = data.get("temp_battery", 20.0)
        max_obc = c2.get("max_temp_obc", 60.0)
        max_bat = c2.get("max_temp_battery", 45.0)
        c2_passed = temp_obc < max_obc and temp_bat < max_bat
        results.append(PhysicalCheckResult(
            "C2_thermal", c2_passed,
            actual_value=f"obc={temp_obc}°C, bat={temp_bat}°C",
            threshold=f"obc<{max_obc}°C, bat<{max_bat}°C",
            description=c2.get("description", ""),
        ))

        # C3 碰撞约束
        c3 = self.constraints.get("C3_collision", {})
        collision_risk = data.get("collision_risk", 0.0)
        max_collision = c3.get("max_collision_risk", 1e-4)
        c3_passed = collision_risk < max_collision
        results.append(PhysicalCheckResult(
            "C3_collision", c3_passed,
            actual_value=collision_risk,
            threshold=f"< {max_collision}",
            description=c3.get("description", ""),
        ))

        # C4 电池约束
        c4 = self.constraints.get("C4_battery", {})
        battery_soc = data.get("battery_soc", 100.0)
        min_soc = c4.get("min_battery_soc", 20.0)
        c4_passed = battery_soc > min_soc
        results.append(PhysicalCheckResult(
            "C4_battery", c4_passed,
            actual_value=battery_soc,
            threshold=f"> {min_soc}%",
            description=c4.get("description", ""),
        ))

        # C5 辐射约束
        c5 = self.constraints.get("C5_radiation", {})
        seu_rate = data.get("seu_rate", 0.0)
        max_seu = c5.get("max_seu_rate", 10.0)
        c5_passed = seu_rate < max_seu
        results.append(PhysicalCheckResult(
            "C5_radiation", c5_passed,
            actual_value=seu_rate,
            threshold=f"< {max_seu} SEU/day",
            description=c5.get("description", ""),
        ))

        return results

    def decide(self, rsm_result: Optional[Dict], dtm_result: Optional[Dict],
               telemetry: Optional[Dict] = None,
               action_intent: Optional[Dict] = None) -> GatewayDecision:
        """
        执行汇合判定。

        Args:
            rsm_result: RSMResult 的 dict 形式（或 None）
            dtm_result: DTMResult 的 dict 形式（或 None）
            telemetry: 物理状态快照（遥测数据）
            action_intent: 动作意图元数据（来自 tool_call 事件，P2新增）
                支持字段：power_delta_w, thermal_delta_c, is_reversible,
                requires_approval, action_category, is_mutation

        Returns:
            GatewayDecision
        """
        # 提取上下文
        run_id = ""
        task_id = ""
        sat_id = ""
        if rsm_result:
            run_id = rsm_result.get("run_id", run_id)
            task_id = rsm_result.get("task_id", task_id)
            sat_id = rsm_result.get("sat_id", sat_id)
        if dtm_result:
            run_id = run_id or dtm_result.get("run_id", "")
            task_id = task_id or dtm_result.get("task_id", "")
            sat_id = sat_id or dtm_result.get("sat_id", "")

        decision = GatewayDecision(run_id=run_id, task_id=task_id, sat_id=sat_id)

        # 1. 物理约束检查
        physical_violations = []
        if telemetry:
            check_results = self.check_physical_constraints(telemetry)
            for cr in check_results:
                decision.physical_checks[cr.constraint_id] = cr.to_dict()
                if not cr.passed:
                    physical_violations.append(cr.constraint_id)
                    decision.add_reason(f"物理约束违规: {cr.constraint_id} ({cr.description})")
        else:
            # 无遥测数据时标记所有约束为"未检查"
            for cid in ["C1_power", "C2_thermal", "C3_collision", "C4_battery", "C5_radiation"]:
                decision.physical_checks[cid] = {
                    "constraint_id": cid, "passed": None,
                    "actual_value": None, "threshold": None,
                    "description": "无遥测数据，未检查",
                }

        # 1.5 动作意图增量约束检查（P2新增：消费 tool_call 中的动作元数据）
        action_requires_approval = False
        action_irreversible_mutation = False
        if action_intent and telemetry:
            tel_data = telemetry.get("telemetry", telemetry)
            # C1 增量：动作执行后功率是否超标
            power_delta = action_intent.get("power_delta_w", 0)
            if power_delta:
                current_power = tel_data.get("power_consumption_w", 0)
                power_gen = tel_data.get("power_generation_w", 0)
                projected_power = current_power + power_delta
                safety_factor = self.constraints.get("C1_power", {}).get("safety_factor", 0.95)
                if projected_power > power_gen * safety_factor:
                    physical_violations.append("C1_power_delta")
                    decision.add_reason(
                        f"动作增量约束违规: C1功率 (当前{current_power:.0f}W + 增量{power_delta:.0f}W "
                        f"= 投影{projected_power:.0f}W > 发电{power_gen:.0f}W × {safety_factor})"
                    )
            # C2 增量：动作执行后温度是否超标
            thermal_delta = action_intent.get("thermal_delta_c", 0)
            if thermal_delta:
                current_temp = tel_data.get("temp_obc", 0)
                projected_temp = current_temp + thermal_delta
                temp_limit = self.constraints.get("C2_thermal", {}).get("max_temp_obc", 60.0)
                if projected_temp > temp_limit:
                    physical_violations.append("C2_thermal_delta")
                    decision.add_reason(
                        f"动作增量约束违规: C2温度 (当前{current_temp:.0f}°C + 增量{thermal_delta:.0f}°C "
                        f"= 投影{projected_temp:.0f}°C > 限制{temp_limit}°C)"
                    )
            # 需要人工审批的动作
            if action_intent.get("requires_approval", False):
                action_requires_approval = True
                decision.add_reason(f"动作需要人工审批: {action_intent.get('action_category', 'unknown')}")
            # 不可恢复的变异操作
            if action_intent.get("is_mutation", False) and not action_intent.get("is_reversible", True):
                action_irreversible_mutation = True
                decision.add_reason("动作是不可恢复的变异操作，风险升档")

        # 2. RSM 结果分析
        rsm_fault = False
        rsm_critical = False
        rsm_fault_type = ""
        if rsm_result and rsm_result.get("fault_detected"):
            rsm_fault = True
            rsm_fault_type = rsm_result.get("fault_type", "")
            severity = rsm_result.get("severity", "low")
            if severity == "critical":
                rsm_critical = True
            decision.add_reason(f"RSM检测到故障: {rsm_fault_type} (severity={severity})")

        # 3. DTM 结果分析
        dtm_trust = 1.0
        dtm_risk = "low"
        if dtm_result:
            dtm_trust = dtm_result.get("trust_score", 1.0)
            dtm_risk = dtm_result.get("risk_label", "low")
            if dtm_result.get("issues"):
                decision.add_reason(f"DTM检测到 {len(dtm_result['issues'])} 个可信度问题 (trust_score={dtm_trust:.2f})")

        # 保存摘要
        decision.rsm_summary = {
            "fault_detected": rsm_fault,
            "fault_type": rsm_fault_type,
            "critical": rsm_critical,
        }
        decision.dtm_summary = {
            "trust_score": dtm_trust,
            "risk_label": dtm_risk,
        }

        # 4. 三级路由判定（按优先级：BLOCK > SUSPEND > PERMIT）
        if rsm_critical:
            decision.route = "BLOCK"
            decision.containment = "ISOLATE"
            decision.confidence = 0.95
        elif dtm_trust < self.dtm_block_threshold:
            decision.route = "BLOCK"
            decision.containment = "ISOLATE"
            decision.confidence = 0.90
            decision.add_reason(f"DTM trust_score={dtm_trust:.2f} < {self.dtm_block_threshold}，判定BLOCK")
        elif rsm_fault:
            decision.route = "SUSPEND"
            decision.containment = "TOOL_RESTRICT"
            decision.confidence = 0.85
        elif dtm_trust < self.dtm_suspend_threshold:
            decision.route = "SUSPEND"
            decision.containment = "PLANNING_BLOCK"
            decision.confidence = 0.80
            decision.add_reason(f"DTM trust_score={dtm_trust:.2f} < {self.dtm_suspend_threshold}，判定SUSPEND")
        elif physical_violations:
            decision.route = "SUSPEND"
            decision.containment = "TOOL_RESTRICT"
            decision.confidence = 0.82
        elif action_requires_approval or action_irreversible_mutation:
            # 动作需要人工审批或不可恢复变异：至少SUSPEND，不能直接放行
            decision.route = "SUSPEND"
            decision.containment = "PLANNING_BLOCK" if action_requires_approval else "TOOL_RESTRICT"
            decision.confidence = 0.80
            if action_requires_approval:
                decision.add_reason("动作需要人工审批，判定SUSPEND")
            if action_irreversible_mutation:
                decision.add_reason("不可恢复变异操作，判定SUSPEND")
        else:
            decision.route = "PERMIT"
            decision.containment = "NONE"
            decision.confidence = 0.98
            decision.add_reason("所有检查通过，判定PERMIT")

        return decision

    def get_constraints_summary(self) -> Dict[str, Any]:
        """获取物理约束配置摘要。"""
        return {cid: {"name": c.get("name"), "description": c.get("description")}
                for cid, c in self.constraints.items()}
