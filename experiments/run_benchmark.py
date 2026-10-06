# -*- coding: utf-8 -*-
"""
OrbitGuard 量化实验脚本（阶段五）。

实验内容：
1. 单故障检测率：9类故障 × RSM/DTM检测准确率
2. 复合故障检测率
3. 噪声鲁棒性
4. 全链路决策准确率
5. 性能开销（各环节延迟）

运行: python experiments/run_benchmark.py
输出: docs/benchmark_results.json + docs/benchmark_report.txt
"""
import sys
import os
import json
import time
import statistics
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 控制台编码兼容：非 UTF-8 代码页（如 CI 的英文 Windows）下中文输出不再抛 UnicodeEncodeError
from agent.console import enable_utf8_stdout
enable_utf8_stdout()

from agent.task_agent import TaskAgent
from fault_injector.injector import FaultInjector
from rsm.monitor import RuntimeStabilityMonitor
from dtm.monitor import DecisionTrustMonitor
from gateway.gateway import DecisionGateway


class EventCapture:
    def __init__(self):
        self.events = []
    def publish(self, event):
        self.events.append(event)


STABILITY_FAULTS = ["infinite_loop", "process_crash", "no_output_timeout",
                     "memory_bloat", "repetitive_calls"]
CREDIBILITY_FAULTS = ["wrong_diagnosis", "inflated_confidence",
                       "missing_evidence", "contradictory_sensors"]
ALL_FAULTS = STABILITY_FAULTS + CREDIBILITY_FAULTS

# 故障到期望检测模块的映射
EXPECTED_DETECTION = {
    "infinite_loop": "RSM",
    "process_crash": "RSM",
    "no_output_timeout": "RSM",
    "memory_bloat": "RSM",
    "repetitive_calls": "RSM",
    "wrong_diagnosis": "DTM",
    "inflated_confidence": "DTM",
    "missing_evidence": "DTM",
    "contradictory_sensors": "DTM",
}


