# -*- coding: utf-8 -*-
"""
DTM（输出可信度监护器）自动化测试。

运行: python tests/test_dtm.py
"""
import sys
import os
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dtm.monitor import DecisionTrustMonitor, DTMResult
from dtm.checkers import (
    ConclusionConsistencyChecker, ConfidenceCalibrationChecker,
    EvidenceCompletenessChecker, MultiSourceConflictChecker,
)


def _make_event(event_type: str, **kwargs) -> dict:
    event = {
        "event_type": event_type,
        "sat_id": "SAT-TEST",
        "task_id": "TEST-001",
        "run_id": "RUN-TEST",
        "timestamp_ms": int(time.time() * 1000),
    }
    event.update(kwargs)
    return event


def _make_fault(fault_code="SA_BRANCH_SHORT", confidence=0.99, evidence_count=2) -> dict:
    evidence = [{"metric": f"metric_{i}", "value": 1.0, "status": "normal"} for i in range(evidence_count)]
    return {
        "fault_code": fault_code,
        "severity": "low",
        "confidence": confidence,
        "supporting_evidence": evidence,
        "conflicting_evidence": [],
    }


class TestConclusionConsistencyChecker:
    """结论一致性检查器测试。"""

    def test_consistent_diagnosis_no_issue(self):
        """结论与遥测一致时不触发。"""
        c = ConclusionConsistencyChecker({})
        c.process_event(_make_event("telemetry", telemetry={"sa_current": 6.0, "bcr_current": 4.0}))
        result = c.process_event(_make_event("diagnosis", faults=[_make_fault("SA_BRANCH_SHORT")]))
        assert result is None

    def test_inconsistent_diagnosis_detected(self):
        """结论与遥测不一致时触发。"""
        c = ConclusionConsistencyChecker({})
        # BCR_OPEN 应该 bcr_current≈0，但实际是4.0
        c.process_event(_make_event("telemetry", telemetry={"bcr_current": 4.0, "bus_voltage": 28.0}))
        result = c.process_event(_make_event("diagnosis", faults=[_make_fault("BCR_OPEN")]))
        assert result is not None
        assert result.issue_detected
        assert result.issue_type == "CONCLUSION_EVIDENCE_MISMATCH"

    def test_no_fault_skipped(self):
        """NO_FAULT 不检查一致性。"""
        c = ConclusionConsistencyChecker({})
        result = c.process_event(_make_event("diagnosis", faults=[_make_fault("NO_FAULT")]))
        assert result is None


class TestConfidenceCalibrationChecker:
    """置信度校准检查器测试。"""

    def test_high_conf_with_enough_evidence_no_issue(self):
        """高置信度+足够证据不触发。"""
        c = ConfidenceCalibrationChecker({"min_evidence_for_high_conf": 2})
        result = c.process_event(_make_event("diagnosis", faults=[_make_fault(confidence=0.99, evidence_count=3)]))
        assert result is None

    def test_high_conf_with_insufficient_evidence_detected(self):
        """高置信度+证据不足触发虚高。"""
        c = ConfidenceCalibrationChecker({"min_evidence_for_high_conf": 2, "high_conf_threshold": 0.90})
        result = c.process_event(_make_event("diagnosis", faults=[_make_fault(confidence=0.99, evidence_count=0)]))
        assert result is not None
        assert result.issue_detected
        assert result.issue_type == "INFLATED_CONFIDENCE"

    def test_low_confidence_not_flagged(self):
        """低置信度不触发虚高检查。"""
        c = ConfidenceCalibrationChecker({"high_conf_threshold": 0.90})
        result = c.process_event(_make_event("diagnosis", faults=[_make_fault(confidence=0.5, evidence_count=0)]))
        assert result is None


class TestEvidenceCompletenessChecker:
    """证据完整性检查器测试。"""

    def test_sufficient_evidence_no_issue(self):
        """足够证据不触发。"""
        c = EvidenceCompletenessChecker({"min_evidence_count": 1})
        result = c.process_event(_make_event("diagnosis", faults=[_make_fault(evidence_count=2)]))
        assert result is None

    def test_missing_evidence_detected(self):
        """证据缺失触发。"""
        c = EvidenceCompletenessChecker({"min_evidence_count": 1})
        result = c.process_event(_make_event("diagnosis", faults=[_make_fault(evidence_count=0)]))
        assert result is not None
        assert result.issue_detected
        assert result.issue_type == "MISSING_EVIDENCE"

    def test_no_fault_skipped(self):
        """NO_FAULT 不检查证据完整性。"""
        c = EvidenceCompletenessChecker({})
        result = c.process_event(_make_event("diagnosis", faults=[_make_fault("NO_FAULT", evidence_count=0)]))
        assert result is None


