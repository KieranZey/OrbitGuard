# -*- coding: utf-8 -*-
"""
RSM 检测器集合。

每个检测器独立维护内部状态，消费事件，输出检测结果。
基类 BaseDetector 定义统一接口。
"""
import time
from typing import Dict, Any, Optional, List
from abc import ABC, abstractmethod


class DetectionResult:
    """单个检测器的检测结果。"""
    def __init__(self, fault_detected: bool, fault_type: str = "",
                 severity: str = "low", confidence: float = 0.0,
                 evidence: Optional[Dict] = None, description: str = ""):
        self.fault_detected = fault_detected
        self.fault_type = fault_type
        self.severity = severity
        self.confidence = confidence
        self.evidence = evidence or {}
        self.description = description

    def to_dict(self) -> Dict[str, Any]:
        return {
            "fault_detected": self.fault_detected,
            "fault_type": self.fault_type,
            "severity": self.severity,
            "confidence": self.confidence,
            "evidence": self.evidence,
            "description": self.description,
        }


class BaseDetector(ABC):
    """检测器基类。"""

    def __init__(self, config: Optional[Dict] = None):
        self.config = config or {}
        self._events_processed = 0

    @abstractmethod
    def process_event(self, event: Dict[str, Any]) -> Optional[DetectionResult]:
        """处理一个事件，返回检测结果（无异常时返回None）。"""
        pass

    @abstractmethod
    def get_state(self) -> Dict[str, Any]:
        """获取检测器内部状态（用于调试和快照）。"""
        pass

    def reset(self):
        """重置检测器状态。"""
        self._events_processed = 0


class HeartbeatMonitor(BaseDetector):
    """
    心跳检测器。
    超过 heartbeat_timeout_s 无心跳事件 → 判定为进程崩溃。
    """
    def __init__(self, config: Optional[Dict] = None):
        super().__init__(config)
        self.timeout_s = self.config.get("heartbeat_timeout_s", 15.0)
        self.last_heartbeat_ms: Optional[int] = None
        self.last_heartbeat_seq: Optional[int] = None
        self._crash_detected = False

    def process_event(self, event: Dict[str, Any]) -> Optional[DetectionResult]:
        self._events_processed += 1
        if event.get("event_type") == "heartbeat":
            self.last_heartbeat_ms = event.get("timestamp_ms")
            self.last_heartbeat_seq = event.get("seq")
            self._crash_detected = False
            return None
        # 非心跳事件时检查是否超时
        if self.last_heartbeat_ms is not None and not self._crash_detected:
            now_ms = event.get("timestamp_ms", int(time.time() * 1000))
            gap_s = (now_ms - self.last_heartbeat_ms) / 1000.0
            if gap_s > self.timeout_s:
                self._crash_detected = True
                return DetectionResult(
                    fault_detected=True,
                    fault_type="PROCESS_CRASH",
                    severity="critical",
                    confidence=0.95,
                    evidence={"last_heartbeat_ms": self.last_heartbeat_ms,
                              "gap_s": round(gap_s, 2),
                              "timeout_s": self.timeout_s},
                    description=f"超过{self.timeout_s}s无心跳，判定进程崩溃",
                )
        return None

    def get_state(self) -> Dict[str, Any]:
        return {
            "last_heartbeat_ms": self.last_heartbeat_ms,
            "timeout_s": self.timeout_s,
            "crash_detected": self._crash_detected,
        }

    def reset(self):
        super().reset()
        self.last_heartbeat_ms = None
        self._crash_detected = False


class StallMonitor(BaseDetector):
    """
    无进展检测器。
    uptime（心跳中的uptime_s）持续增长但 task_progress 的 step 不变 → 死循环/活锁。
    """
    def __init__(self, config: Optional[Dict] = None):
        super().__init__(config)
        self.stall_timeout_s = self.config.get("stall_timeout_s", 8.0)
        self.last_step: Optional[int] = None
        self.last_step_time_ms: Optional[int] = None
        self.current_uptime_s: float = 0.0
        self._stall_detected = False

    def process_event(self, event: Dict[str, Any]) -> Optional[DetectionResult]:
        self._events_processed += 1
        etype = event.get("event_type")

        if etype == "heartbeat":
            self.current_uptime_s = event.get("uptime_s", 0.0)
            # 心跳时检查是否停滞
            if (self.last_step is not None and self.last_step_time_ms is not None
                    and not self._stall_detected):
                now_ms = event.get("timestamp_ms", int(time.time() * 1000))
                stall_duration_s = (now_ms - self.last_step_time_ms) / 1000.0
                if stall_duration_s > self.stall_timeout_s and self.current_uptime_s > 0:
                    self._stall_detected = True
                    return DetectionResult(
                        fault_detected=True,
                        fault_type="INFINITE_LOOP",
                        severity="high",
                        confidence=0.88,
                        evidence={"last_step": self.last_step,
                                  "stall_duration_s": round(stall_duration_s, 2),
                                  "current_uptime_s": self.current_uptime_s},
                        description=f"任务步骤停滞{round(stall_duration_s, 1)}s但心跳正常，判定死循环/活锁",
                    )

        elif etype == "task_progress":
            step = event.get("step", 0)
            if self.last_step is None or step > self.last_step:
                self.last_step = step
                self.last_step_time_ms = event.get("timestamp_ms")
                self._stall_detected = False

        return None

    def get_state(self) -> Dict[str, Any]:
        return {
            "last_step": self.last_step,
            "last_step_time_ms": self.last_step_time_ms,
            "stall_timeout_s": self.stall_timeout_s,
            "stall_detected": self._stall_detected,
        }

    def reset(self):
        super().reset()
        self.last_step = None
        self.last_step_time_ms = None
        self.current_uptime_s = 0.0
        self._stall_detected = False