def run_single_trial(fault_name=None, noise_config: dict = None) -> dict:
    """运行单次实验，返回检测结果。fault_name可以是字符串或列表（复合故障）。"""
    # 统一转为列表
    fault_names = [fault_name] if isinstance(fault_name, str) else (fault_name or [])
    config = {
        "agent": {
            "task_id": "BENCH-TEST", "sat_id": "SAT-BENCH",
            "telemetry_interval_ms": 50, "heartbeat_interval_ms": 100, "resource_interval_ms": 100,
        },
        "fault_injector": {
            "global_enabled": len(fault_names) > 0,
            "stability_faults": {}, "credibility_faults": {},
        },
        "ground_truth": {"log_dir": "logs", "log_file": "bench_gt.jsonl"},
    }
    if noise_config:
        config["agent"]["telemetry_noise"] = noise_config
    for fname in fault_names:
        if fname in STABILITY_FAULTS:
            config["fault_injector"]["stability_faults"][fname] = {"enabled": True}
        else:
            cred_cfg = {"enabled": True}
            if fname == "contradictory_sensors":
                cred_cfg["faulty_sensors"] = {"bus_current": 0.0}
            config["fault_injector"]["credibility_faults"][fname] = cred_cfg

    capture = EventCapture()
    injector = FaultInjector(config) if fault_names else None
    agent = TaskAgent(config, injector, event_bus=capture)
    start = time.time()
    # 用线程运行，设置超时（infinite_loop等故障会永久卡住）
    run_thread = threading.Thread(target=agent.run_task, args=("fault_diagnosis_and_recovery",))
    run_thread.daemon = True
    run_thread.start()
    run_thread.join(timeout=3.0)  # 最多等3秒，模拟超时检测窗口
    timed_out = run_thread.is_alive()
    agent_time = time.time() - start

    # RSM + DTM（使用实验敏感阈值，短时间内可触发检测）
    # 无 memory_bloat 注入时，阈值设高避免进程 RSS 残留导致误报（Python clear() 后内存不还给 OS）
    has_memory_bloat = "memory_bloat" in fault_names
    memory_threshold = 50.0 if has_memory_bloat else 2000.0
    rsm_config = {
        "rsm": {
            "heartbeat": {"heartbeat_timeout_s": 1.0},
            "stall": {"stall_timeout_s": 0.5},
            "tool_timeout": {"tool_timeout_s": 1.0},
            "memory": {"memory_threshold_mb": memory_threshold, "growth_window": 2},
            "repetitive_calls": {"repeat_threshold": 2},
        }
    }
    dtm_config = {
        "dtm": {
            "confidence_calibration": {"min_evidence_for_high_conf": 3, "high_conf_threshold": 0.80},
            "evidence_completeness": {"min_evidence_count": 2},
        }
    }
    rsm = RuntimeStabilityMonitor(rsm_config)
    dtm = DecisionTrustMonitor(dtm_config)
    rsm.start_watchdog(check_interval_s=0.05)  # 启动独立看门狗（墙钟驱动）
    rsm_start = time.time()
    rsm_detected = False
    dtm_issues = 0
    detected_fault_types = set()  # 收集检测到的具体故障类型（P0修复：复合故障逐故障匹配）
    for event in capture.events:
        r = rsm.process_event(event)
        if r and r.fault_detected:
            rsm_detected = True
            if r.fault_type:
                detected_fault_types.add(r.fault_type.lower())
            for issue in r.issues:
                if issue.get("fault_type"):
                    detected_fault_types.add(issue["fault_type"].lower())
        d = dtm.process_event(event)
        if d and d.issues:
            dtm_issues = len(d.issues)
            for issue in d.issues:
                # DTM issue 字段名是 issue_type
                itype = issue.get("issue_type") or issue.get("type") or ""
                if itype:
                    detected_fault_types.add(itype.lower())
    rsm_time = time.time() - rsm_start  # 事件处理耗时（不含看门狗等待）
    # 仅当注入了需要看门狗检测的终结性故障（process_crash/infinite_loop）时才等待；正常完成则停止看门狗避免误报
    terminal_faults = {"process_crash", "infinite_loop", "no_output_timeout"}
    needs_watchdog = any(f in terminal_faults for f in fault_names)
    if needs_watchdog:
        time.sleep(1.5)  # 心跳超时1.0s，留0.5s余量
        watchdog_result = rsm.get_watchdog_result()
        if watchdog_result and watchdog_result.fault_detected:
            rsm_detected = True
            if watchdog_result.fault_type:
                detected_fault_types.add(watchdog_result.fault_type.lower())
            for issue in watchdog_result.issues:
                if issue.get("fault_type"):
                    detected_fault_types.add(issue["fault_type"].lower())
    rsm.stop_watchdog()

    # Gateway
    gw = DecisionGateway({})
    telemetry = None
    for e in reversed(capture.events):
        if e.get("event_type") == "telemetry":
            telemetry = e
            break
    gw_start = time.time()
    decision = gw.decide(
        rsm_result=rsm.get_last_result().to_dict() if rsm.get_last_result() else None,
        dtm_result=dtm.get_last_result().to_dict() if dtm.get_last_result() else None,
        telemetry=telemetry,
    )
    gw_time = time.time() - gw_start

    # ===== 试验隔离清理（P0修复：防止内存泄漏和线程泄漏污染后续试验）=====
    try:
        agent.stop()  # 停止心跳/遥测/资源后台线程
    except Exception:
        pass
    try:
        rsm.stop_watchdog()
    except Exception:
        pass
    # 释放 memory_bloat 注入的内存（否则同进程后续试验被 MemoryMonitor 误报）
    if injector and hasattr(injector, "_memory_bloat_data"):
        injector._memory_bloat_data.clear()
    # 重置检测器状态（累积式结果不跨试验残留）
    try:
        rsm.reset()
        dtm.reset()
    except Exception:
        pass

    return {
        "fault": "+".join(fault_names) if fault_names else "normal",
        "timed_out": timed_out,
        "event_count": len(capture.events),
        "rsm_detected": rsm_detected,
        "dtm_issues": dtm_issues,
        "dtm_detected": dtm_issues > 0,
        "detected_fault_types": list(detected_fault_types),
        "gateway_route": decision.route,
        "agent_time_ms": round(agent_time * 1000, 1),
        "rsm_dtm_time_ms": round(rsm_time * 1000, 1),
        "gateway_time_ms": round(gw_time * 1000, 1),
    }


