# -*- coding: utf-8 -*-
"""
故障注入测试床 - 自动化测试。
验证各类故障的触发率和行为一致性。

运行: python -m pytest tests/test_faults.py -v
或:   python tests/test_faults.py
"""
import sys
import os
import json
import time
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.task_agent import TaskAgent
from agent.telemetry import TelemetrySource
from agent.diagnosis import DiagnosisEngine
from agent.tools import ToolRegistry
from agent.noise_model import NoiseModel
from fault_injector.injector import FaultInjector


def _make_config(fault_name: str = None, layer: str = "stability") -> dict:
    """生成测试用配置。"""
    config = {
        "agent": {
            "task_id": "TEST-001",
            "sat_id": "SAT-TEST",
            "telemetry_interval_ms": 100,
            "heartbeat_interval_ms": 200,
            "max_tool_calls": 5,
            "decision_timeout_ms": 5000,
        },
        "fault_injector": {
            "global_enabled": fault_name is not None,
            "stability_faults": {},
            "credibility_faults": {},
        },
        "ground_truth": {"enabled": True, "log_dir": "logs", "log_file": "test_log.jsonl"},
    }
    if fault_name:
        layer_key = "stability_faults" if layer == "stability" else "credibility_faults"
        config["fault_injector"][layer_key][fault_name] = {"enabled": True}
    return config


class TestNormalMode:
    """正常模式测试。"""

    def test_agent_runs_successfully(self):
        config = _make_config()
        agent = TaskAgent(config)
        result = agent.run_task()
        assert result["status"] == "completed"
        assert "conclusion" in result
        assert "confidence" in result
        assert len(agent.get_event_log()) > 0

    def test_telemetry_source_generates_valid_data(self):
        ts = TelemetrySource("SAT-TEST")
        frame = ts.get_telemetry()
        assert frame["sat_id"] == "SAT-TEST"
        assert frame["seq"] == 1
        assert "bus_voltage" in frame["telemetry"]
        assert "battery_soc" in frame["telemetry"]

    def test_diagnosis_engine_detects_faults(self):
        engine = DiagnosisEngine()
        # 构造 BCR 开路的遥测
        frame = {"telemetry": {"bcr_current": 0.01, "sa_current": 5.0, "bus_voltage": 28.0}}
        faults = engine.diagnose(frame)
        assert any(f.fault_code == "BCR_OPEN" for f in faults)

    def test_tool_registry_calls_tools(self):
        registry = ToolRegistry()
        result = registry.call("tool_get_telemetry", {"sensor": "bus_voltage"})
        assert result.ok is True
        assert result.tool_name == "tool_get_telemetry"

    def test_heartbeat_events_generated(self):
        config = _make_config()
        agent = TaskAgent(config)
        agent.run_task()
        heartbeats = [e for e in agent.get_event_log() if e["event_type"] == "heartbeat"]
        assert len(heartbeats) >= 1


