# -*- coding: utf-8 -*-
"""
故障注入器核心。
采用装饰器 + 配置驱动的非侵入式架构，不修改 Agent 核心逻辑。

设计原则：
1. 非侵入式：通过装饰器在函数调用前后注入故障，不修改原函数代码
2. 配置驱动：所有故障通过 config.json 开关，改配置即可切换正常/故障模式
3. 可观测：每次故障注入都记录 ground truth 日志，供后续验证
4. 分层：执行层注入器处理稳定性故障，数据层注入器处理可信性故障
"""
import time
import json
import hashlib
import functools
import threading
from typing import Dict, Any, Callable, Optional, List
from enum import Enum


class FaultLayer(Enum):
    """故障注入层。"""
    EXECUTION = "execution"      # 执行层：修改函数行为（稳定性故障）
    DATA = "data"                # 数据层：修改输入/输出数据（可信性故障）


class FaultType(Enum):
    """故障类型。"""
    # 稳定性故障
    INFINITE_LOOP = "infinite_loop"
    PROCESS_CRASH = "process_crash"
    NO_OUTPUT_TIMEOUT = "no_output_timeout"
    MEMORY_BLOAT = "memory_bloat"
    REPETITIVE_CALLS = "repetitive_calls"
    # 可信性故障
    WRONG_DIAGNOSIS = "wrong_diagnosis"
    INFLATED_CONFIDENCE = "inflated_confidence"
    MISSING_EVIDENCE = "missing_evidence"
    CONTRADICTORY_SENSORS = "contradictory_sensors"


# 故障间关系定义
# terminal faults（终结性故障）：触发后函数不会正常返回，与其他执行层故障互斥
TERMINAL_FAULTS = {"infinite_loop", "process_crash"}
# non-terminal faults（非终结性故障）：触发后函数可能继续执行，可叠加
NON_TERMINAL_FAULTS = {"no_output_timeout", "memory_bloat"}
# 执行层稳定性故障全集
EXECUTION_FAULTS = TERMINAL_FAULTS | NON_TERMINAL_FAULTS
# 可信性故障全集（数据层，独立注入点，天然可叠加）
CREDIBILITY_FAULTS = {"wrong_diagnosis", "inflated_confidence", "missing_evidence", "contradictory_sensors"}

# 故障强度分级预设（阶段一1.3）
# 每个故障三档：light（轻度）/ medium（中度）/ heavy（重度）
INTENSITY_PRESETS = {
    "memory_bloat": {
        "light":  {"alloc_mb_per_step": 10, "max_alloc_mb": 100},
        "medium": {"alloc_mb_per_step": 50, "max_alloc_mb": 400},
        "heavy":  {"alloc_mb_per_step": 200, "max_alloc_mb": 1000},
    },
    "no_output_timeout": {
        "light":  {"sleep_seconds": 10},
        "medium": {"sleep_seconds": 60},
        "heavy":  {"sleep_seconds": 300},
    },
    "infinite_loop": {
        "light":  {"auto_recover_after_s": 5},
        "medium": {},
        "heavy":  {"cpu_burn": True},
    },
    "process_crash": {
        "light":  {"mode": "exception"},
        "medium": {"mode": "exception"},
        "heavy":  {"mode": "exit"},
    },
    "repetitive_calls": {
        "light":  {"repeat_count": 5},
        "medium": {"repeat_count": 8},
        "heavy":  {"repeat_count": 12},
    },
    "wrong_diagnosis": {
        "light":  {"wrong_fault_code": "SA_BRANCH_SHORT", "correct_fault_code": "NO_FAULT"},
        "medium": {"wrong_fault_code": "SA_BRANCH_SHORT", "correct_fault_code": None},
        "heavy":  {"wrong_fault_code": "BCR_OPEN", "correct_fault_code": None},
    },
    "inflated_confidence": {
        "light":  {"confidence_range": [0.70, 0.80]},
        "medium": {"confidence_range": [0.85, 0.95]},
        "heavy":  {"confidence_range": [0.95, 0.99]},
    },
    "missing_evidence": {
        "light":  {"keep_first_n": 1},
        "medium": {"keep_first_n": 0},
        "heavy":  {"keep_first_n": 0, "clear_conflicting": True},
    },
    "contradictory_sensors": {
        "light":  {"faulty_sensors": {"bus_current": 0.0}},
        "medium": {"faulty_sensors": {"bus_current": 0.0, "sa_current": 0.0}},
        "heavy":  {"faulty_sensors": {"bus_current": 0.0, "sa_current": 0.0, "battery_voltage": 0.0}},
    },
}