class ToolTimeoutMonitor(BaseDetector):
    """
    工具超时/无输出检测器。

    两条独立判据（P1修复：no_output_timeout 独立判别）：
    1. 监护超时：tool_call 发布后超过 tool_timeout_s 未收到对应 call_id 的
       tool_result → 工具卡住，判定 NO_OUTPUT_TIMEOUT；
    2. 执行方回报超时：tool_result.ok=False 且 error_message 含超时特征 →
       工具自身回报无输出超时，判定 NO_OUTPUT_TIMEOUT（与看门狗判据互相印证）。
    """
    def __init__(self, config: Optional[Dict] = None):
        super().__init__(config)
        self.timeout_s = self.config.get("tool_timeout_s", 5.0)
        self.cpu_threshold = self.config.get("cpu_threshold_percent", 50.0)
        self.last_cpu_percent = 0.0
        # 归因标记：一旦判定过工具挂起，本轮运行不再把停滞归因于Agent死循环
        self.attributed = False
        self.pending_calls: Dict[str, Dict] = {}  # call_id -> {tool_name, call_time_ms, step}

    def _discriminate(self, tool_name: str, duration_s: float, call_id: str,
                      detection_mode: str) -> DetectionResult:
        """按CPU特征区分忙等死循环（高CPU）与无输出挂起（低CPU）。"""
        cpu = self.last_cpu_percent
        if cpu >= self.cpu_threshold:
            return DetectionResult(
                fault_detected=True,
                fault_type="INFINITE_LOOP",
                severity="high",
                confidence=0.88,
                evidence={"call_id": call_id, "tool_name": tool_name,
                          "duration_s": round(duration_s, 2), "cpu_percent": round(cpu, 1),
                          "detection_mode": detection_mode},
                description=(f"工具{tool_name}挂起{round(duration_s, 1)}s且CPU占用{cpu:.0f}%"
                             f"（忙等特征），判定死循环"),
            )
        return DetectionResult(
            fault_detected=True,
            fault_type="NO_OUTPUT_TIMEOUT",
            severity="medium",
            confidence=0.92,
            evidence={"call_id": call_id, "tool_name": tool_name,
                      "duration_s": round(duration_s, 2), "cpu_percent": round(cpu, 1),
                      "detection_mode": detection_mode},
            description=(f"工具{tool_name}挂起{round(duration_s, 1)}s且CPU占用{cpu:.0f}%"
                         f"（阻塞特征），判定无输出超时"),
        )

    def process_event(self, event: Dict[str, Any]) -> Optional[DetectionResult]:
        self._events_processed += 1
        etype = event.get("event_type")

        # 跟踪最近一次CPU采样，供挂起判别使用
        if etype == "resource":
            try:
                self.last_cpu_percent = float(event.get("cpu_percent", 0.0) or 0.0)
            except (TypeError, ValueError):
                self.last_cpu_percent = 0.0

        if etype == "tool_call":
            call_id = event.get("call_id", "")
            if call_id:
                self.pending_calls[call_id] = {
                    "tool_name": event.get("tool_name"),
                    "call_time_ms": event.get("timestamp_ms"),
                    "step": event.get("step"),
                }

        elif etype == "tool_result":
            call_id = event.get("call_id", "")
            if call_id and call_id in self.pending_calls:
                del self.pending_calls[call_id]
            # 判据2：执行方自身回报超时 → 独立判为无输出超时
            if not event.get("ok", True):
                err_text = ((event.get("error_message") or "") +
                            (event.get("observation") or "")).lower()
                if "timed out" in err_text or "timeout" in err_text:
                    self.attributed = True
                    return DetectionResult(
                        fault_detected=True,
                        fault_type="NO_OUTPUT_TIMEOUT",
                        severity="medium",
                        confidence=0.95,
                        evidence={"call_id": call_id,
                                  "tool_name": event.get("tool_name"),
                                  "error_message": event.get("error_message", ""),
                                  "detection_mode": "executor_report"},
                        description=f"工具 {event.get('tool_name')} 执行方回报超时，判定无输出超时",
                    )

        # 任何事件都检查超时（判据1：监护侧超时，按CPU特征区分死循环/无输出）
        now_ms = event.get("timestamp_ms", int(time.time() * 1000))
        timed_out = []
        for call_id, info in self.pending_calls.items():
            duration_s = (now_ms - info["call_time_ms"]) / 1000.0
            if duration_s > self.timeout_s:
                timed_out.append((call_id, info, duration_s))

        for call_id, info, duration_s in timed_out:
            del self.pending_calls[call_id]
            self.attributed = True
            return self._discriminate(info["tool_name"], duration_s, call_id, "monitor_timeout")

        return None

    def get_state(self) -> Dict[str, Any]:
        return {
            "pending_calls_count": len(self.pending_calls),
            "pending_call_ids": list(self.pending_calls.keys()),
            "timeout_s": self.timeout_s,
        }

    def reset(self):
        super().reset()
        self.pending_calls.clear()
        self.attributed = False
        self.last_cpu_percent = 0.0


