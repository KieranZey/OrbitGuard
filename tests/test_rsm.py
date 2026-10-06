# -*- coding: utf-8 -*-
"""
RSM（运行稳定性监护器）自动化测试。

运行: python tests/test_rsm.py
"""
import sys
import os
import time
import json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 控制台编码兼容：非 UTF-8 代码页（如 CI 的英文 Windows）下中文输出不再抛 UnicodeEncodeError
from agent.console import enable_utf8_stdout
enable_utf8_stdout()

from rsm.monitor import RuntimeStabilityMonitor, RSMResult
from rsm.detectors import (
    HeartbeatMonitor, StallMonitor, ToolTimeoutMonitor,
    MemoryMonitor, RepetitiveCallMonitor,
)


def _make_event(event_type: str, **kwargs) -> dict:
    """生成测试事件。"""
    event = {
        "event_type": event_type,
        "sat_id": "SAT-TEST",
        "task_id": "TEST-001",
        "run_id": "RUN-TEST",
        "timestamp_ms": int(time.time() * 1000),
    }
    event.update(kwargs)
    return event


class TestHeartbeatMonitor:
    """心跳检测器测试。"""

    def test_normal_heartbeat_no_detection(self):
        """正常心跳不触发检测。"""
        m = HeartbeatMonitor({"heartbeat_timeout_s": 10.0})
        result = m.process_event(_make_event("heartbeat", uptime_s=1.0))
        assert result is None

    def test_heartbeat_timeout_detects_crash(self):
        """心跳超时检测到进程崩溃。"""
        m = HeartbeatMonitor({"heartbeat_timeout_s": 0.1})
        m.process_event(_make_event("heartbeat", timestamp_ms=1000))
        # 模拟200ms后的事件
        result = m.process_event(_make_event("telemetry", timestamp_ms=1300))
        assert result is not None
        assert result.fault_detected
        assert result.fault_type == "PROCESS_CRASH"
        assert result.severity == "critical"

    def test_heartbeat_recovery_resets(self):
        """心跳恢复后重置崩溃状态。"""
        m = HeartbeatMonitor({"heartbeat_timeout_s": 0.1})
        m.process_event(_make_event("heartbeat", timestamp_ms=1000))
        m.process_event(_make_event("telemetry", timestamp_ms=1300))  # 触发崩溃
        # 心跳恢复
        result = m.process_event(_make_event("heartbeat", timestamp_ms=1400))
        assert result is None
        assert m._crash_detected is False


class TestStallMonitor:
    """无进展检测器测试。"""

    def test_progress_no_detection(self):
        """有进展时不触发检测。"""
        m = StallMonitor({"stall_timeout_s": 10.0})
        m.process_event(_make_event("task_progress", step=1, timestamp_ms=1000))
        m.process_event(_make_event("heartbeat", uptime_s=1.0, timestamp_ms=1100))
        m.process_event(_make_event("task_progress", step=2, timestamp_ms=1200))
        assert m._stall_detected is False

    def test_stall_detects_infinite_loop(self):
        """步骤停滞但心跳正常 → 死循环。"""
        m = StallMonitor({"stall_timeout_s": 0.1})
        m.process_event(_make_event("task_progress", step=1, timestamp_ms=1000))
        # 200ms后心跳仍在，但step没变
        result = m.process_event(_make_event("heartbeat", uptime_s=5.0, timestamp_ms=1300))
        assert result is not None
        assert result.fault_detected
        assert result.fault_type == "INFINITE_LOOP"

    def test_new_progress_resets_stall(self):
        """新进展重置停滞状态。"""
        m = StallMonitor({"stall_timeout_s": 0.1})
        m.process_event(_make_event("task_progress", step=1, timestamp_ms=1000))
        m.process_event(_make_event("heartbeat", uptime_s=5.0, timestamp_ms=1300))  # 触发
        m.process_event(_make_event("task_progress", step=2, timestamp_ms=1400))  # 新进展
        assert m._stall_detected is False