class TestCredibilityFaults:
    """可信性故障测试。"""

    def test_wrong_diagnosis_injected(self):
        config = _make_config("wrong_diagnosis", "credibility")
        config["fault_injector"]["credibility_faults"]["wrong_diagnosis"]["wrong_fault_code"] = "SA_BRANCH_SHORT"
        config["fault_injector"]["credibility_faults"]["wrong_diagnosis"]["correct_fault_code"] = "BCR_OPEN"
        injector = FaultInjector(config)
        # 直接测试注入器方法，传入一个有 BCR_OPEN 故障的诊断结果
        sample_faults = [{"fault_code": "BCR_OPEN", "severity": "high",
                          "description": "BCR开路", "confidence": 0.92,
                          "supporting_evidence": [{"metric": "bcr_current", "value": 0.01}]}]
        result = injector.inject_diagnosis_output(sample_faults)
        assert result[0]["fault_code"] == "SA_BRANCH_SHORT"
        assert "INJECTED" in result[0]["description"]
        gt = injector.gt_logger.get_records()
        assert any(r["fault_type"] == "wrong_diagnosis" for r in gt)

    def test_inflated_confidence_injected(self):
        config = _make_config("inflated_confidence", "credibility")
        config["fault_injector"]["credibility_faults"]["inflated_confidence"]["confidence_range"] = [0.90, 0.95]
        injector = FaultInjector(config)
        # 传入一个有故障的诊断结果（非 NO_FAULT）
        sample_faults = [{"fault_code": "BCR_OPEN", "severity": "high",
                          "description": "BCR开路", "confidence": 0.50,
                          "supporting_evidence": [{"metric": "bcr_current", "value": 0.01}]}]
        result = injector.inject_diagnosis_output(sample_faults)
        assert result[0]["confidence"] >= 0.90
        assert result[0]["confidence"] <= 0.95
        gt = injector.gt_logger.get_records()
        assert any(r["fault_type"] == "inflated_confidence" for r in gt)

    def test_missing_evidence_injected(self):
        config = _make_config("missing_evidence", "credibility")
        injector = FaultInjector(config)
        # 传入一个有故障且有证据的诊断结果
        sample_faults = [{"fault_code": "BCR_OPEN", "severity": "high",
                          "description": "BCR开路", "confidence": 0.92,
                          "supporting_evidence": [{"metric": "bcr_current", "value": 0.01},
                                                  {"metric": "sa_current", "value": 5.0}]}]
        result = injector.inject_diagnosis_output(sample_faults)
        assert result[0]["supporting_evidence"] == []
        gt = injector.gt_logger.get_records()
        assert any(r["fault_type"] == "missing_evidence" for r in gt)

    def test_contradictory_sensors_injected(self):
        config = _make_config("contradictory_sensors", "credibility")
        config["fault_injector"]["credibility_faults"]["contradictory_sensors"]["faulty_sensors"] = {"bus_voltage": 0.0}
        injector = FaultInjector(config)
        agent = TaskAgent(config, injector)
        # 检查遥测源是否被设置了故障传感器
        assert agent.telemetry_source._faulty_sensors.get("bus_voltage") == 0.0
        frame = agent.telemetry_source.get_telemetry()
        assert frame["telemetry"]["bus_voltage"] == 0.0


class TestStabilityFaults:
    """稳定性故障测试（注意：部分故障会阻塞或崩溃，用线程+超时测试）。"""

    def test_process_crash_raises_exception(self):
        config = _make_config("process_crash", "stability")
        config["fault_injector"]["stability_faults"]["process_crash"]["mode"] = "exception"
        config["fault_injector"]["stability_faults"]["process_crash"]["trigger_step"] = 3
        injector = FaultInjector(config)
        agent = TaskAgent(config, injector)
        result = agent.run_task()
        # 进程崩溃故障应该导致任务失败
        assert result["status"] == "failed"
        assert "Simulated crash" in result.get("error", "")

    def test_no_output_timeout_returns_timeout(self):
        config = _make_config("no_output_timeout", "stability")
        config["fault_injector"]["stability_faults"]["no_output_timeout"]["sleep_seconds"] = 1
        config["fault_injector"]["stability_faults"]["no_output_timeout"]["trigger_step"] = 4
        injector = FaultInjector(config)
        agent = TaskAgent(config, injector)
        result = agent.run_task()
        gt = injector.gt_logger.get_records()
        assert any(r["fault_type"] == "no_output_timeout" for r in gt)

    def test_memory_bloat_allocates_memory(self):
        config = _make_config("memory_bloat", "stability")
        config["fault_injector"]["stability_faults"]["memory_bloat"]["alloc_mb_per_step"] = 10
        config["fault_injector"]["stability_faults"]["memory_bloat"]["max_alloc_mb"] = 30
        config["fault_injector"]["stability_faults"]["memory_bloat"]["trigger_step"] = 4
        injector = FaultInjector(config)
        agent = TaskAgent(config, injector)
        result = agent.run_task()
        gt = injector.gt_logger.get_records()
        assert any(r["fault_type"] == "memory_bloat" for r in gt)
        # 检查是否分配了内存
        assert hasattr(agent, '_bloat_data')
        assert len(agent._bloat_data) > 0


