# -*- coding: utf-8 -*-
"""
Gateway 汇合判定模块自动化测试。

运行: python tests/test_gateway.py
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gateway.gateway import DecisionGateway, GatewayDecision


def _make_rsm(fault_detected=False, fault_type="", severity="low", confidence=0.9) -> dict:
    return {
        "monitor_type": "RSM",
        "run_id": "RUN-TEST", "task_id": "TASK-TEST", "sat_id": "SAT-TEST",
        "fault_detected": fault_detected, "fault_type": fault_type,
        "severity": severity, "confidence": confidence,
        "issues": [], "detector_states": {}, "timestamp_ms": 1000,
    }


def _make_dtm(trust_score=1.0, risk_label="low", issues=None) -> dict:
    return {
        "monitor_type": "DTM",
        "run_id": "RUN-TEST", "task_id": "TASK-TEST", "sat_id": "SAT-TEST",
        "trust_score": trust_score, "risk_label": risk_label,
        "issues": issues or [], "checker_states": {}, "timestamp_ms": 1000,
    }


def _make_telemetry(**overrides) -> dict:
    base = {
        "bus_voltage": 28.0, "bus_current": 4.5,
        "power_consumption_w": 126.0, "power_generation_w": 150.0,
        "temp_obc": 25.0, "temp_battery": 20.0,
        "battery_soc": 85.0, "collision_risk": 1e-6, "seu_rate": 0.5,
    }
    base.update(overrides)
    return base


class TestPhysicalConstraints:
    """C1-C5 物理约束检查测试。"""

    def test_all_constraints_pass_normal(self):
        """正常遥测所有约束通过。"""
        gw = DecisionGateway({})
        results = gw.check_physical_constraints(_make_telemetry())
        assert all(r.passed for r in results)
        assert len(results) == 5

    def test_C1_power_violation(self):
        """C1 功率约束：功耗超过发电*0.9 → 违规。"""
        gw = DecisionGateway({})
        results = gw.check_physical_constraints(_make_telemetry(
            power_consumption_w=145.0, power_generation_w=150.0))
        c1 = next(r for r in results if r.constraint_id == "C1_power")
        assert not c1.passed

    def test_C2_thermal_violation(self):
        """C2 热约束：OBC温度超过60°C → 违规。"""
        gw = DecisionGateway({})
        results = gw.check_physical_constraints(_make_telemetry(temp_obc=70.0))
        c2 = next(r for r in results if r.constraint_id == "C2_thermal")
        assert not c2.passed

    def test_C3_collision_violation(self):
        """C3 碰撞约束：碰撞风险超过1e-4 → 违规。"""
        gw = DecisionGateway({})
        results = gw.check_physical_constraints(_make_telemetry(collision_risk=1e-3))
        c3 = next(r for r in results if r.constraint_id == "C3_collision")
        assert not c3.passed

    def test_C4_battery_violation(self):
        """C4 电池约束：SOC低于20% → 违规。"""
        gw = DecisionGateway({})
        results = gw.check_physical_constraints(_make_telemetry(battery_soc=15.0))
        c4 = next(r for r in results if r.constraint_id == "C4_battery")
        assert not c4.passed

    def test_C5_radiation_violation(self):
        """C5 辐射约束：SEU率超过10 → 违规。"""
        gw = DecisionGateway({})
        results = gw.check_physical_constraints(_make_telemetry(seu_rate=15.0))
        c5 = next(r for r in results if r.constraint_id == "C5_radiation")
        assert not c5.passed

    def test_telemetry_event_wrapper(self):
        """支持传入包含 telemetry 字段的完整事件。"""
        gw = DecisionGateway({})
        event = {"event_type": "telemetry", "telemetry": _make_telemetry()}
        results = gw.check_physical_constraints(event)
        assert all(r.passed for r in results)


class TestGatewayRouting:
    """三级路由判定测试。"""

    def test_permit_all_clear(self):
        """所有检查通过 → PERMIT。"""
        gw = DecisionGateway({})
        decision = gw.decide(_make_rsm(), _make_dtm(), _make_telemetry())
        assert decision.route == "PERMIT"
        assert decision.containment == "NONE"

    def test_block_rsm_critical(self):
        """RSM critical故障 → BLOCK。"""
        gw = DecisionGateway({})
        decision = gw.decide(
            _make_rsm(fault_detected=True, fault_type="PROCESS_CRASH", severity="critical"),
            _make_dtm(), _make_telemetry())
        assert decision.route == "BLOCK"
        assert decision.containment == "ISOLATE"

    def test_block_dtm_low_trust(self):
        """DTM trust_score < 0.5 → BLOCK。"""
        gw = DecisionGateway({})
        decision = gw.decide(_make_rsm(), _make_dtm(trust_score=0.3), _make_telemetry())
        assert decision.route == "BLOCK"

    def test_suspend_rsm_fault(self):
        """RSM非critical故障 → SUSPEND。"""
        gw = DecisionGateway({})
        decision = gw.decide(
            _make_rsm(fault_detected=True, fault_type="MEMORY_BLOAT", severity="high"),
            _make_dtm(), _make_telemetry())
        assert decision.route == "SUSPEND"
        assert decision.containment == "TOOL_RESTRICT"

    def test_suspend_dtm_medium_trust(self):
        """DTM trust_score 0.5-0.8 → SUSPEND。"""
        gw = DecisionGateway({})
        decision = gw.decide(_make_rsm(), _make_dtm(trust_score=0.65), _make_telemetry())
        assert decision.route == "SUSPEND"

    def test_suspend_physical_violation(self):
        """物理约束违规 → SUSPEND。"""
        gw = DecisionGateway({})
        decision = gw.decide(_make_rsm(), _make_dtm(),
                              _make_telemetry(temp_obc=70.0))
        assert decision.route == "SUSPEND"

    def test_block_priority_over_suspend(self):
        """BLOCK优先级高于SUSPEND：同时有critical和物理违规 → BLOCK。"""
        gw = DecisionGateway({})
        decision = gw.decide(
            _make_rsm(fault_detected=True, fault_type="PROCESS_CRASH", severity="critical"),
            _make_dtm(trust_score=0.6),
            _make_telemetry(temp_obc=70.0))
        assert decision.route == "BLOCK"

    def test_no_telemetry_marks_unchecked(self):
        """无遥测数据时物理约束标记为未检查。"""
        gw = DecisionGateway({})
        decision = gw.decide(_make_rsm(), _make_dtm(), None)
        assert decision.physical_checks["C1_power"]["passed"] is None


class TestGatewayDecision:
    """GatewayDecision 结构测试。"""

    def test_decision_has_required_fields(self):
        """决策结果包含必要字段。"""
        gw = DecisionGateway({})
        decision = gw.decide(_make_rsm(), _make_dtm(), _make_telemetry())
        d = decision.to_dict()
        assert d["monitor_type"] == "GATEWAY"
        assert "route" in d
        assert "containment" in d
        assert "reasons" in d
        assert "confidence" in d
        assert "physical_checks" in d
        assert "rsm_summary" in d
        assert "dtm_summary" in d

    def test_reasons_are_unique(self):
        """理由不重复。"""
        gw = DecisionGateway({})
        decision = gw.decide(_make_rsm(), _make_dtm(), _make_telemetry())
        assert len(decision.reasons) == len(set(decision.reasons))

    def test_context_propagation(self):
        """run_id/task_id/sat_id 从输入传播到输出。"""
        gw = DecisionGateway({})
        rsm = _make_rsm()
        rsm["run_id"] = "RUN-123"
        rsm["task_id"] = "TASK-456"
        rsm["sat_id"] = "SAT-789"
        decision = gw.decide(rsm, _make_dtm(), _make_telemetry())
        assert decision.run_id == "RUN-123"
        assert decision.task_id == "TASK-456"
        assert decision.sat_id == "SAT-789"


def run_all_tests():
    print("=" * 60)
    print("Gateway 汇合判定模块 - 自动化测试")
    print("=" * 60)

    test_classes = [TestPhysicalConstraints, TestGatewayRouting, TestGatewayDecision]
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