class TestMultiSourceConflictChecker:
    """多源矛盾检查器测试。"""

    def test_normal_telemetry_no_conflict(self):
        """正常遥测不触发矛盾。"""
        c = MultiSourceConflictChecker({})
        result = c.process_event(_make_event("telemetry", telemetry={
            "bus_voltage": 28.0, "bus_current": 4.5,
            "power_consumption_w": 126.0, "temp_battery": 20.0, "temp_obc": 25.0,
        }))
        assert result is None

    def test_voltage_current_conflict_detected(self):
        """电压正常但电流为0 → 矛盾。"""
        c = MultiSourceConflictChecker({})
        result = c.process_event(_make_event("telemetry", telemetry={
            "bus_voltage": 28.0, "bus_current": 0.0,
            "power_consumption_w": 100.0, "temp_battery": 20.0, "temp_obc": 25.0,
        }))
        assert result is not None
        assert result.issue_detected
        assert result.issue_type == "MULTI_SOURCE_CONFLICT"

    def test_temperature_conflict_detected(self):
        """温度差异异常 → 矛盾。"""
        c = MultiSourceConflictChecker({})
        result = c.process_event(_make_event("telemetry", telemetry={
            "bus_voltage": 28.0, "bus_current": 4.5,
            "power_consumption_w": 126.0, "temp_battery": -10.0, "temp_obc": 60.0,
        }))
        assert result is not None
        assert result.issue_detected


class TestDTMIntegration:
    """DTM 主类集成测试。"""

    def test_dtm_normal_events_high_trust(self):
        """正常事件流 trust_score 保持高位。"""
        dtm = DecisionTrustMonitor({})
        dtm.process_event(_make_event("telemetry", telemetry={"sa_current": 6.0, "bcr_current": 4.0}))
        result = dtm.process_event(_make_event("diagnosis", faults=[_make_fault("SA_BRANCH_SHORT", confidence=0.95, evidence_count=3)]))
        assert result.trust_score == 1.0
        assert result.risk_label == "low"

    def test_dtm_multiple_issues_accumulate_penalty(self):
        """多个问题累积扣减 trust_score。"""
        dtm = DecisionTrustMonitor({
            "dtm": {
                "confidence_calibration": {"min_evidence_for_high_conf": 2, "high_conf_threshold": 0.90},
                "evidence_completeness": {"min_evidence_count": 1},
            }
        })
        # 高置信度+0证据 → 同时触发虚高和证据缺失
        result = dtm.process_event(_make_event("diagnosis", faults=[_make_fault(confidence=0.99, evidence_count=0)]))
        assert len(result.issues) >= 1
        assert result.trust_score < 1.0
        assert result.risk_label in ("medium", "high", "critical")

    def test_dtm_result_has_all_checker_states(self):
        """DTMResult 包含所有检查器状态。"""
        dtm = DecisionTrustMonitor({})
        result = dtm.process_event(_make_event("telemetry", telemetry={}))
        assert "conclusion_consistency" in result.checker_states
        assert "confidence_calibration" in result.checker_states
        assert "evidence_completeness" in result.checker_states
        assert "multi_source_conflict" in result.checker_states

    def test_dtm_context_extraction(self):
        """DTM 从事件中提取上下文。"""
        dtm = DecisionTrustMonitor({})
        dtm.process_event(_make_event("diagnosis", run_id="RUN-456", task_id="TASK-789", sat_id="SAT-001", faults=[]))
        assert dtm._run_id == "RUN-456"
        assert dtm._task_id == "TASK-789"
        assert dtm._sat_id == "SAT-001"

    def test_dtm_summary(self):
        """DTM get_summary 返回运行摘要。"""
        dtm = DecisionTrustMonitor({})
        dtm.process_event(_make_event("telemetry", telemetry={}))
        summary = dtm.get_summary()
        assert summary["events_processed"] == 1
        assert "checkers" in summary


def run_all_tests():
    print("=" * 60)
    print("DTM 输出可信度监护器 - 自动化测试")
    print("=" * 60)

    test_classes = [
        TestConclusionConsistencyChecker, TestConfidenceCalibrationChecker,
        TestEvidenceCompletenessChecker, TestMultiSourceConflictChecker,
        TestDTMIntegration,
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
