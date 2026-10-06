# -*- coding: utf-8 -*-
"""
RSM 主监护器。

统一管理5个检测器，消费事件流，输出统一的 RSMResult。
支持在线模式（逐事件 process_event）和离线模式（批量 process_events）。
"""
import time
import json
import threading
from typing import Dict, Any, Optional, List
from .detectors import (
    BaseDetector,
    DetectionResult,
    HeartbeatMonitor,
    StallMonitor,
    ToolTimeoutMonitor,
    MemoryMonitor,
    RepetitiveCallMonitor,
)


class RSMResult:
    """
    RSM 统一输出结果。
    供 Gateway 汇合判定使用。
    """
    def __init__(self, run_id: str = "", task_id: str = "", sat_id: str = ""):
        self.monitor_type = "RSM"
        self.run_id = run_id
        self.task_id = task_id
        self.sat_id = sat_id
        self.fault_detected = False
        self.fault_type = ""
        self.severity = "low"  # low / medium / high / critical
        self.confidence = 0.0
        self.issues: List[Dict] = []  # 所有检测到的问题
        self.detector_states: Dict[str, Dict] = {}
        self.timestamp_ms = int(time.time() * 1000)

    def add_issue(self, result: DetectionResult, detector_name: str):
        """添加一个检测到的问题。"""
        # 同一检测器对同一故障类型的重复上报去重（如监护超时与执行方回报超时的叠加）
        for existing in self.issues:
            if existing.get("detector") == detector_name and existing.get("fault_type") == result.fault_type:
                return
        self.issues.append({
            "detector": detector_name,
            "fault_type": result.fault_type,
            "severity": result.severity,
            "confidence": result.confidence,
            "evidence": result.evidence,
            "description": result.description,
        })
        # 更新主故障（取最严重的）
        severity_order = {"low": 0, "medium": 1, "high": 2, "critical": 3}
        if not self.fault_detected or severity_order.get(result.severity, 0) > severity_order.get(self.severity, 0):
            self.fault_detected = True
            self.fault_type = result.fault_type
            self.severity = result.severity
            self.confidence = result.confidence

    def to_dict(self) -> Dict[str, Any]:
        return {
            "monitor_type": self.monitor_type,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "sat_id": self.sat_id,
            "fault_detected": self.fault_detected,
            "fault_type": self.fault_type,
            "severity": self.severity,
            "confidence": self.confidence,
            "issues": self.issues,
            "detector_states": self.detector_states,
            "timestamp_ms": self.timestamp_ms,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)