class TestGroundTruth:
    """Ground truth 日志测试。"""

    def test_ground_truth_logged(self):
        config = _make_config("wrong_diagnosis", "credibility")
        injector = FaultInjector(config)
        agent = TaskAgent(config, injector)
        agent.run_task()
        records = injector.gt_logger.get_records()
        assert len(records) >= 1
        assert records[0]["fault_type"] == "wrong_diagnosis"
        assert "expected_behavior" in records[0]
        assert "timestamp_ms" in records[0]

    def test_ground_truth_not_visible_in_events(self):
        """ground truth 不应该出现在 Agent 的事件日志中（保证监护器看不到答案）。"""
        config = _make_config("wrong_diagnosis", "credibility")
        injector = FaultInjector(config)
        agent = TaskAgent(config, injector)
        agent.run_task()
        event_log = agent.get_event_log()
        # 事件日志中不应该有 injection_id 字段（那是 ground truth 的字段）
        for event in event_log:
            assert "injection_id" not in event


class TestOutputFormat:
    """统一输出格式测试。"""

    def test_agent_output_has_required_fields(self):
        config = _make_config()
        agent = TaskAgent(config)
        result = agent.run_task()
        required_fields = ["event_type", "task_id", "sat_id", "status",
                           "conclusion", "confidence", "diagnosis", "tool_calls",
                           "evidence", "start_time_ms", "end_time_ms", "duration_ms"]
        for field in required_fields:
            assert field in result, f"缺少字段: {field}"

    def test_tool_result_has_required_fields(self):
        registry = ToolRegistry()
        result = registry.call("tool_get_telemetry")
        d = result.to_dict()
        required = ["call_id", "tool_name", "ok", "observation", "error_message",
                    "data", "duration_ms", "timestamp_ms"]
        for field in required:
            assert field in d, f"缺少字段: {field}"


class TestMultiFaultOverlay:
    """多故障叠加引擎测试（阶段一1.1）。"""

    def _make_multi_config(self, faults: list) -> dict:
        """生成多故障配置。"""
        config = {
            "agent": {"task_id": "TEST-001", "sat_id": "SAT-TEST",
                      "telemetry_interval_ms": 100, "heartbeat_interval_ms": 200},
            "fault_injector": {"global_enabled": True, "stability_faults": {}, "credibility_faults": {}},
            "ground_truth": {"log_dir": "logs", "log_file": "test_gt.jsonl"},
        }
        for f in faults:
            if f in ["infinite_loop", "process_crash", "no_output_timeout", "memory_bloat", "repetitive_calls"]:
                config["fault_injector"]["stability_faults"][f] = {"enabled": True, "alloc_mb_per_step": 10, "max_alloc_mb": 50}
            else:
                config["fault_injector"]["credibility_faults"][f] = {"enabled": True}
        return config

    def test_cross_layer_overlay_memory_bloat_wrong_diagnosis(self):
        """跨层叠加：memory_bloat（稳定性）+ wrong_diagnosis（可信性）同时触发。"""
        config = self._make_multi_config(["memory_bloat", "wrong_diagnosis"])
        injector = FaultInjector(config)
        agent = TaskAgent(config, injector)
        result = agent.run_task("fault_diagnosis_and_recovery")
        gt = injector.gt_logger.get_records()
        fault_types = [r["fault_type"] for r in gt]
        assert "memory_bloat" in fault_types, "memory_bloat 未触发"
        assert "wrong_diagnosis" in fault_types, "wrong_diagnosis 未触发"
        assert result["status"] == "completed"
        assert "SA_BRANCH_SHORT" in result["conclusion"], "wrong_diagnosis 未生效"

    def test_credibility_overlay_wrong_diagnosis_inflated_confidence(self):
        """可信性故障叠加：wrong_diagnosis + inflated_confidence 同时触发。"""
        config = self._make_multi_config(["wrong_diagnosis", "inflated_confidence"])
        injector = FaultInjector(config)
        agent = TaskAgent(config, injector)
        result = agent.run_task("fault_diagnosis_and_recovery")
        gt = injector.gt_logger.get_records()
        fault_types = [r["fault_type"] for r in gt]
        assert "wrong_diagnosis" in fault_types
        assert "inflated_confidence" in fault_types
        assert result["confidence"] >= 0.85, "inflated_confidence 未生效"

    def test_injector_get_enabled_execution_faults(self):
        """injector.get_enabled_execution_faults 返回所有已启用执行层故障。"""
        config = self._make_multi_config(["memory_bloat", "wrong_diagnosis"])
        injector = FaultInjector(config)
        exec_faults = injector.get_enabled_execution_faults()
        assert "memory_bloat" in exec_faults
        assert "wrong_diagnosis" not in exec_faults  # 可信性不在执行层

    def test_injector_get_enabled_credibility_faults(self):
        """injector.get_enabled_credibility_faults 返回所有已启用可信性故障。"""
        config = self._make_multi_config(["memory_bloat", "wrong_diagnosis", "inflated_confidence"])
        injector = FaultInjector(config)
        cred_faults = injector.get_enabled_credibility_faults()
        assert "wrong_diagnosis" in cred_faults
        assert "inflated_confidence" in cred_faults
        assert "memory_bloat" not in cred_faults

    def test_injector_get_all_enabled_faults(self):
        """injector.get_all_enabled_faults 按层分组返回所有已启用故障。"""
        config = self._make_multi_config(["memory_bloat", "wrong_diagnosis"])
        injector = FaultInjector(config)
        all_faults = injector.get_all_enabled_faults()
        assert "memory_bloat" in all_faults["execution"]
        assert "wrong_diagnosis" in all_faults["credibility"]

    def test_terminal_faults_mutually_exclusive(self):
        """终结性故障（infinite_loop/process_crash）互斥：get_triggered_execution_faults 最多返回一个。"""
        config = self._make_multi_config(["infinite_loop", "process_crash"])
        injector = FaultInjector(config)
        # 手动设置 step 让故障触发
        injector._step_counter = 5
        triggered = injector.get_triggered_execution_faults()
        assert len(triggered["terminal"]) <= 1, "终结性故障不应同时触发多个"