class GroundTruthLogger:
    """
    Ground truth 日志记录器。
    记录每次故障注入的详细信息，不通过 Redis 发布，仅写入本地文件。
    监护器在运行时无法获取这些信息，保证评估的公正性。
    """

    def __init__(self, log_dir: str = "logs", log_file: str = "fault_injector_log.jsonl"):
        import os
        self.log_dir = log_dir
        self.log_file = log_file
        self.log_path = os.path.join(log_dir, log_file)
        os.makedirs(log_dir, exist_ok=True)
        self._lock = threading.Lock()
        self._records: List[Dict] = []

    def log(self, fault_type: str, target: str, trigger_step: int,
            details: Dict[str, Any], expected_behavior: str):
        """记录一次故障注入。"""
        record = {
            "injection_id": f"INJ-{hashlib.md5(str(time.time()).encode()).hexdigest()[:10]}",
            "timestamp_ms": int(time.time() * 1000),
            "fault_type": fault_type,
            "target": target,
            "trigger_step": trigger_step,
            "details": details,
            "expected_behavior": expected_behavior,
        }
        with self._lock:
            self._records.append(record)
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        return record

    def get_records(self) -> List[Dict]:
        with self._lock:
            return list(self._records)

    def clear(self):
        with self._lock:
            self._records.clear()


