# -*- coding: utf-8 -*-
"""
执行前置门控（ExecutionGate）测试。

覆盖：
1. 门控单元语义——PERMIT 签发许可 / BLOCK 拒绝 / SUSPEND 拒绝 / requires_approval 策略
2. 一次性许可——消费、防重放、动作绑定校验
3. 端到端——mutation 工具在执行前被门控拦截（wrong_diagnosis 场景），
   正常场景零干扰（只读工具不需要许可）

运行: python tests/test_gate.py
"""
import sys
import os
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rsm.monitor import RuntimeStabilityMonitor
from dtm.monitor import DecisionTrustMonitor
from gateway.gateway import DecisionGateway
from egm.egm import ExecutionGateModule
from egm.gate import ExecutionGate


def _make_event(event_type: str, **kwargs) -> dict:
    event = {
        "event_type": event_type,
        "sat_id": "SAT-GATE",
        "task_id": "GATE-001",
        "run_id": "RUN-GATE",
        "timestamp_ms": int(time.time() * 1000),
    }
    event.update(kwargs)
    return event


def _make_tool_call(tool_name: str = "tool_switch_sa_branch", mutation: bool = True,
                    requires_approval: bool = False, power_delta_w: float = 0.0,
                    call_id: str = "CALL-GATE-1") -> dict:
    return {
        "event_type": "tool_call",
        "sat_id": "SAT-GATE",
        "task_id": "GATE-001",
        "call_id": call_id,
        "tool_name": tool_name,
        "args": {"branch": "A", "action": "off"},
        "is_mutation": mutation,
        "requires_approval": requires_approval,
        "action_category": "PAYLOAD_OPERATION",
        "is_reversible": True,
        "power_delta_w": power_delta_w,
        "thermal_delta_c": 0.0,
        "timestamp_ms": int(time.time() * 1000),
    }


def _normal_telemetry() -> dict:
    # 留有充足功率裕度：120/150 = 0.8 < 0.95，增量动作不会被物理约束误拦
    return {
        "event_type": "telemetry",
        "sat_id": "SAT-GATE",
        "telemetry": {
            "power_consumption_w": 120.0,
            "power_generation_w": 150.0,
            "temp_obc": 25.0,
            "temp_battery": 18.0,
            "collision_risk": 1e-6,
            "battery_soc": 85.0,
            "seu_rate": 0.0,
        },
        "timestamp_ms": int(time.time() * 1000),
    }


def _fresh_stack(human_approval: str = "REJECT"):
    rsm = RuntimeStabilityMonitor({})
    dtm = DecisionTrustMonitor({})
    gw = DecisionGateway({})
    egm = ExecutionGateModule({"egm": {"log_dir": "logs", "log_file": "test_gate_audit.jsonl"}})
    gate = ExecutionGate(rsm, dtm, gw, egm, human_approval=human_approval)
    return rsm, dtm, gw, egm, gate