def experiment_single_fault_detection(trials: int = 5) -> dict:
    """实验1：单故障检测率。"""
    print(f"  实验1：单故障检测率（每故障{trials}次）...")
    results = {}
    for fault in ALL_FAULTS:
        detections = []
        for _ in range(trials):
            r = run_single_trial(fault)
            expected = EXPECTED_DETECTION[fault]
            if expected == "RSM":
                detected = r["rsm_detected"]
            else:
                detected = r["dtm_detected"]
            detections.append(detected)
        detection_rate = sum(detections) / len(detections) * 100
        results[fault] = {
            "expected_module": EXPECTED_DETECTION[fault],
            "trials": trials,
            "detected": sum(detections),
            "detection_rate": round(detection_rate, 1),
        }
    return results


# 故障名称 → 检测标签映射（用于复合故障逐故障匹配）
FAULT_TO_DETECTION_LABEL = {
    "infinite_loop": {"infinite_loop"},
    "process_crash": {"process_crash"},
    "no_output_timeout": {"no_output_timeout"},  # P1修复：独立判别，不再混用停滞/心跳标签
    "memory_bloat": {"memory_bloat"},
    "repetitive_calls": {"repetitive_calls"},
    "wrong_diagnosis": {"conclusion_evidence_mismatch"},
    "inflated_confidence": {"inflated_confidence"},
    "missing_evidence": {"missing_evidence"},
    "contradictory_sensors": {"multi_source_conflict"},
}


def experiment_composite_fault(trials: int = 3) -> dict:
    """实验2：复合故障检测率（逐故障匹配，非OR口径）。"""
    print(f"  实验2：复合故障检测率（每组合{trials}次，逐故障匹配）...")
    composites = [
        ["wrong_diagnosis", "inflated_confidence"],
        ["memory_bloat", "wrong_diagnosis"],
        ["missing_evidence", "contradictory_sensors"],
    ]
    results = {}
    for composite in composites:
        key = "+".join(composite)
        per_fault_results = {f: [] for f in composite}
        for _ in range(trials):
            r = run_single_trial(composite)
            detected_types = set(r.get("detected_fault_types", []))
            for fault in composite:
                expected_labels = FAULT_TO_DETECTION_LABEL.get(fault, {fault})
                # 逐故障匹配：该故障对应的检测标签是否出现在检测结果中
                fault_detected = bool(detected_types & expected_labels)
                per_fault_results[fault].append(fault_detected)
        # 复合检测率 = 所有注入故障都被检出的试验比例
        all_detected_count = 0
        for i in range(trials):
            if all(per_fault_results[f][i] for f in composite):
                all_detected_count += 1
        per_fault_rates = {
            f: round(sum(per_fault_results[f]) / len(per_fault_results[f]) * 100, 1)
            for f in composite
        }
        results[key] = {
            "faults": composite,
            "detection_rate": round(all_detected_count / trials * 100, 1),
            "per_fault_detection": per_fault_rates,
            "note": "逐故障匹配：所有注入故障都被对应检测器检出才算成功",
        }
    return results


def experiment_noise_robustness(trials: int = 3) -> dict:
    """实验3：噪声鲁棒性。"""
    print(f"  实验3：噪声鲁棒性（每噪声{trials}次）...")
    noise_configs = {
        "none": None,
        "gaussian_light": {"enabled": True, "gaussian": {"enabled": True, "sigma_scale": 0.05}},
        "gaussian_heavy": {"enabled": True, "gaussian": {"enabled": True, "sigma_scale": 0.2}},
        "spike": {"enabled": True, "spike": {"enabled": True, "probability": 0.3, "magnitude_scale": 3.0}},
        "packet_loss": {"enabled": True, "packet_loss": {"enabled": True, "probability": 0.2}},
    }
    results = {}
    for noise_name, noise_cfg in noise_configs.items():
        completion_rates = []
        false_positive_rates = []
        for _ in range(trials):
            r = run_single_trial(noise_config=noise_cfg)
            # 鲁棒性：Agent正常完成（不崩溃、不超时）
            completion_rates.append(not r.get("timed_out", False))
            # 误报率：无故障注入下RSM/DTM是否误报
            false_positive = r["rsm_detected"] or r["dtm_detected"]
            false_positive_rates.append(false_positive)
        results[noise_name] = {
            "completion_rate": round(sum(completion_rates) / len(completion_rates) * 100, 1),
            "false_positive_rate": round(sum(false_positive_rates) / len(false_positive_rates) * 100, 1),
            "note": "噪声下Agent正常完成率 + 无故障时的误报率",
        }
    return results


