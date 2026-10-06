# -*- coding: utf-8 -*-
"""
五模块全链路编排器集成测试。

运行: python tests/test_five_module.py
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from five_module_orchestrator import FiveModuleOrchestrator


class TestFiveModuleNormal:
    """正常模式五模块测试。"""

    def test_normal_mode_completes(self):
        """正常模式五模块链路完整执行。"""
        orch = FiveModuleOrchestrator()
        report = orch.run()
        assert report["orchestrator"] == "FiveModuleOrchestrator"
        assert report["event_count"] > 0
        assert report["agent_result"]["status"] == "completed"

    def test_normal_mode_has_all_five_modules(self):
        """正常模式包含全部五个模块的结果。"""
        orch = FiveModuleOrchestrator()
        report = orch.run()
        assert report["rsm_result"] is not None
        assert report["dtm_result"] is not None
        assert report["gateway_decision"] is not None
        assert report["egm_review"] is not None
        # egm_execution 可能为 None（如果BLOCK或REJECT）
        assert "egm_execution" in report

    def test_normal_mode_has_final_outcome(self):
        """正常模式有最终执行结果。"""
        orch = FiveModuleOrchestrator()
        report = orch.run()
        assert "final_outcome" in report
        assert len(report["final_outcome"]) > 0


class TestFiveModuleFault:
    """故障注入五模块测试。"""

    def test_wrong_diagnosis_fault(self):
        """注入wrong_diagnosis故障。"""
        orch = FiveModuleOrchestrator()
        report = orch.run(fault_names=["wrong_diagnosis"])
        assert "wrong_diagnosis" in report["injected_faults"]
        assert report["event_count"] > 0

    def test_process_crash_fault(self):
        """注入process_crash故障。"""
        orch = FiveModuleOrchestrator()
        report = orch.run(fault_names=["process_crash"])
        assert "process_crash" in report["injected_faults"]

    def test_multi_fault_injection(self):
        """多故障同时注入。"""
        orch = FiveModuleOrchestrator()
        report = orch.run(fault_names=["wrong_diagnosis", "inflated_confidence"])
        assert len(report["injected_faults"]) == 2

    def test_human_reject_blocks_execution(self):
        """人工审批拒绝 → 不执行。"""
        orch = FiveModuleOrchestrator()
        report = orch.run(fault_names=None, human_approval="REJECT")
        assert report["egm_review"]["egm_state"] == "REJECTED"
        assert report["egm_execution"] is None


class TestFiveModuleGateway:
    """Gateway决策在五模块中的验证。"""

    def test_gateway_route_valid(self):
        """Gateway路由是有效值。"""
        orch = FiveModuleOrchestrator()
        report = orch.run()
        route = report["gateway_decision"]["route"]
        assert route in ("PERMIT", "SUSPEND", "BLOCK")

    def test_gateway_physical_checks_present(self):
        """Gateway包含C1-C5物理约束检查。"""
        orch = FiveModuleOrchestrator()
        report = orch.run()
        checks = report["gateway_decision"]["physical_checks"]
        assert len(checks) == 5


class TestFiveModuleEGM:
    """EGM在五模块中的验证。"""

    def test_egm_audit_events_present(self):
        """EGM输出包含审计事件。"""
        orch = FiveModuleOrchestrator()
        report = orch.run()
        assert "audit_events" in report["egm_review"]

    def test_egm_state_valid(self):
        """EGM状态是有效值。"""
        orch = FiveModuleOrchestrator()
        report = orch.run()
        state = report["egm_review"]["egm_state"]
        valid_states = ["PENDING_HUMAN", "PERMIT_ISSUED", "STATE_CHECK",
                        "EXECUTING", "COMPLETED", "REJECTED",
                        "STATE_CHANGED", "SAFE_HOLD"]
        assert state in valid_states


def run_all_tests():
    print("=" * 60)
    print("五模块全链路编排器 - 集成测试")
    print("=" * 60)

    test_classes = [
        TestFiveModuleNormal, TestFiveModuleFault,
        TestFiveModuleGateway, TestFiveModuleEGM,
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