class TestToolTimeoutMonitor:
    """工具超时检测器测试。"""

    def test_normal_call_no_timeout(self):
        """正常调用（有result）不触发超时。"""
        m = ToolTimeoutMonitor({"tool_timeout_s": 10.0})
        m.process_event(_make_event("tool_call", call_id="C1", tool_name="tool_get_telemetry", timestamp_ms=1000))
        m.process_event(_make_event("tool_result", call_id="C1", timestamp_ms=1050))
        assert len(m.pending_calls) == 0

    def test_timeout_detection(self):
        """tool_call后超时无result → 无输出超时检测。"""
        m = ToolTimeoutMonitor({"tool_timeout_s": 0.1})
        m.process_event(_make_event("tool_call", call_id="C1", tool_name="tool_get_telemetry", timestamp_ms=1000))
        result = m.process_event(_make_event("heartbeat", timestamp_ms=1300))
        assert result is not None
        assert result.fault_detected
        assert result.fault_type == "NO_OUTPUT_TIMEOUT"
        assert "C1" in result.evidence["call_id"]

    def test_executor_reported_timeout(self):
        """执行方回报超时（tool_result.ok=False 含timeout）→ 独立判为无输出超时。"""
        m = ToolTimeoutMonitor({"tool_timeout_s": 10.0})
        m.process_event(_make_event("tool_call", call_id="C1", tool_name="tool_get_telemetry", timestamp_ms=1000))
        result = m.process_event(_make_event(
            "tool_result", call_id="C1", tool_name="tool_get_telemetry",
            ok=False, error_message="Tool timed out after 120s (fault injection)", timestamp_ms=1050))
        assert result is not None
        assert result.fault_detected
        assert result.fault_type == "NO_OUTPUT_TIMEOUT"
        assert len(m.pending_calls) == 0

    def test_executor_normal_failure_not_timeout(self):
        """执行方普通失败（非超时）不判为无输出超时。"""
        m = ToolTimeoutMonitor({"tool_timeout_s": 10.0})
        m.process_event(_make_event("tool_call", call_id="C1", tool_name="tool_get_telemetry", timestamp_ms=1000))
        result = m.process_event(_make_event(
            "tool_result", call_id="C1", ok=False,
            error_message="tool execution exception", timestamp_ms=1050))
        assert result is None
        assert len(m.pending_calls) == 0

    def test_multiple_pending_calls(self):
        """多个进行中的调用分别管理。"""
        m = ToolTimeoutMonitor({"tool_timeout_s": 10.0})
        m.process_event(_make_event("tool_call", call_id="C1", timestamp_ms=1000))
        m.process_event(_make_event("tool_call", call_id="C2", timestamp_ms=1010))
        assert len(m.pending_calls) == 2
        m.process_event(_make_event("tool_result", call_id="C1", timestamp_ms=1020))
        assert len(m.pending_calls) == 1
        assert "C2" in m.pending_calls


class TestMemoryMonitor:
    """内存检测器测试。"""

    def test_normal_memory_no_detection(self):
        """正常内存不触发检测。"""
        m = MemoryMonitor({"memory_threshold_mb": 500.0})
        result = m.process_event(_make_event("resource", memory_mb=100.0))
        assert result is None

    def test_absolute_threshold_detection(self):
        """内存超过绝对阈值 → 检测。"""
        m = MemoryMonitor({"memory_threshold_mb": 200.0})
        result = m.process_event(_make_event("resource", memory_mb=300.0))
        assert result is not None
        assert result.fault_detected
        assert result.fault_type == "MEMORY_BLOAT"

    def test_monotonic_growth_detection(self):
        """内存连续单调增长 → 疑似泄漏。"""
        m = MemoryMonitor({"memory_threshold_mb": 1000.0, "growth_window": 3})
        result = None
        for i, mem in enumerate([50, 120, 200, 280, 360]):
            result = m.process_event(_make_event("resource", memory_mb=mem, timestamp_ms=1000 + i * 100))
            if result and result.fault_detected:
                break
        assert result is not None
        assert result.fault_type == "MEMORY_BLOAT"