class TestTelemetryNoise:
    """真实遥测噪声模型测试（阶段一1.2）。"""

    def test_gaussian_noise_changes_values(self):
        """高斯噪声：启用后遥测值发生变化，且 noise_status 标记 active。"""
        ts = TelemetrySource("SAT-TEST", noise_config={"enabled": True, "gaussian": {"enabled": True, "sigma_scale": 0.2}})
        frame = ts.get_telemetry()
        assert frame is not None
        assert "gaussian" in frame["noise_status"]["active_modes"]
        # 高斯噪声下值应该有变化（多次采样不全相同）
        values = [ts.get_telemetry()["telemetry"]["bus_voltage"] for _ in range(5)]
        assert len(set(values)) > 1, "高斯噪声下值不应完全相同"

    def test_packet_loss_returns_none(self):
        """数据丢包：高概率下 get_telemetry 应返回 None。"""
        ts = TelemetrySource("SAT-TEST", noise_config={"enabled": True, "packet_loss": {"enabled": True, "probability": 0.9}})
        dropped = sum(1 for _ in range(20) if ts.get_telemetry() is None)
        assert dropped >= 10, "90%%丢包概率下20帧应至少丢10帧，实际丢%d" % dropped

    def test_spike_noise_produces_spikes(self):
        """脉冲干扰：启用后 noise_status.spike_active 非空。"""
        ts = TelemetrySource("SAT-TEST", noise_config={"enabled": True, "spike": {"enabled": True, "probability": 0.8, "magnitude_scale": 5.0}})
        spike_found = False
        for _ in range(15):
            f = ts.get_telemetry()
            if f and f["noise_status"]["spike_active"]:
                spike_found = True
                break
        assert spike_found, "80%%概率下15帧内应检测到尖峰"

    def test_drift_noise_accumulates(self):
        """随机漂移：drift_accumulator 有值，值随时间偏移。"""
        nm = NoiseModel({"enabled": True, "drift": {"enabled": True, "drift_rate_per_sec": 0.01}})
        data = {"bus_voltage": 28.0}
        r1 = nm.apply(data)
        assert "bus_voltage" in r1
        # drift_accumulator 应该已初始化
        assert len(nm._drift_accumulator) > 0

    def test_noise_disabled_returns_original(self):
        """噪声禁用时：apply 返回原始数据不变。"""
        nm = NoiseModel({"enabled": False})
        data = {"bus_voltage": 28.0, "temp_obc": 25.0}
        result = nm.apply(data)
        assert result == data

    def test_noise_status_in_telemetry_frame(self):
        """遥测帧包含 noise_status 字段。"""
        ts = TelemetrySource("SAT-TEST", noise_config={"enabled": False})
        frame = ts.get_telemetry()
        assert "noise_status" in frame
        assert frame["noise_status"]["enabled"] is False

    def test_agent_runs_with_gaussian_noise(self):
        """Agent 在高斯噪声下能正常完成任务。"""
        config = {
            "agent": {"task_id": "TEST-NOISE", "sat_id": "SAT-TEST",
                      "telemetry_interval_ms": 100, "heartbeat_interval_ms": 200,
                      "telemetry_noise": {"enabled": True, "gaussian": {"enabled": True, "sigma_scale": 0.05}}},
            "fault_injector": {"global_enabled": False, "stability_faults": {}, "credibility_faults": {}},
            "ground_truth": {"log_dir": "logs", "log_file": "test_noise_gt.jsonl"},
        }
        agent = TaskAgent(config)
        result = agent.run_task("fault_diagnosis_and_recovery")
        assert result["status"] == "completed"