class TestGateUnit:
    """门控单元语义测试（纯合成状态，无线程）。"""

    def test_permit_route_issues_permit(self):
        """无故障状态下 mutation 工具获得一次性许可。"""
        rsm, dtm, gw, egm, gate = _fresh_stack()
        gate.observe(_normal_telemetry())
        verdict = gate.authorize(_make_tool_call())
        assert verdict.approved
        assert verdict.route == "PERMIT"
        assert verdict.permit_id
        assert verdict.binding_digest

    def test_block_route_denies(self):
        """RSM 检出 critical 故障 → BLOCK → 拒绝放行。"""
        rsm, dtm, gw, egm, gate = _fresh_stack()
        t0 = int(time.time() * 1000)
        # 事件驱动的 PROCESS_CRASH：心跳之后 20s 无心跳（默认阈值 15s）
        rsm.process_event(_make_event("heartbeat", timestamp_ms=t0, uptime_s=1.0))
        rsm.process_event(_make_event("telemetry", timestamp_ms=t0 + 20000))
        verdict = gate.authorize(_make_tool_call())
        assert not verdict.approved
        assert verdict.route == "BLOCK"
        assert "BLOCK" in verdict.reason
        summary = gate.get_summary()
        assert summary["denied"] == 1

    def test_suspend_route_denies(self):
        """DTM 信任分低于挂起阈值 → SUSPEND → 自动模式拒绝放行。"""
        rsm, dtm, gw, egm, gate = _fresh_stack()
        # 注入一条证据缺失的诊断（penalty 0.25 → trust 0.75 < 0.8）
        dtm.process_event(_make_event("diagnosis", faults=[{
            "fault_code": "X_F", "confidence": 0.5, "supporting_evidence": [],
        }]))
        verdict = gate.authorize(_make_tool_call())
        assert not verdict.approved
        assert verdict.route == "SUSPEND"
        assert "SUSPEND" in verdict.reason

    def test_requires_approval_rejected_by_default_policy(self):
        """requires_approval 工具在默认策略（REJECT）下被拒绝。"""
        rsm, dtm, gw, egm, gate = _fresh_stack()  # human_approval="REJECT"
        gate.observe(_normal_telemetry())
        verdict = gate.authorize(_make_tool_call(requires_approval=True))
        assert not verdict.approved
        assert "requires_approval" in verdict.reason

    def test_requires_approval_approved_with_approve_policy(self):
        """requires_approval 工具在 APPROVE 策略下获得许可。"""
        rsm, dtm, gw, egm, gate = _fresh_stack(human_approval="APPROVE")
        gate.observe(_normal_telemetry())
        verdict = gate.authorize(_make_tool_call(requires_approval=True))
        assert verdict.approved
        assert verdict.permit_id

    def test_permit_one_time_and_binding(self):
        """一次性许可：消费一次、重放拒绝、动作绑定不匹配拒绝。"""
        rsm, dtm, gw, egm, gate = _fresh_stack()
        gate.observe(_normal_telemetry())
        call_event = _make_tool_call()
        verdict = gate.authorize(call_event)
        assert verdict.approved
        binding = gate.binding_digest_for(call_event)

        # 绑定不匹配 → 拒绝消费
        assert not gate.consume(verdict.permit_id, "WRONG-BINDING")
        # 正确绑定 → 消费成功
        assert gate.consume(verdict.permit_id, binding)
        # 重放 → 拒绝
        assert not gate.consume(verdict.permit_id, binding)

    def test_unknown_permit_consume_rejected(self):
        """不存在的许可 → 拒绝消费。"""
        rsm, dtm, gw, egm, gate = _fresh_stack()
        assert not gate.consume("PERMIT-NOT-EXIST", "x")


class TestGateIntegration:
    """端到端集成测试（五模块编排器）。"""

    def test_normal_mode_no_gating_interference(self):
        """正常场景：只读工具不需要许可，门控零干扰。"""
        from five_module_orchestrator import FiveModuleOrchestrator
        orch = FiveModuleOrchestrator()
        report = orch.run()
        # 只读工具不经过门控
        assert report["gate_summary"]["total_checked"] == 0
        # 正常链路可达 PERMIT/SUSPEND（C1 裕度受遥测噪声影响，不断言固定路由）
        assert report["gateway_decision"]["route"] in ("PERMIT", "SUSPEND")

    def test_wrong_diagnosis_mutation_tool_gated(self):
        """wrong_diagnosis → DTM trust 下降 → SUSPEND → mutation 工具在执行前被门控拦截。"""
        from five_module_orchestrator import FiveModuleOrchestrator
        orch = FiveModuleOrchestrator()
        report = orch.run(fault_names=["wrong_diagnosis"])
        # 门控拦截记录
        assert report["gate_summary"]["denied"] >= 1
        # 最终结论体现"工具被拦截"而非"执行成功"
        assert report["final_outcome"].startswith("TOOL_GATED")
        # 拦截来自 SUSPEND/BLOCK 路由
        assert report["gate_summary"]["denials"][0]["route"] in ("SUSPEND", "BLOCK")


def run_all_tests():
    print("=" * 60)
    print("执行前置门控（ExecutionGate）- 测试")
    print("=" * 60)

    test_classes = [TestGateUnit, TestGateIntegration]
    passed = 0
    failed = 0
    errors = []

    for test_class in test_classes:
        instance = test_class()
        methods = [m for m in dir(instance) if m.startswith("test_")]
        for method_name in methods:
            try:
                getattr(instance, method_name)()
                print(f"  PASS  {test_class.__name__}.{method_name}")
                passed += 1
            except Exception as e:
                print(f"  FAIL  {test_class.__name__}.{method_name}: {e}")
                failed += 1
                errors.append((method_name, str(e)))

    print(f"\n{'=' * 60}")
    print(f"测试结果: {passed} passed, {failed} failed")
    if errors:
        print("\n失败详情:")
        for name, err in errors:
            print(f"  - {name}: {err}")
    print("=" * 60)
    return failed == 0


if __name__ == "__main__":
    success = run_all_tests()
    sys.exit(0 if success else 1)
