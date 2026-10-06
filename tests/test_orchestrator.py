# -*- coding: utf-8 -*-
"""
四模块全链路编排器集成测试。

运行: python tests/test_orchestrator.py
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 控制台编码兼容：非 UTF-8 代码页（如 CI 的英文 Windows）下中文输出不再抛 UnicodeEncodeError
from agent.console import enable_utf8_stdout
enable_utf8_stdout()

from orchestrator import FourModuleOrchestrator, EventCapture


class TestEventCapture:
    """事件捕获器测试。"""

    def test_capture_publishes_events(self):
        capture = EventCapture()
        capture.publish({"event_type": "heartbeat"})
        capture.publish({"event_type": "telemetry"})
        assert len(capture.events) == 2
        assert capture.events[0]["event_type"] == "heartbeat"


class TestOrchestratorNormal:
    """正常模式编排测试。"""

    def test_normal_mode_runs(self):
        """正常模式能完整执行四模块链路。"""
        orch = FourModuleOrchestrator()
        report = orch.run()
        assert report["orchestrator"] == "FourModuleOrchestrator"
        assert report["injected_faults"] == []
        assert report["event_count"] > 0
        assert report["agent_result"]["status"] == "completed"

    def test_normal_mode_has_all_module_results(self):
        """正常模式包含RSM/DTM/Gateway结果。"""
        orch = FourModuleOrchestrator()
        report = orch.run()
        assert report["rsm_result"] is not None
        assert report["dtm_result"] is not None
        assert report["gateway_decision"] is not None

    def test_normal_mode_gateway_route_valid(self):
        """正常模式Gateway路由是有效值。"""
        orch = FourModuleOrchestrator()
        report = orch.run()
        route = report["gateway_decision"]["route"]
        assert route in ("PERMIT", "SUSPEND", "BLOCK")

    def test_normal_mode_event_summary(self):
        """正常模式事件分布统计正确。"""
        orch = FourModuleOrchestrator()
        report = orch.run()
        summary = report["event_summary"]
        assert "heartbeat" in summary
        assert "telemetry" in summary
        assert "agent_output" in summary
        assert sum(summary.values()) == report["event_count"]


class TestOrchestratorFault:
    """故障注入模式编排测试。"""

    def test_wrong_diagnosis_injected(self):
        """注入wrong_diagnosis故障。"""
        orch = FourModuleOrchestrator()
        report = orch.run(fault_names=["wrong_diagnosis"])
        assert "wrong_diagnosis" in report["injected_faults"]
        assert report["agent_result"]["status"] == "completed"
        # 结论应该不是"系统正常"
        conclusion = report["agent_result"].get("conclusion", "")
        assert "正常" not in conclusion or "故障" in conclusion

    def test_inflated_confidence_injected(self):
        """注入inflated_confidence故障。"""
        orch = FourModuleOrchestrator()
        report = orch.run(fault_names=["inflated_confidence"])
        assert "inflated_confidence" in report["injected_faults"]
        assert report["agent_result"]["status"] == "completed"

    def test_process_crash_injected(self):
        """注入process_crash故障（会抛异常但被捕获）。"""
        orch = FourModuleOrchestrator()
        report = orch.run(fault_names=["process_crash"])
        assert "process_crash" in report["injected_faults"]
        # 进程崩溃故障会导致agent_result状态为failed
        assert report["agent_result"]["status"] in ("failed", "completed")

    def test_multi_fault_injection(self):
        """多故障同时注入。"""
        orch = FourModuleOrchestrator()
        report = orch.run(fault_names=["wrong_diagnosis", "inflated_confidence"])
        assert len(report["injected_faults"]) == 2
        assert report["event_count"] > 0


class TestOrchestratorGateway:
    """Gateway决策验证测试。"""

    def test_gateway_decision_has_physical_checks(self):
        """Gateway决策包含C1-C5物理约束检查。"""
        orch = FourModuleOrchestrator()
        report = orch.run()
        checks = report["gateway_decision"]["physical_checks"]
        assert "C1_power" in checks
        assert "C2_thermal" in checks
        assert "C3_collision" in checks
        assert "C4_battery" in checks
        assert "C5_radiation" in checks

    def test_gateway_decision_has_reasons(self):
        """Gateway决策包含理由列表。"""
        orch = FourModuleOrchestrator()
        report = orch.run()
        reasons = report["gateway_decision"]["reasons"]
        assert isinstance(reasons, list)
        assert len(reasons) >= 1

    def test_gateway_confidence_in_range(self):
        """Gateway置信度在0-1范围内。"""
        orch = FourModuleOrchestrator()
        report = orch.run()
        conf = report["gateway_decision"]["confidence"]
        assert 0 <= conf <= 1


def run_all_tests():
    print("=" * 60)
    print("四模块全链路编排器 - 集成测试")
    print("=" * 60)

    test_classes = [
        TestEventCapture, TestOrchestratorNormal,
        TestOrchestratorFault, TestOrchestratorGateway,
    ]
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