class MemoryMonitor(BaseDetector):
    """
    内存检测器。
    resource 事件中 memory_mb 持续增长超过 memory_threshold_mb → 内存泄漏。
    使用滑动窗口判断增长趋势。
    """
    def __init__(self, config: Optional[Dict] = None):
        super().__init__(config)
        self.threshold_mb = self.config.get("memory_threshold_mb", 300.0)
        self.growth_window = self.config.get("growth_window", 5)  # 最近N个采样点
        self.memory_history: List[float] = []
        self._leak_detected = False

    def process_event(self, event: Dict[str, Any]) -> Optional[DetectionResult]:
        self._events_processed += 1
        if event.get("event_type") == "resource":
            mem_mb = event.get("memory_mb", 0.0)
            if mem_mb > 0:
                self.memory_history.append(mem_mb)
                if len(self.memory_history) > self.growth_window:
                    self.memory_history.pop(0)

                # 检查绝对阈值
                if mem_mb > self.threshold_mb and not self._leak_detected:
                    self._leak_detected = True
                    return DetectionResult(
                        fault_detected=True,
                        fault_type="MEMORY_BLOAT",
                        severity="high",
                        confidence=0.85,
                        evidence={"current_mb": mem_mb,
                                  "threshold_mb": self.threshold_mb,
                                  "history": self.memory_history[-5:]},
                        description=f"内存使用 {mem_mb:.0f}MB 超过阈值 {self.threshold_mb}MB",
                    )

                # 检查增长趋势（窗口内单调增长）
                if (len(self.memory_history) >= self.growth_window
                        and not self._leak_detected):
                    window = self.memory_history[-self.growth_window:]
                    is_monotonic = all(window[i] < window[i+1] for i in range(len(window)-1))
                    growth_mb = window[-1] - window[0]
                    if is_monotonic and growth_mb > 50:
                        self._leak_detected = True
                        return DetectionResult(
                            fault_detected=True,
                            fault_type="MEMORY_BLOAT",
                            severity="medium",
                            confidence=0.75,
                            evidence={"growth_mb": round(growth_mb, 1),
                                      "window_size": len(window),
                                      "history": window},
                            description=f"内存连续{len(window)}个采样点单调增长，累计{growth_mb:.0f}MB，疑似泄漏",
                        )

        return None

    def get_state(self) -> Dict[str, Any]:
        return {
            "memory_history": self.memory_history,
            "threshold_mb": self.threshold_mb,
            "leak_detected": self._leak_detected,
        }

    def reset(self):
        super().reset()
        self.memory_history.clear()
        self._leak_detected = False


class RepetitiveCallMonitor(BaseDetector):
    """
    重复调用检测器。
    同一工具连续调用超过 repeat_threshold 次 → 重复循环。
    """
    def __init__(self, config: Optional[Dict] = None):
        super().__init__(config)
        self.repeat_threshold = self.config.get("repeat_threshold", 4)
        self.last_tool: Optional[str] = None
        self.consecutive_count = 0
        self.call_history: List[str] = []
        self._detected = False

    def process_event(self, event: Dict[str, Any]) -> Optional[DetectionResult]:
        self._events_processed += 1
        if event.get("event_type") == "tool_call":
            tool_name = event.get("tool_name", "")
            self.call_history.append(tool_name)
            if len(self.call_history) > 10:
                self.call_history.pop(0)

            if tool_name == self.last_tool:
                self.consecutive_count += 1
            else:
                self.consecutive_count = 1
                self.last_tool = tool_name
                self._detected = False

            if self.consecutive_count >= self.repeat_threshold and not self._detected:
                self._detected = True
                return DetectionResult(
                    fault_detected=True,
                    fault_type="REPETITIVE_CALLS",
                    severity="medium",
                    confidence=0.92,
                    evidence={"tool_name": tool_name,
                              "consecutive_count": self.consecutive_count,
                              "threshold": self.repeat_threshold,
                              "recent_calls": self.call_history[-5:]},
                    description=f"工具 {tool_name} 连续调用 {self.consecutive_count} 次，疑似重复循环",
                )

        return None

    def get_state(self) -> Dict[str, Any]:
        return {
            "last_tool": self.last_tool,
            "consecutive_count": self.consecutive_count,
            "repeat_threshold": self.repeat_threshold,
            "detected": self._detected,
        }

    def reset(self):
        super().reset()
        self.last_tool = None
        self.consecutive_count = 0
        self.call_history.clear()
        self._detected = False