class FaultInjector:
    """
    故障注入器（门面类）。
    统一管理执行层和数据层注入器，对外提供一致的 activate/deactivate 接口。
    """

    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.fi_config = config.get("fault_injector", {})
        self.global_enabled = self.fi_config.get("global_enabled", False)
        self.gt_logger = GroundTruthLogger(
            log_dir=config.get("ground_truth", {}).get("log_dir", "logs"),
            log_file=config.get("ground_truth", {}).get("log_file", "fault_injector_log.jsonl"),
        )
        self._step_counter = 0
        self._lock = threading.Lock()
        self._active_faults: Dict[str, bool] = {}
        self._memory_bloat_data = []  # 用于内存暴涨故障
        # 应用强度分级预设（阶段一1.3）
        self._apply_intensity_presets()

    def _apply_intensity_presets(self):
        """
        根据每个故障配置中的 intensity 字段，自动应用预设参数。
        intensity 可选值：light / medium / heavy
        如果配置中已显式指定了参数，则保留显式值（不覆盖）。
        """
        for layer_key in ["stability_faults", "credibility_faults"]:
            faults = self.fi_config.get(layer_key, {})
            for fault_name, fault_cfg in faults.items():
                if not isinstance(fault_cfg, dict):
                    continue
                intensity = fault_cfg.get("intensity")
                if intensity and fault_name in INTENSITY_PRESETS:
                    preset = INTENSITY_PRESETS[fault_name].get(intensity, {})
                    # 只填充未显式指定的参数
                    for key, value in preset.items():
                        if key not in fault_cfg:
                            fault_cfg[key] = value

    def get_intensity(self, fault_name: str, layer: str = "stability") -> Optional[str]:
        """获取故障的强度等级（light/medium/heavy），未设置返回 None。"""
        cfg = self.get_fault_config(fault_name, layer)
        return cfg.get("intensity")

    def increment_step(self):
        """递增任务步骤计数器（用于按步骤触发故障）。
        如果 memory_bloat 故障启用，每个 step 阶梯式分配内存，形成持续增长。"""
        with self._lock:
            self._step_counter += 1
        # memory_bloat 阶梯式分配：每个 step 分配 alloc_mb，直到 max_mb 上限
        if self.is_fault_enabled("memory_bloat", "stability"):
            cfg = self.get_fault_config("memory_bloat", "stability")
            alloc_mb = cfg.get("alloc_mb_per_step", 50)
            max_mb = cfg.get("max_alloc_mb", 400)
            current_mb = len(self._memory_bloat_data) * alloc_mb
            if current_mb < max_mb:
                chunk = bytearray(alloc_mb * 1024 * 1024)
                self._memory_bloat_data.append(chunk)
        return self._step_counter

    def get_step(self) -> int:
        with self._lock:
            return self._step_counter

    def is_fault_enabled(self, fault_name: str, layer: str = "stability") -> bool:
        """检查某个故障是否启用。"""
        if not self.global_enabled:
            return False
        layer_key = "stability_faults" if layer == "stability" else "credibility_faults"
        faults = self.fi_config.get(layer_key, {})
        fault_cfg = faults.get(fault_name, {})
        return fault_cfg.get("enabled", False)

    def get_fault_config(self, fault_name: str, layer: str = "stability") -> Dict:
        layer_key = "stability_faults" if layer == "stability" else "credibility_faults"
        return self.fi_config.get(layer_key, {}).get(fault_name, {})

    def should_trigger(self, fault_name: str, layer: str = "stability") -> bool:
        """
        判断故障是否应该在当前步骤触发。
        如果配置了 trigger_step，则在该步骤触发一次；否则每次调用都触发。
        """
        if not self.is_fault_enabled(fault_name, layer):
            return False
        cfg = self.get_fault_config(fault_name, layer)
        trigger_step = cfg.get("trigger_step")
        if trigger_step is not None:
            return self.get_step() >= trigger_step and fault_name not in self._active_faults
        return True

    def mark_active(self, fault_name: str):
        self._active_faults[fault_name] = True

    def get_active_faults(self) -> List[str]:
        """获取当前已触发的活跃故障列表。"""
        with self._lock:
            return [k for k, v in self._active_faults.items() if v]

    def get_enabled_execution_faults(self) -> List[str]:
        """获取所有已启用的执行层稳定性故障。"""
        if not self.global_enabled:
            return []
        stab = self.fi_config.get("stability_faults", {})
        return [name for name in EXECUTION_FAULTS if stab.get(name, {}).get("enabled", False)]

    def get_enabled_credibility_faults(self) -> List[str]:
        """获取所有已启用的可信性故障。"""
        if not self.global_enabled:
            return []
        cred = self.fi_config.get("credibility_faults", {})
        return [name for name in CREDIBILITY_FAULTS if cred.get(name, {}).get("enabled", False)]

    def get_all_enabled_faults(self) -> Dict[str, List[str]]:
        """获取所有已启用故障，按层分组。"""
        return {
            "execution": self.get_enabled_execution_faults(),
            "credibility": self.get_enabled_credibility_faults(),
            "repetitive_calls": ["repetitive_calls"] if self.is_fault_enabled("repetitive_calls", "stability") else [],
        }

    def get_triggered_execution_faults(self) -> Dict[str, List[str]]:
        """
        获取当前步骤应触发的执行层故障，按终结性/非终结性分组。
        终结性故障最多一个（互斥），非终结性故障可多个叠加。
        """
        terminal = []
        non_terminal = []
        for name in self.get_enabled_execution_faults():
            if self.should_trigger(name, "stability"):
                if name in TERMINAL_FAULTS:
                    terminal.append(name)
                else:
                    non_terminal.append(name)
        return {"terminal": terminal[:1], "non_terminal": non_terminal}

    # ===== 装饰器：执行层故障注入 =====

    def inject_execution(self, fault_name: str):
        """
        执行层故障注入装饰器。
        包裹工具执行函数，在调用前后注入稳定性故障。
        """
        def decorator(func: Callable) -> Callable:
            @functools.wraps(func)
            def wrapper(*args, **kwargs):
                if not self.should_trigger(fault_name, "stability"):
                    return func(*args, **kwargs)

                self.mark_active(fault_name)
                cfg = self.get_fault_config(fault_name, "stability")

                # ---- 死循环/活锁 ----
                if fault_name == "infinite_loop":
                    self.gt_logger.log(
                        fault_type="infinite_loop",
                        target=func.__name__,
                        trigger_step=self.get_step(),
                        details={"mode": "independent_thread_livelock"},
                        expected_behavior="进程存活、心跳正常、但任务无进展（活锁）",
                    )
                    # 在独立线程中死循环，主线程继续（模拟活锁）
                    stop_event = threading.Event()
                    def _loop():
                        while not stop_event.is_set():
                            pass  # 忙等死循环
                    t = threading.Thread(target=_loop, daemon=True)
                    t.start()
                    # 主线程不返回结果，模拟卡住
                    # 注意：这里我们让函数不返回，调用方会超时
                    stop_event.wait(timeout=3600)  # 最长等1小时，模拟永久卡住
                    return None

                # ---- 进程崩溃 ----
                if fault_name == "process_crash":
                    mode = cfg.get("mode", "exception")
                    self.gt_logger.log(
                        fault_type="process_crash",
                        target=func.__name__,
                        trigger_step=self.get_step(),
                        details={"mode": mode},
                        expected_behavior="进程崩溃（未捕获异常或直接退出），心跳停止",
                    )
                    if mode == "exit":
                        import os
                        os._exit(1)
                    else:
                        raise RuntimeError("Simulated process crash: unhandled exception in fault injection")

                # ---- 无输出/超时 ----
                if fault_name == "no_output_timeout":
                    sleep_sec = cfg.get("sleep_seconds", 120)
                    self.gt_logger.log(
                        fault_type="no_output_timeout",
                        target=func.__name__,
                        trigger_step=self.get_step(),
                        details={"sleep_seconds": sleep_sec},
                        expected_behavior=f"函数卡住{sleep_sec}秒不返回，调用方超时",
                    )
                    time.sleep(sleep_sec)
                    return func(*args, **kwargs)

                # ---- 内存暴涨 ----
                if fault_name == "memory_bloat":
                    alloc_mb = cfg.get("alloc_mb_per_step", 50)
                    max_mb = cfg.get("max_alloc_mb", 400)
                    self.gt_logger.log(
                        fault_type="memory_bloat",
                        target=func.__name__,
                        trigger_step=self.get_step(),
                        details={"alloc_mb_per_step": alloc_mb, "max_alloc_mb": max_mb},
                        expected_behavior=f"内存阶梯式增长，上限{max_mb}MB，不释放",
                    )
                    current_mb = len(self._memory_bloat_data) * alloc_mb
                    if current_mb < max_mb:
                        # 分配约 alloc_mb MB 数据（1MB ~ 1024*1024 字节）
                        chunk = bytearray(alloc_mb * 1024 * 1024)
                        self._memory_bloat_data.append(chunk)
                    return func(*args, **kwargs)

                # ---- 重复调用循环（在决策循环层处理，这里透传） ----
                return func(*args, **kwargs)

            return wrapper
        return decorator

    # ===== 数据层：可信性故障注入 =====

    def inject_diagnosis_output(self, diagnosis_result: List[Dict]) -> List[Dict]:
        """
        在诊断输出层注入可信性故障。
        在 DiagnosisEngine 返回结果后、发布到 Redis 前调用。
        """
        if not self.global_enabled:
            return diagnosis_result

        cred_faults = self.fi_config.get("credibility_faults", {})

        # ---- 诊断结论错误 ----
        if cred_faults.get("wrong_diagnosis", {}).get("enabled", False):
            cfg = cred_faults["wrong_diagnosis"]
            wrong_code = cfg.get("wrong_fault_code", "SA_BRANCH_SHORT")
            correct_code = cfg.get("correct_fault_code")
            for fault in diagnosis_result:
                if correct_code is None or fault.get("fault_code") == correct_code:
                    original = fault["fault_code"]
                    fault["fault_code"] = wrong_code
                    fault["description"] = f"[INJECTED] 原诊断={original}, 注入错误结论={wrong_code}"
                    self.gt_logger.log(
                        fault_type="wrong_diagnosis",
                        target="diagnosis_output",
                        trigger_step=self.get_step(),
                        details={"original": original, "injected": wrong_code},
                        expected_behavior="诊断结论错误，但证据字段仍指向原故障",
                    )
                    break

        # ---- 置信度虚高 ----
        if cred_faults.get("inflated_confidence", {}).get("enabled", False):
            cfg = cred_faults["inflated_confidence"]
            conf_range = cfg.get("confidence_range", [0.95, 0.99])
            import random
            target_fault = None
            for fault in diagnosis_result:
                if fault.get("fault_code") != "NO_FAULT":
                    target_fault = fault
                    break
            # 如果诊断全是NO_FAULT，先注入一个假故障结论（确保DTM能检测到置信度虚高）
            if target_fault is None and diagnosis_result:
                target_fault = diagnosis_result[0]
                target_fault["fault_code"] = "SA_BRANCH_SHORT"
                target_fault["description"] = "[INJECTED] 原诊断=NO_FAULT, 注入假故障结论=SA_BRANCH_SHORT"
                target_fault["severity"] = "medium"
            if target_fault is not None:
                original_conf = target_fault.get("confidence", 0.5)
                original_evidence = target_fault.get("supporting_evidence", [])
                target_fault["confidence"] = round(random.uniform(*conf_range), 4)
                # 保留1条证据（DTM阈值<2条触发虚高检测）
                if len(original_evidence) > 1:
                    target_fault["supporting_evidence"] = original_evidence[:1]
                self.gt_logger.log(
                    fault_type="inflated_confidence",
                    target="diagnosis_output",
                    trigger_step=self.get_step(),
                    details={"original_confidence": original_conf,
                             "injected_confidence": target_fault["confidence"],
                             "evidence_count": len(target_fault.get("supporting_evidence", []))},
                    expected_behavior="高置信度(0.95-0.99)但证据不足(<=1条)，DTM应检测到置信度虚高",
                )

        # ---- 证据缺失 ----
        if cred_faults.get("missing_evidence", {}).get("enabled", False):
            cfg = cred_faults["missing_evidence"]
            keep_first_n = cfg.get("keep_first_n", 0)
            clear_conflicting = cfg.get("clear_conflicting", False)
            target_fault = None
            for fault in diagnosis_result:
                if fault.get("fault_code") != "NO_FAULT":
                    target_fault = fault
                    break
            # 如果诊断全是NO_FAULT，先注入一个假故障结论
            if target_fault is None and diagnosis_result:
                target_fault = diagnosis_result[0]
                target_fault["fault_code"] = "SA_BRANCH_SHORT"
                target_fault["description"] = "[INJECTED] 原诊断=NO_FAULT, 注入假故障结论=SA_BRANCH_SHORT"
                target_fault["severity"] = "medium"
            if target_fault is not None:
                original_evidence = target_fault.get("supporting_evidence", [])
                if keep_first_n > 0:
                    target_fault["supporting_evidence"] = original_evidence[:keep_first_n]
                else:
                    target_fault["supporting_evidence"] = []
                if clear_conflicting:
                    target_fault["conflicting_evidence"] = []
                self.gt_logger.log(
                    fault_type="missing_evidence",
                    target="diagnosis_output",
                    trigger_step=self.get_step(),
                    details={
                        "original_evidence_count": len(original_evidence),
                        "injected_count": len(target_fault.get("supporting_evidence", [])),
                        "keep_first_n": keep_first_n,
                        "clear_conflicting": clear_conflicting,
                    },
                    expected_behavior="有结论但支持证据不足（保留%d条），DTM应检测到证据缺失" % keep_first_n,
                )

        return diagnosis_result

    def apply_telemetry_faults(self, telemetry_source):
        """
        应用遥测层故障（多源数据矛盾）。
        在 Agent 初始化时调用，设置故障传感器。
        """
        if not self.global_enabled:
            return
        cred_faults = self.fi_config.get("credibility_faults", {})
        contradictory = cred_faults.get("contradictory_sensors", {})
        if contradictory.get("enabled", False):
            faulty = contradictory.get("faulty_sensors", {})
            telemetry_source.set_faulty_sensors(faulty)
            self.gt_logger.log(
                fault_type="contradictory_sensors",
                target="telemetry_source",
                trigger_step=0,
                details={"faulty_sensors": faulty},
                expected_behavior="指定传感器输出异常值，与其他传感器冲突",
            )