class TestRepetitiveCallMonitor:
    """重复调用检测器测试。"""

    def test_different_tools_no_detection(self):
        """不同工具交替调用不触发检测。"""
        m = RepetitiveCallMonitor({"repeat_threshold": 4})
        m.process_event(_make_event("tool_call", tool_name="tool_A"))
        m.process_event(_make_event("tool_call", tool_name="tool_B"))
        m.process_event(_make_event("tool_call", tool_name="tool_A"))
        assert m._detected is False

    def test_repetitive_detection(self):
        """同一工具连续调用超阈值 → 检测。"""
        m = RepetitiveCallMonitor({"repeat_threshold": 3})
        result = None
        for i in range(3):
            result = m.process_event(_make_event("tool_call", tool_name="tool_get_telemetry"))
        assert result is not None
        assert result.fault_detected
        assert result.fault_type == "REPETITIVE_CALLS"
        assert result.evidence["consecutive_count"] >= 3

    def test_tool_change_resets_count(self):
        """工具切换重置连续计数。"""
        m = RepetitiveCallMonitor({"repeat_threshold": 4})
        for _ in range(3):
            m.process_event(_make_event("tool_call", tool_name="tool_A"))
        assert m.consecutive_count == 3
        m.process_event(_make_event("tool_call", tool_name="tool_B"))
        assert m.consecutive_count == 1
        assert m.last_tool == "tool_B"


class TestRSMIntegration:
    """RSM 主类集成测试。"""

    def test_rsm_processes_normal_events(self):
        """RSM 正常事件流不报错。"""
        rsm = RuntimeStabilityMonitor({})
        events = [
            _make_event("heartbeat", uptime_s=0.0, active_faults=[]),
            _make_event("telemetry", seq=1),
            _make_event("task_progress", step=1, action="read_telemetry"),
            _make_event("tool_call", call_id="C1", tool_name="tool_get_telemetry"),
            _make_event("tool_result", call_id="C1"),
            _make_event("agent_output", status="completed"),
        ]
        for event in events:
            result = rsm.process_event(event)
            assert result is not None
            assert result.monitor_type == "RSM"

    def test_rsm_detects_crash_in_stream(self):
        """RSM 在事件流中检测到进程崩溃。"""
        rsm = RuntimeStabilityMonitor({"rsm": {"heartbeat": {"heartbeat_timeout_s": 0.1}}})
        rsm.process_event(_make_event("heartbeat", uptime_s=0.0, timestamp_ms=1000))
        result = rsm.process_event(_make_event("telemetry", timestamp_ms=1300))
        assert result.fault_detected
        assert result.fault_type == "PROCESS_CRASH"
        assert len(result.issues) >= 1

    def test_rsm_result_has_all_detector_states(self):
        """RSMResult 包含所有检测器状态。"""
        rsm = RuntimeStabilityMonitor({})
        result = rsm.process_event(_make_event("heartbeat", uptime_s=0.0))
        assert "heartbeat" in result.detector_states
        assert "stall" in result.detector_states
        assert "tool_timeout" in result.detector_states
        assert "memory" in result.detector_states
        assert "repetitive_calls" in result.detector_states

    def test_rsm_context_extraction(self):
        """RSM 从事件中提取 run_id/task_id/sat_id。"""
        rsm = RuntimeStabilityMonitor({})
        rsm.process_event(_make_event("heartbeat", run_id="RUN-123", task_id="TASK-456", sat_id="SAT-789"))
        assert rsm._run_id == "RUN-123"
        assert rsm._task_id == "TASK-456"
        assert rsm._sat_id == "SAT-789"

    def test_rsm_summary(self):
        """RSM get_summary 返回运行摘要。"""
        rsm = RuntimeStabilityMonitor({})
        rsm.process_event(_make_event("heartbeat"))
        summary = rsm.get_summary()
        assert summary["events_processed"] == 1
        assert "detectors" in summary


def run_all_tests():
    """直接运行所有测试。"""
    print("=" * 60)
    print("RSM 运行稳定性监护器 - 自动化测试")
    print("=" * 60)

    test_classes = [
        TestHeartbeatMonitor, TestStallMonitor, TestToolTimeoutMonitor,
        TestMemoryMonitor, TestRepetitiveCallMonitor, TestRSMIntegration,
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