class TestFaultIntensity:
    """故障强度分级测试（阶段一1.3）。"""

    def _make_intensity_config(self, fault_name: str, intensity: str, layer: str = "stability") -> dict:
        config = {
            "agent": {"task_id": "TEST-INT", "sat_id": "SAT-TEST",
                      "telemetry_interval_ms": 100, "heartbeat_interval_ms": 200},
            "fault_injector": {"global_enabled": True, "stability_faults": {}, "credibility_faults": {}},
            "ground_truth": {"log_dir": "logs", "log_file": "test_intensity_gt.jsonl"},
        }
        layer_key = "stability_faults" if layer == "stability" else "credibility_faults"
        config["fault_injector"][layer_key][fault_name] = {"enabled": True, "intensity": intensity}
        return config

    def test_memory_bloat_light_intensity(self):
        """memory_bloat light：alloc_mb=10, max=100。"""
        config = self._make_intensity_config("memory_bloat", "light")
        injector = FaultInjector(config)
        cfg = injector.get_fault_config("memory_bloat")
        assert cfg["alloc_mb_per_step"] == 10
        assert cfg["max_alloc_mb"] == 100
        assert injector.get_intensity("memory_bloat") == "light"

    def test_memory_bloat_heavy_intensity(self):
        """memory_bloat heavy：alloc_mb=200, max=1000。"""
        config = self._make_intensity_config("memory_bloat", "heavy")
        injector = FaultInjector(config)
        cfg = injector.get_fault_config("memory_bloat")
        assert cfg["alloc_mb_per_step"] == 200
        assert cfg["max_alloc_mb"] == 1000

    def test_inflated_confidence_light_range(self):
        """inflated_confidence light：置信度范围 0.70-0.80。"""
        config = self._make_intensity_config("inflated_confidence", "light", "credibility")
        injector = FaultInjector(config)
        cfg = injector.get_fault_config("inflated_confidence", "credibility")
        assert cfg["confidence_range"] == [0.70, 0.80]

    def test_inflated_confidence_heavy_range(self):
        """inflated_confidence heavy：置信度范围 0.95-0.99。"""
        config = self._make_intensity_config("inflated_confidence", "heavy", "credibility")
        injector = FaultInjector(config)
        cfg = injector.get_fault_config("inflated_confidence", "credibility")
        assert cfg["confidence_range"] == [0.95, 0.99]

    def test_explicit_params_not_overridden_by_intensity(self):
        """显式指定的参数不被 intensity 预设覆盖。"""
        config = self._make_intensity_config("memory_bloat", "light")
        config["fault_injector"]["stability_faults"]["memory_bloat"]["max_alloc_mb"] = 999
        injector = FaultInjector(config)
        cfg = injector.get_fault_config("memory_bloat")
        assert cfg["max_alloc_mb"] == 999  # 显式值保留
        assert cfg["alloc_mb_per_step"] == 10  # 预设值填充

    def test_missing_evidence_light_keeps_one(self):
        """missing_evidence light：保留1条证据。"""
        config = self._make_intensity_config("missing_evidence", "light", "credibility")
        injector = FaultInjector(config)
        agent = TaskAgent(config, injector)
        # 先注入 wrong_diagnosis 让结论不是 NO_FAULT
        config2 = dict(config)
        config2["fault_injector"] = dict(config["fault_injector"])
        config2["fault_injector"]["credibility_faults"] = dict(config["fault_injector"]["credibility_faults"])
        config2["fault_injector"]["credibility_faults"]["wrong_diagnosis"] = {"enabled": True}
        injector2 = FaultInjector(config2)
        agent2 = TaskAgent(config2, injector2)
        result = agent2.run_task("fault_diagnosis_and_recovery")
        # 检查诊断中的证据数量
        diagnosis = result.get("diagnosis", [])
        if diagnosis:
            evidence = diagnosis[0].get("supporting_evidence", [])
            assert len(evidence) <= 1, "light强度应保留最多1条证据，实际%d条" % len(evidence)

    def test_repetitive_calls_heavy_repeat_12(self):
        """repetitive_calls heavy：repeat_count=12。"""
        config = self._make_intensity_config("repetitive_calls", "heavy")
        injector = FaultInjector(config)
        cfg = injector.get_fault_config("repetitive_calls")
        assert cfg["repeat_count"] == 12

    def test_no_intensity_uses_defaults(self):
        """未设置 intensity 时不应用预设，使用配置中的显式值或默认值。"""
        config = {
            "agent": {"task_id": "TEST", "sat_id": "SAT-TEST"},
            "fault_injector": {"global_enabled": True, "stability_faults": {
                "memory_bloat": {"enabled": True, "alloc_mb_per_step": 77}
            }, "credibility_faults": {}},
            "ground_truth": {"log_dir": "logs", "log_file": "test.jsonl"},
        }
        injector = FaultInjector(config)
        cfg = injector.get_fault_config("memory_bloat")
        assert cfg["alloc_mb_per_step"] == 77  # 显式值
        assert "max_alloc_mb" not in cfg or cfg.get("max_alloc_mb") is None or cfg["max_alloc_mb"] != 100
        assert injector.get_intensity("memory_bloat") is None