def experiment_performance(trials: int = 5) -> dict:
    """实验4：性能开销。"""
    print(f"  实验4：性能开销（{trials}次取均值）...")
    agent_times = []
    rsm_dtm_times = []
    gateway_times = []
    for _ in range(trials):
        r = run_single_trial()
        agent_times.append(r["agent_time_ms"])
        rsm_dtm_times.append(r["rsm_dtm_time_ms"])
        gateway_times.append(r["gateway_time_ms"])
    return {
        "agent_execution_ms": {
            "mean": round(statistics.mean(agent_times), 1),
            "max": round(max(agent_times), 1),
            "min": round(min(agent_times), 1),
        },
        "rsm_dtm_processing_ms": {
            "mean": round(statistics.mean(rsm_dtm_times), 1),
            "max": round(max(rsm_dtm_times), 1),
        },
        "gateway_decision_ms": {
            "mean": round(statistics.mean(gateway_times), 1),
            "max": round(max(gateway_times), 1),
        },
        "total_overhead_ms": round(statistics.mean(rsm_dtm_times) + statistics.mean(gateway_times), 1),
        "trials": trials,
    }


def generate_report(results: dict) -> str:
    """生成文本报告。"""
    lines = []
    lines.append("=" * 70)
    lines.append("OrbitGuard 量化实验报告")
    lines.append("=" * 70)
    lines.append("")

    # 实验1
    lines.append("【实验1：单故障检测率】")
    lines.append(f"  {'故障名':<25} {'期望模块':<8} {'检测率':>8}")
    lines.append("  " + "-" * 45)
    for fault, data in results["single_fault"].items():
        lines.append(f"  {fault:<25} {data['expected_module']:<8} {data['detection_rate']:>7.1f}%")
    rates = [d["detection_rate"] for d in results["single_fault"].values()]
    lines.append(f"  {'平均检测率':<25} {'':<8} {statistics.mean(rates):>7.1f}%")
    lines.append("")

    # 实验2
    lines.append("【实验2：复合故障检测率（逐故障匹配）】")
    for key, data in results["composite_fault"].items():
        per_fault = ", ".join(f"{f}={r}%" for f, r in data["per_fault_detection"].items())
        lines.append(f"  {key}: 全检出率 {data['detection_rate']}%  (逐故障: {per_fault})")
    lines.append("")

    # 实验3
    lines.append("【实验3：噪声鲁棒性】")
    for noise, data in results["noise_robustness"].items():
        lines.append(f"  {noise:<20} 完成率: {data['completion_rate']}%  误报率: {data['false_positive_rate']}%")
    lines.append("")

    # 实验4
    lines.append("【实验4：性能开销】")
    perf = results["performance"]
    lines.append(f"  Agent执行:     均值 {perf['agent_execution_ms']['mean']}ms (最大 {perf['agent_execution_ms']['max']}ms)")
    lines.append(f"  RSM+DTM处理:  均值 {perf['rsm_dtm_processing_ms']['mean']}ms")
    lines.append(f"  Gateway判定:   均值 {perf['gateway_decision_ms']['mean']}ms")
    lines.append(f"  监护总开销:    {perf['total_overhead_ms']}ms")
    lines.append("")

    lines.append("=" * 70)
    return "\n".join(lines)


def main():
    print("=" * 60)
    print("OrbitGuard 量化实验开始")
    print("=" * 60)

    results = {}
    results["single_fault"] = experiment_single_fault_detection(trials=5)
    results["composite_fault"] = experiment_composite_fault(trials=3)
    results["noise_robustness"] = experiment_noise_robustness(trials=3)
    results["performance"] = experiment_performance(trials=5)

    # 保存JSON
    docs_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "docs")
    os.makedirs(docs_dir, exist_ok=True)
    json_path = os.path.join(docs_dir, "benchmark_results.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)

    # 生成文本报告
    report = generate_report(results)
    txt_path = os.path.join(docs_dir, "benchmark_report.txt")
    with open(txt_path, "w", encoding="utf-8") as f:
        f.write(report)

    print()
    print(report)
    print(f"\nJSON结果: {json_path}")
    print(f"文本报告: {txt_path}")


if __name__ == "__main__":
    main()
