# -*- coding: utf-8 -*-
"""
EGM 执行门控模块自动化测试。

运行: python tests/test_egm.py
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from egm.egm import ExecutionGateModule, EGMOutput, Permit, AuditLogger, EGMState


def _make_gateway_decision(route="PERMIT", **overrides) -> dict:
    base = {
        "monitor_type": "GATEWAY",
        "run_id": "RUN-TEST", "task_id": "TASK-TEST", "sat_id": "SAT-TEST",
        "route": route, "containment": "NONE",
        "reasons": ["所有检查通过"], "confidence": 0.98,
        "physical_checks": {}, "rsm_summary": {}, "dtm_summary": {},
        "timestamp_ms": 1000,
    }
    base.update(overrides)
    return base


class TestPermit:
    """一次性许可测试。"""

    def test_permit_creation(self):
        """许可创建包含必要字段。"""
        p = Permit("P1", "RUN-1", "TASK-1", "digest1", "digest2")
        assert p.permit_id == "P1"
        assert p.used is False
        assert p.run_id == "RUN-1"

    def test_permit_consume(self):
        """消费许可后标记为已用。"""
        p = Permit("P1", "RUN-1", "TASK-1", "digest1", "digest2")
        assert p.consume() is True
        assert p.used is True
        assert p.used_ms is not None

    def test_permit_double_consume_rejected(self):
        """重复消费返回False（防重放）。"""
        p = Permit("P1", "RUN-1", "TASK-1", "digest1", "digest2")
        p.consume()
        assert p.consume() is False

    def test_permit_validity_check(self):
        """许可有效性检查：上下文匹配且未使用。"""
        p = Permit("P1", "RUN-1", "TASK-1", "digest1", "digest2")
        assert p.is_valid("RUN-1", "TASK-1", "digest1", "digest2") is True

    def test_permit_invalid_wrong_run(self):
        """run_id不匹配时许可无效。"""
        p = Permit("P1", "RUN-1", "TASK-1", "digest1", "digest2")
        assert p.is_valid("RUN-2", "TASK-1", "digest1", "digest2") is False

    def test_permit_invalid_after_use(self):
        """使用后许可无效。"""
        p = Permit("P1", "RUN-1", "TASK-1", "digest1", "digest2")
        p.consume()
        assert p.is_valid("RUN-1", "TASK-1", "digest1", "digest2") is False


class TestAuditLogger:
    """审计记录器测试。"""

    def test_audit_log_creation(self):
        """审计记录器能创建日志文件。"""
        logger = AuditLogger(log_dir="logs", log_file="test_egm_audit.jsonl")
        assert logger.get_count() == 0

    def test_audit_log_records(self):
        """记录审计事件。"""
        logger = AuditLogger(log_dir="logs", log_file="test_egm_audit2.jsonl")
        logger.log("TEST_EVENT", {"key": "value"}, "tester")
        assert logger.get_count() == 1
        records = logger.get_records()
        assert records[0]["event_type"] == "TEST_EVENT"
        assert records[0]["operator"] == "tester"


class TestEGMHumanReview:
    """人工审批流程测试。"""

    def test_permit_route_approved(self):
        """PERMIT路由 + 人工审批通过 → 签发许可。"""
        egm = ExecutionGateModule({"egm": {"log_file": "test_egm1.jsonl"}})
        output = egm.submit_gateway_decision(_make_gateway_decision("PERMIT"), human_approval="APPROVE")
        assert output.egm_state == EGMState.PERMIT_ISSUED.value
        assert output.permit_id is not None
        assert output.action is not None

    def test_permit_route_rejected(self):
        """人工审批拒绝 → REJECTED。"""
        egm = ExecutionGateModule({"egm": {"log_file": "test_egm2.jsonl"}})
        output = egm.submit_gateway_decision(_make_gateway_decision("PERMIT"), human_approval="REJECT")
        assert output.egm_state == EGMState.REJECTED.value
        assert output.execution_status == "REJECTED"

    def test_block_route_skips_human_review(self):
        """BLOCK路由直接进入安全保持，无需人工审批。"""
        egm = ExecutionGateModule({"egm": {"log_file": "test_egm3.jsonl"}})
        output = egm.submit_gateway_decision(_make_gateway_decision("BLOCK"))
        assert output.egm_state == EGMState.SAFE_HOLD.value
        assert output.execution_status == "BLOCKED"

    def test_suspend_route_approved(self):
        """SUSPEND路由 + 审批通过 → 签发许可。"""
        egm = ExecutionGateModule({"egm": {"log_file": "test_egm4.jsonl"}})
        output = egm.submit_gateway_decision(_make_gateway_decision("SUSPEND"), human_approval="APPROVE")
        assert output.egm_state == EGMState.PERMIT_ISSUED.value


class TestEGMExecution:
    """执行流程测试。"""

    def test_execute_with_valid_permit(self):
        """使用有效许可执行 → COMPLETED。"""
        egm = ExecutionGateModule({"egm": {"log_file": "test_egm5.jsonl"}})
        output = egm.submit_gateway_decision(_make_gateway_decision("PERMIT"), human_approval="APPROVE")
        permit_id = output.permit_id
        result = egm.execute_with_permit(permit_id, {"task": "test"})
        assert result.egm_state == EGMState.COMPLETED.value
        assert result.execution_status == "SUCCESS"
        assert result.after_state is not None

    def test_execute_with_nonexistent_permit(self):
        """使用不存在的许可 → 拒绝。"""
        egm = ExecutionGateModule({"egm": {"log_file": "test_egm6.jsonl"}})
        result = egm.execute_with_permit("NONEXISTENT", {})
        assert result.egm_state == EGMState.REJECTED.value

    def test_permit_replay_protection(self):
        """许可重放保护：同一许可不能执行两次。"""
        egm = ExecutionGateModule({"egm": {"log_file": "test_egm7.jsonl"}})
        output = egm.submit_gateway_decision(_make_gateway_decision("PERMIT"), human_approval="APPROVE")
        permit_id = output.permit_id
        # 第一次执行成功
        r1 = egm.execute_with_permit(permit_id, {})
        assert r1.egm_state == EGMState.COMPLETED.value
        # 第二次执行被拒绝（防重放）
        r2 = egm.execute_with_permit(permit_id, {})
        assert r2.egm_state == EGMState.REJECTED.value
        assert "重放" in (r2.rejection_reason or "")

    def test_state_change_rejects_execution(self):
        """执行前状态变更 → 拒绝执行。"""
        egm = ExecutionGateModule({"egm": {"log_file": "test_egm8.jsonl"}})
        output = egm.submit_gateway_decision(_make_gateway_decision("PERMIT"), human_approval="APPROVE")
        permit_id = output.permit_id
        # 传入state_changed=True
        result = egm.execute_with_permit(permit_id, {"state_changed": True})
        assert result.egm_state == EGMState.STATE_CHANGED.value
        assert result.execution_status == "REJECTED"


class TestEGMAudit:
    """审计记录测试。"""

    def test_audit_records_generated(self):
        """完整流程产生审计记录。"""
        egm = ExecutionGateModule({"egm": {"log_file": "test_egm9.jsonl"}})
        output = egm.submit_gateway_decision(_make_gateway_decision("PERMIT"), human_approval="APPROVE")
        permit_id = output.permit_id
        egm.execute_with_permit(permit_id, {})
        assert egm.get_audit_count() >= 3  # 审批请求 + 许可签发 + 执行完成

    def test_audit_events_in_output(self):
        """输出中包含审计事件。"""
        egm = ExecutionGateModule({"egm": {"log_file": "test_egm10.jsonl"}})
        output = egm.submit_gateway_decision(_make_gateway_decision("PERMIT"), human_approval="APPROVE")
        assert len(output.audit_events) >= 1


class TestEGMOutput:
    """EGMOutput结构测试。"""

    def test_output_has_required_fields(self):
        """输出包含必要字段。"""
        egm = ExecutionGateModule({"egm": {"log_file": "test_egm11.jsonl"}})
        output = egm.submit_gateway_decision(_make_gateway_decision("BLOCK"))
        d = output.to_dict()
        assert d["monitor_type"] == "EGM"
        assert "egm_state" in d
        assert "execution_status" in d
        assert "audit_events" in d
        assert "timestamp_ms" in d


def run_all_tests():
    print("=" * 60)
    print("EGM 执行门控模块 - 自动化测试")
    print("=" * 60)

    test_classes = [
        TestPermit, TestAuditLogger, TestEGMHumanReview,
        TestEGMExecution, TestEGMAudit, TestEGMOutput,
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