class TestFaultCoverage:
    """故障覆盖率量化报告测试（阶段一1.4）。"""

    def test_coverage_report_generates(self):
        """覆盖率报告能正常生成，包含必要字段。"""
        from fault_injector.coverage_report import calculate_coverage, generate_text_report
        report = calculate_coverage()
        assert "summary" in report
        assert "layer_stats" in report
        assert "coverage_matrix" in report
        assert "gaps" in report
        assert report["summary"]["total_subdimensions"] == 22
        assert report["summary"]["covered_subdimensions"] == 10  # P2修复：packet_loss 已计入
        assert report["summary"]["overall_coverage_rate"] > 0

    def test_coverage_report_text_not_empty(self):
        """文本报告非空且包含关键章节。"""
        from fault_injector.coverage_report import calculate_coverage, generate_text_report
        report = calculate_coverage()
        text = generate_text_report(report)
        assert len(text) > 100
        assert "总览" in text
        assert "覆盖矩阵" in text
        assert "空白区域" in text

    def test_all_faults_in_coverage_map(self):
        """9类故障 + packet_loss（噪声层实现）全部在覆盖率映射中。"""
        from fault_injector.coverage_report import FAULT_COVERAGE_MAP
        expected = {"infinite_loop", "no_output_timeout", "memory_bloat", "process_crash",
                    "repetitive_calls", "wrong_diagnosis", "inflated_confidence",
                    "missing_evidence", "contradictory_sensors", "packet_loss"}
        assert set(FAULT_COVERAGE_MAP.keys()) == expected

    def test_gaps_are_uncovered_subdimensions(self):
        """空白区域数量 = 总数 - 已覆盖数。"""
        from fault_injector.coverage_report import calculate_coverage
        report = calculate_coverage()
        assert len(report["gaps"]) == report["summary"]["total_subdimensions"] - report["summary"]["covered_subdimensions"]


def run_all_tests():
    """直接运行所有测试（不依赖 pytest）。"""
    print("=" * 60)
    print("故障注入测试床 - 自动化测试")
    print("=" * 60)

    test_classes = [TestNormalMode, TestCredibilityFaults, TestStabilityFaults,
                    TestGroundTruth, TestOutputFormat, TestMultiFaultOverlay,
                    TestTelemetryNoise, TestFaultIntensity, TestFaultCoverage]
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