class RuntimeStabilityMonitor:
    """
    运行稳定性监护器（RSM）。

    统一管理5个检测器：
    - HeartbeatMonitor：心跳检测（进程崩溃）
    - StallMonitor：无进展检测（死循环/活锁）
    - ToolTimeoutMonitor：工具超时检测
    - MemoryMonitor：内存泄漏检测
    - RepetitiveCallMonitor：重复调用检测

    使用方式：
        rsm = RuntimeStabilityMonitor(config)
        for event in event_stream:
            result = rsm.process_event(event)
            if result and result.fault_detected:
                # 处理异常
                pass
    """

    def __init__(self, config: Optional[Dict] = None):
        self.config = config or {}
        rsm_config = self.config.get("rsm", {})

        # 初始化5个检测器
        self.detectors: Dict[str, BaseDetector] = {
            "heartbeat": HeartbeatMonitor(rsm_config.get("heartbeat", {})),
            "stall": StallMonitor(rsm_config.get("stall", {})),
            "tool_timeout": ToolTimeoutMonitor(rsm_config.get("tool_timeout", {})),
            "memory": MemoryMonitor(rsm_config.get("memory", {})),
            "repetitive_calls": RepetitiveCallMonitor(rsm_config.get("repetitive_calls", {})),
        }

        self._run_id = ""
        self._task_id = ""
        self._sat_id = ""
        self._event_count = 0
        self._last_result: Optional[RSMResult] = None
        self._cumulative_result: Optional[RSMResult] = None  # 累积式结果（P2修复）
        self._agent_shutdown = False  # P1修复：Agent正常shutdown标志，看门狗看到后不判崩溃
        self._last_cpu_percent = 0.0  # P1修复：最近一次CPU采样，用于区分忙等死循环与无输出挂起
        self._cpu_discriminator_threshold = 50.0  # CPU判别阈值：>=50% 判忙等死循环，否则判无输出超时

        # 独立看门狗（墙钟驱动，P1架构修复）
        self._watchdog_thread: Optional[threading.Thread] = None
        self._watchdog_stop = threading.Event()
        self._watchdog_result: Optional[RSMResult] = None
        self._watchdog_lock = threading.Lock()

    def set_context(self, run_id: str = "", task_id: str = "", sat_id: str = ""):
        """设置运行上下文（从第一个事件中提取）。"""
        if run_id:
            self._run_id = run_id
        if task_id:
            self._task_id = task_id
        if sat_id:
            self._sat_id = sat_id

    def process_event(self, event: Dict[str, Any]) -> Optional[RSMResult]:
        """
        处理一个事件，返回 RSMResult。
        如果没有检测到异常，返回 None（或返回 fault_detected=False 的结果）。
        """
        self._event_count += 1

        # 从事件中提取上下文
        if not self._run_id and "run_id" in event:
            self._run_id = event["run_id"]
        if not self._task_id and "task_id" in event:
            self._task_id = event["task_id"]
        if not self._sat_id and "sat_id" in event:
            self._sat_id = event["sat_id"]

        # P1修复：识别Agent正常shutdown事件，看门狗看到后不判崩溃
        # 异常路径的 shutdown（reason=agent_fault）不设置该标志，看门狗仍按崩溃判定
        if event.get("event_type") == "agent_shutdown":
            reason = event.get("reason")
            if reason in (None, "", "normal_shutdown"):
                self._agent_shutdown = True

        # P1修复：记录最近一次CPU采样（resource事件），供看门狗区分忙等/挂起
        if event.get("event_type") == "resource":
            try:
                self._last_cpu_percent = float(event.get("cpu_percent", 0.0) or 0.0)
            except (TypeError, ValueError):
                self._last_cpu_percent = 0.0

        # 累积式结果：持续追加所有检测到的问题，不被后续无异常事件覆盖
        if self._cumulative_result is None:
            self._cumulative_result = RSMResult(
                run_id=self._run_id, task_id=self._task_id, sat_id=self._sat_id,
            )

        any_detected = False
        tool_mon = self.detectors["tool_timeout"]
        for name, detector in self.detectors.items():
            detection = detector.process_event(event)
            # P1修复：Agent已正常停机后，迟到事件（收尾的tool_result等）不再触发崩溃判定
            if name == "heartbeat" and detection and detection.fault_detected \
                    and self._agent_shutdown:
                detection = None
            # P1修复（no_output_timeout 独立判别）：
            # 存在未决工具调用、或工具挂起已被归因时，忽略停滞判定——挂起因在工具，不在Agent
            if name == "stall" and detection and detection.fault_detected:
                if tool_mon.pending_calls or tool_mon.attributed:
                    detection = None
            if detection and detection.fault_detected:
                self._cumulative_result.add_issue(detection, name)
                any_detected = True
                if name == "tool_timeout":
                    tool_mon.attributed = True
            self._cumulative_result.detector_states[name] = detector.get_state()

        self._cumulative_result.timestamp_ms = int(time.time() * 1000)
        self._last_result = self._cumulative_result
        return self._cumulative_result

    def process_events(self, events: List[Dict[str, Any]]) -> List[RSMResult]:
        """批量处理事件流，返回所有检测到异常的结果。"""
        results = []
        for event in events:
            result = self.process_event(event)
            if result and result.fault_detected:
                results.append(result)
        return results

    def get_last_result(self) -> Optional[RSMResult]:
        """获取最近一次检测结果。优先返回看门狗结果（墙钟驱动，更及时）。"""
        with self._watchdog_lock:
            if self._watchdog_result is not None:
                return self._watchdog_result
        return self._last_result

    def get_summary(self) -> Dict[str, Any]:
        """获取RSM运行摘要。"""
        return {
            "events_processed": self._event_count,
            "run_id": self._run_id,
            "task_id": self._task_id,
            "detectors": {name: d.get_state() for name, d in self.detectors.items()},
            "last_result": self._last_result.to_dict() if self._last_result else None,
        }

    def start_watchdog(self, check_interval_s: float = 0.1):
        """
        启动独立看门狗线程（墙钟驱动超时检测）。
        解决事件驱动模式下 Agent 崩溃后无事件可看、超时判定永不触发的问题。
        """
        if self._watchdog_thread and self._watchdog_thread.is_alive():
            return  # 已在运行
        self._watchdog_stop.clear()
        self._watchdog_thread = threading.Thread(
            target=self._watchdog_loop,
            args=(check_interval_s,),
            daemon=True,
            name="RSM-Watchdog",
        )
        self._watchdog_thread.start()

    def stop_watchdog(self):
        """停止看门狗线程。"""
        self._watchdog_stop.set()
        if self._watchdog_thread:
            self._watchdog_thread.join(timeout=2.0)
            self._watchdog_thread = None

    def _watchdog_loop(self, check_interval_s: float):
        """看门狗主循环：用墙钟推进超时判定，不依赖事件流。"""
        while not self._watchdog_stop.is_set():
            try:
                now_ms = int(time.time() * 1000)
                detected = None
                detector_name = ""

                # 1. 心跳超时检查（进程崩溃）
                # P1修复：如果Agent正常shutdown，不判崩溃
                hb = self.detectors["heartbeat"]
                if (hb.last_heartbeat_ms is not None
                        and not hb._crash_detected
                        and not self._agent_shutdown):
                    gap_s = (now_ms - hb.last_heartbeat_ms) / 1000.0
                    if gap_s > hb.timeout_s:
                        hb._crash_detected = True
                        detected = DetectionResult(
                            fault_detected=True,
                            fault_type="PROCESS_CRASH",
                            severity="critical",
                            confidence=0.95,
                            evidence={"last_heartbeat_ms": hb.last_heartbeat_ms,
                                      "gap_s": round(gap_s, 2),
                                      "timeout_s": hb.timeout_s,
                                      "detection_mode": "watchdog"},
                            description=f"看门狗检测：超过{hb.timeout_s}s无心跳，判定进程崩溃",
                        )
                        detector_name = "heartbeat"

                # 2. 工具超时检查（优先于停滞判定：存在未决工具调用时，先归因于"工具挂起"）
                # P1修复（no_output_timeout 独立判别）：
                #   用最近CPU采样区分两类挂起——
                #     高CPU（忙等）   -> INFINITE_LOOP（活锁在烧CPU）
                #     低CPU（阻塞）   -> NO_OUTPUT_TIMEOUT（工具卡住无输出）
                tool_mon = self.detectors["tool_timeout"]
                if detected is None:
                    for call_id, info in list(tool_mon.pending_calls.items()):
                        duration_s = (now_ms - info["call_time_ms"]) / 1000.0
                        if duration_s > tool_mon.timeout_s:
                            del tool_mon.pending_calls[call_id]
                            cpu = self._last_cpu_percent
                            if cpu >= self._cpu_discriminator_threshold:
                                fault_type = "INFINITE_LOOP"
                                severity = "high"
                                confidence = 0.88
                                desc = (f"看门狗检测：工具{info['tool_name']}挂起且CPU占用{cpu:.0f}%"
                                        f"（忙等特征），判定死循环")
                            else:
                                fault_type = "NO_OUTPUT_TIMEOUT"
                                severity = "medium"
                                confidence = 0.92
                                desc = (f"看门狗检测：工具{info['tool_name']}挂起且CPU占用{cpu:.0f}%"
                                        f"（阻塞特征），判定无输出超时")
                            detected = DetectionResult(
                                fault_detected=True,
                                fault_type=fault_type,
                                severity=severity,
                                confidence=confidence,
                                evidence={"call_id": call_id,
                                          "tool_name": info["tool_name"],
                                          "duration_s": round(duration_s, 2),
                                          "cpu_percent": round(cpu, 1),
                                          "detection_mode": "watchdog"},
                                description=desc,
                            )
                            detector_name = "tool_timeout"
                            tool_mon.attributed = True
                            break

                # 3. 停滞超时检查（仅当无未决工具调用且工具挂起未被归因：
                #    否则停滞是工具挂起的后果，而非Agent死循环）
                if detected is None and not self._agent_shutdown \
                        and not tool_mon.pending_calls and not tool_mon.attributed:
                    stall = self.detectors["stall"]
                    if (stall.last_step_time_ms is not None
                            and not stall._stall_detected
                            and stall.current_uptime_s > 0):
                        stall_duration_s = (now_ms - stall.last_step_time_ms) / 1000.0
                        if stall_duration_s > stall.stall_timeout_s:
                            stall._stall_detected = True
                            detected = DetectionResult(
                                fault_detected=True,
                                fault_type="INFINITE_LOOP",
                                severity="high",
                                confidence=0.88,
                                evidence={"last_step": stall.last_step,
                                          "stall_duration_s": round(stall_duration_s, 2),
                                          "detection_mode": "watchdog"},
                                description=f"看门狗检测：任务步骤停滞{round(stall_duration_s, 1)}s，判定死循环",
                            )
                            detector_name = "stall"

                # 检测到故障，保存结果（只保留第一次/最严重的，不被后续较轻故障覆盖）
                if detected:
                    result = RSMResult(
                        run_id=self._run_id,
                        task_id=self._task_id,
                        sat_id=self._sat_id,
                    )
                    result.add_issue(detected, detector_name)
                    with self._watchdog_lock:
                        if self._watchdog_result is None:
                            self._watchdog_result = result
                    # 同时合并到累积结果（P2修复：看门狗故障不丢失）
                    if self._cumulative_result is not None:
                        self._cumulative_result.add_issue(detected, detector_name)
                        self._last_result = self._cumulative_result

            except Exception:
                pass  # 看门狗内部异常不影响主流程

            self._watchdog_stop.wait(check_interval_s)

    def get_watchdog_result(self) -> Optional[RSMResult]:
        """获取看门狗检测到的结果。"""
        with self._watchdog_lock:
            return self._watchdog_result

    def reset(self):
        """重置所有检测器状态。"""
        self.stop_watchdog()
        for detector in self.detectors.values():
            detector.reset()
        self._event_count = 0
        self._last_result = None
        self._cumulative_result = None
        self._agent_shutdown = False
        self._last_cpu_percent = 0.0
        with self._watchdog_lock:
            self._watchdog_result = None
