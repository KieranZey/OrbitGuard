# -*- coding: utf-8 -*-
"""
简化版星载任务 Agent（固定逻辑，无 LLM）。

执行链路：
  接收任务 -> 读取遥测 -> 故障诊断 -> 选择工具 -> 调用工具 -> 返回结果

与主应用 StarNet-Mind 的对应关系：
  - 本文件的 TaskAgent    <-> satellite/satAgent.py + master/brain.py
  - telemetry.py           <-> satellite/basiliskSatellite.py
  - diagnosis.py           <-> diagnosis/clipsEngine.py
  - tools.py               <-> satellite/executor.py
  - 通信层（print/日志）    <-> redisnet/redisBus.py（后续替换为 Redis Pub/Sub）

设计目标：
  1. 确定性执行：不依赖 LLM，每次运行行为可复现
  2. 可观测：每一步都输出结构化事件，供监护器监听
  3. 可注入：关键节点都有故障注入装饰器钩子
  4. 接口对齐：所有事件携带统一公共字段（run_id/global_seq/schema_version/agent_id），
     tool_call 与 tool_result 通过 call_id 可靠配对，遥测帧可被下游独立重算哈希
"""
import time
import json
import hashlib
import threading
import sys
import os
from typing import Dict, Any, List, Optional

# 把项目根目录加入 path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.telemetry import TelemetrySource
from agent.diagnosis import DiagnosisEngine, FaultEvent
from agent.tools import ToolRegistry, ToolResult
from fault_injector.injector import FaultInjector

SCHEMA_VERSION = "fault-injector-v1"


class TaskAgent:
    """
    简化版星载任务 Agent。
    固定逻辑执行：遥测 -> 诊断 -> 决策 -> 工具调用 -> 结果。
    """

    def __init__(self, config: Dict[str, Any], injector: Optional[FaultInjector] = None,
                 event_bus=None, gate=None):
        self.config = config
        self.event_bus = event_bus  # 可选的事件总线回调（用于编排器捕获事件）
        agent_cfg = config.get("agent", {})
        self.sat_id = agent_cfg.get("sat_id", "SAT-001")
        self.agent_id = agent_cfg.get("agent_id", f"{self.sat_id}-AGENT")
        self.task_id = agent_cfg.get("task_id", "TASK-DEMO-001")
        self.task_type = agent_cfg.get("task_type", "diagnosis")
        self.mission_phase = agent_cfg.get("mission_phase", "NOMINAL")
        self.max_tool_calls = agent_cfg.get("max_tool_calls", 8)
        self.decision_timeout_ms = agent_cfg.get("decision_timeout_ms", 30000)

        # 统一运行标识（P0-⑤）
        self.run_id = f"RUN-{int(time.time()*1000)}-{hashlib.md5(os.urandom(4)).hexdigest()[:6]}"
        self._global_seq = 0

        # 核心组件
        noise_config = config.get("agent", {}).get("telemetry_noise", {})
        self.telemetry_source = TelemetrySource(self.sat_id, noise_config=noise_config)
        self.diagnosis_engine = DiagnosisEngine()
        self.tool_registry = ToolRegistry()

        # 故障注入器
        self.injector = injector
        if self.injector:
            self.injector.apply_telemetry_faults(self.telemetry_source)

        # 执行前置门控（可选，非侵入）：mutation 工具执行前必须持一次性许可
        # gate=None 时行为与旧版完全一致
        self.gate = gate

        # 给工具调用加上包装层（事件记录 + 执行层故障注入 + 前置门控）
        if self.injector or self.gate is not None:
            self._original_call = self.tool_registry.call
            self.tool_registry.call = self._wrap_tool_call(self.tool_registry.call)

        # 运行状态
        self._running = False
        self._heartbeat_thread = None
        self._telemetry_thread = None
        self._resource_thread = None
        self._tool_call_history: List[Dict] = []
        self._event_log: List[Dict] = []
        self._start_time = None
        self._active_faults: List[str] = []  # 当前活跃故障列表（P1-⑪）
        self._bloat_data: List[bytearray] = []
        self._crash_flag = False  # 异常路径标记：shutdown 事件携带 agent_fault 原因

    # ------------------------------------------------------------------
    # 公共字段注入
    # ------------------------------------------------------------------
    def _log_event(self, event: Dict):
        """记录结构化事件（模拟 Redis Pub/Sub 发布），自动注入统一公共字段。"""
        self._global_seq += 1
        event["schema_version"] = SCHEMA_VERSION
        event["run_id"] = self.run_id
        event["agent_id"] = self.agent_id
        event["global_seq"] = self._global_seq
        event["task_type"] = self.task_type
        event["mission_phase"] = self.mission_phase
        self._event_log.append(event)
        # 同时输出到 stdout，模拟通道消息（监护器可订阅）
        print(f"[EVENT] {json.dumps(event, ensure_ascii=False)}", flush=True)
        # 如果设置了 event_bus，也发布到事件总线（编排器捕获用）
        if self.event_bus is not None:
            self.event_bus.publish(event)

    # ------------------------------------------------------------------
    # 工具调用包装（P0-① call_id 配对 + P1-⑩ 决策超时）
    # ------------------------------------------------------------------
    def _wrap_tool_call(self, func):
        """包装工具调用，添加故障注入和事件记录。"""
        def wrapped(tool_name, args=None):
            # 生成统一 call_id，确保 tool_call 与 tool_result 可靠配对
            call_id = f"CALL-{int(time.time()*1000)}-{hashlib.md5(str(time.time()).encode()).hexdigest()[:6]}"
            # 从 TOOL_MANIFEST 读取动作意图元数据（供 Gateway action_intent_snapshot 提取）
            manifest = self.tool_registry.TOOL_MANIFEST.get(tool_name, {})
            # 记录工具调用事件
            call_event = {
                "event_type": "tool_call",
                "sat_id": self.sat_id,
                "task_id": self.task_id,
                "step": self.injector.get_step() if self.injector else 0,
                "tool_name": tool_name,
                "args": args,
                "risk_level": self.tool_registry.get_risk_level(tool_name),
                "call_id": call_id,
                # 动作意图元数据（P1：Gateway C1-C5 物理约束 + 可逆性检查需要）
                "is_mutation": manifest.get("mutation", False),
                "requires_approval": manifest.get("requires_approval", False),
                "action_category": manifest.get("action_category", "PAYLOAD_OPERATION"),
                "is_reversible": manifest.get("is_reversible", True),
                "power_delta_w": manifest.get("power_delta_w", 0.0),
                "thermal_delta_c": manifest.get("thermal_delta_c", 0.0),
                "estimated_duration_s": manifest.get("estimated_duration_s", 1.0),
                "timestamp_ms": int(time.time() * 1000),
            }
            self._log_event(call_event)
            self._tool_call_history.append(call_event)

            # ===== 执行前置门控（P0架构修复）：mutation 工具执行前必须持一次性许可 =====
            permit_id = None
            if self.gate is not None and manifest.get("mutation", False):
                verdict = self.gate.authorize(call_event)
                self._log_event({
                    "event_type": "gate_decision",
                    "sat_id": self.sat_id,
                    "task_id": self.task_id,
                    "call_id": call_id,
                    "tool_name": tool_name,
                    "approved": verdict.approved,
                    "permit_id": verdict.permit_id,
                    "route": verdict.route,
                    "reason": verdict.reason,
                    "timestamp_ms": int(time.time() * 1000),
                })
                if not verdict.approved:
                    # 门控拒绝：工具不执行，返回结构化失败结果（call_id 配对，清除超时跟踪）
                    denied_result = ToolResult(
                        call_id=call_id, tool_name=tool_name, ok=False,
                        error_message=f"GATED_DENIED: {verdict.reason}",
                        data={"route": verdict.route, "permit_id": None, "gated": True},
                    )
                    self._log_event({
                        "event_type": "tool_result",
                        "call_id": call_id,
                        "tool_name": tool_name,
                        "ok": False,
                        "gated": True,
                        "error_message": denied_result.error_message,
                        "duration_ms": 0,
                        "task_id": self.task_id,
                        "sat_id": self.sat_id,
                        "timestamp_ms": int(time.time() * 1000),
                    })
                    return denied_result
                permit_id = verdict.permit_id

            # 执行层故障注入（支持多故障叠加：memory_bloat可与其他故障叠加）
            if self.injector:
                triggered = self.injector.get_triggered_execution_faults()
                # 1. 先执行非终结性故障中的 memory_bloat（pre-execution，分配内存但不返回）
                if "memory_bloat" in triggered["non_terminal"]:
                    self.injector.mark_active("memory_bloat")
                    self._apply_memory_bloat(tool_name)
                # 2. 再检查终结性故障（互斥，最多一个）
                if triggered["terminal"]:
                    fault_name = triggered["terminal"][0]
                    self.injector.mark_active(fault_name)
                    return self._execute_stability_fault(fault_name, tool_name, args)
                # 3. 检查 no_output_timeout（非终结性但执行后返回超时结果）
                if "no_output_timeout" in triggered["non_terminal"]:
                    self.injector.mark_active("no_output_timeout")
                    return self._execute_stability_fault("no_output_timeout", tool_name, args)

            # 正常调用（传入统一 call_id）
            result = func(tool_name, args, call_id=call_id)

            # 一次性许可消费（防重放）：执行完成即消费，同一许可重复使用会被拒绝
            permit_consumed = None
            if permit_id is not None and self.gate is not None:
                try:
                    permit_consumed = self.gate.consume(permit_id, self.gate.binding_digest_for(call_event))
                except Exception:
                    permit_consumed = False

            # 记录工具结果事件（P0-② 补充 task_id/sat_id；P0门控 补充许可信息）
            result_event = {
                "event_type": "tool_result",
                "call_id": result.call_id,
                "tool_name": tool_name,
                "ok": result.ok,
                "duration_ms": result.duration_ms,
                "error_message": result.error_message,
                "task_id": self.task_id,
                "sat_id": self.sat_id,
                "permit_id": permit_id,
                "permit_consumed": permit_consumed,
                "timestamp_ms": result.timestamp_ms,
            }
            self._log_event(result_event)
            return result

        return wrapped

    def _apply_memory_bloat(self, tool_name: str):
        """内存暴涨故障（pre-execution hook：分配内存但不返回，可与其他故障叠加）。"""
        cfg = self.injector.get_fault_config("memory_bloat", "stability")
        alloc_mb = cfg.get("alloc_mb_per_step", 50)
        max_mb = cfg.get("max_alloc_mb", 400)
        self.injector.gt_logger.log(
            fault_type="memory_bloat", target=f"tool:{tool_name}",
            trigger_step=self.injector.get_step(),
            details={"alloc_mb_per_step": alloc_mb, "max_alloc_mb": max_mb, "mode": "pre_execution_overlay"},
            expected_behavior=f"内存阶梯增长至{max_mb}MB（可与其他故障叠加）",
        )
        current_mb = len(self._bloat_data) * alloc_mb
        if current_mb < max_mb:
            self._bloat_data.append(bytearray(alloc_mb * 1024 * 1024))

    def _execute_stability_fault(self, fault_name: str, tool_name: str, args: Dict) -> ToolResult:
        """执行稳定性故障（在工具调用上下文中）。"""
        cfg = self.injector.get_fault_config(fault_name, "stability")

        if fault_name == "infinite_loop":
            self.injector.gt_logger.log(
                fault_type="infinite_loop", target=f"tool:{tool_name}",
                trigger_step=self.injector.get_step(),
                details={"mode": "tool_context_livelock", "tool": tool_name},
                expected_behavior="工具调用陷入死循环，Agent任务无进展但心跳继续",
            )
            # 死循环：忙等活锁（P1修复：忙等而非sleep——RSM看门狗用CPU特征区分
            # INFINITE_LOOP（高CPU）与 NO_OUTPUT_TIMEOUT（低CPU），实现独立判别）
            # 心跳继续因为心跳在独立线程；循环响应 _running 停机标志，
            # Agent 被外部停机（编排器超时/trial清理）时忙等线程立即退出，不泄漏
            while self._running:
                pass

        elif fault_name == "process_crash":
            mode = cfg.get("mode", "exception")
            self.injector.gt_logger.log(
                fault_type="process_crash", target=f"tool:{tool_name}",
                trigger_step=self.injector.get_step(),
                details={"mode": mode, "tool": tool_name},
                expected_behavior="Agent进程崩溃，心跳停止，任务中断",
            )
            if mode == "exit":
                os._exit(1)
            else:
                raise RuntimeError(f"[FAULT INJECTION] Simulated crash during tool: {tool_name}")

        elif fault_name == "no_output_timeout":
            sleep_sec = cfg.get("sleep_seconds", 120)
            self.injector.gt_logger.log(
                fault_type="no_output_timeout", target=f"tool:{tool_name}",
                trigger_step=self.injector.get_step(),
                details={"sleep_seconds": sleep_sec, "tool": tool_name},
                expected_behavior=f"工具调用卡住{sleep_sec}秒不返回，任务超时",
            )
            time.sleep(sleep_sec)
            # 卡住后仍然不返回有效结果
            return ToolResult(
                call_id=f"CALL-TIMEOUT-{int(time.time())}",
                tool_name=tool_name, ok=False,
                error_message=f"Tool timed out after {sleep_sec}s (fault injection)",
            )

        return self._original_call(tool_name, args)

    # ------------------------------------------------------------------
    # 后台线程：心跳 / 遥测 / 资源采样（P1-⑨）
    # ------------------------------------------------------------------
    def _heartbeat_loop(self):
        """心跳循环（独立线程）。"""
        interval_ms = self.config.get("agent", {}).get("heartbeat_interval_ms", 5000)
        while self._running:
            heartbeat = {
                "event_type": "heartbeat",
                "sat_id": self.sat_id,
                "task_id": self.task_id,
                "status": "running",
                "active_faults": list(self._active_faults),  # P1-⑪ 使用维护的活跃故障列表
                "tool_calls_done": len(self._tool_call_history),
                "uptime_s": round(time.time() - self._start_time, 1) if self._start_time else 0,
                "timestamp_ms": int(time.time() * 1000),
            }
            self._log_event(heartbeat)
            time.sleep(interval_ms / 1000)

    def _telemetry_loop(self):
        """遥测采集循环（独立线程）。"""
        interval_ms = self.config.get("agent", {}).get("telemetry_interval_ms", 1000)
        while self._running:
            frame = self.telemetry_source.get_telemetry()
            if frame is None:
                # 数据丢包：发布 packet_loss 事件供监护器检测
                loss_event = {
                    "event_type": "packet_loss",
                    "sat_id": self.sat_id,
                    "task_id": self.task_id,
                    "seq": self.telemetry_source._seq,
                    "reason": "noise_model_packet_loss",
                    "timestamp_ms": int(time.time() * 1000),
                }
                self._log_event(loss_event)
            else:
                tel_event = {
                    "event_type": "telemetry",
                    "sat_id": self.sat_id,
                    "seq": frame["seq"],
                    "telemetry": frame["telemetry"],
                    "status_summary": frame["status_summary"],
                    "timestamp_ms": frame["timestamp_ms"],
                    "source": "background",  # 标记来源：后台遥测线程
                }
                self._log_event(tel_event)
            time.sleep(interval_ms / 1000)

    def _resource_loop(self):
        """资源采样循环（独立线程）—— P1-⑨ 为 RSM 提供 CPU/RSS 观测。"""
        interval_ms = self.config.get("agent", {}).get("resource_interval_ms", 1000)
        last_cpu_time = time.process_time()
        last_wall_time = time.time()
        while self._running:
            now = time.time()
            wall_delta = now - last_wall_time
            current_cpu_time = time.process_time()
            cpu_percent = (current_cpu_time - last_cpu_time) / wall_delta * 100 if wall_delta > 0 else 0.0
            last_cpu_time = current_cpu_time
            last_wall_time = now
            # memory_bloat 阶梯式分配：每次资源采样时分配内存，形成持续增长
            if self.injector and self.injector.is_fault_enabled("memory_bloat", "stability"):
                cfg = self.injector.get_fault_config("memory_bloat", "stability")
                alloc_mb = cfg.get("alloc_mb_per_step", 50)
                max_mb = cfg.get("max_alloc_mb", 400)
                if len(self.injector._memory_bloat_data) * alloc_mb < max_mb:
                    self.injector._memory_bloat_data.append(bytearray(alloc_mb * 1024 * 1024))
            memory_mb = self._get_memory_mb()
            self._log_event({
                "event_type": "resource",
                "cpu_percent": round(cpu_percent, 1),
                "memory_mb": round(memory_mb, 1),
                "timestamp_ms": int(now * 1000),
            })
            time.sleep(interval_ms / 1000)

    @staticmethod
    def _get_memory_mb() -> float:
        """跨平台获取当前进程 RSS 内存（MB），零第三方依赖。"""
        if sys.platform == "win32":
            try:
                import ctypes
                from ctypes import wintypes
                kernel32 = ctypes.windll.kernel32
                psapi = ctypes.windll.psapi
                PROCESS_QUERY_INFORMATION = 0x0400
                pid = kernel32.GetCurrentProcessId()
                handle = kernel32.OpenProcess(PROCESS_QUERY_INFORMATION, False, pid)
                if not handle:
                    return 0.0

                class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
                    _fields_ = [
                        ("cb", wintypes.DWORD),
                        ("PageFaultCount", wintypes.DWORD),
                        ("PeakWorkingSetSize", ctypes.c_size_t),
                        ("WorkingSetSize", ctypes.c_size_t),
                        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                        ("PagefileUsage", ctypes.c_size_t),
                        ("PeakPagefileUsage", ctypes.c_size_t),
                    ]
                pmc = PROCESS_MEMORY_COUNTERS()
                pmc.cb = ctypes.sizeof(pmc)
                result = 0.0
                if psapi.GetProcessMemoryInfo(handle, ctypes.byref(pmc), pmc.cb):
                    result = pmc.WorkingSetSize / 1024 / 1024
                kernel32.CloseHandle(handle)
                return result
            except Exception:
                pass
            return 0.0
        else:
            try:
                with open("/proc/self/status") as f:
                    for line in f:
                        if line.startswith("VmRSS:"):
                            return int(line.split()[1]) / 1024
            except Exception:
                pass
            return 0.0

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def start(self):
        """启动 Agent（心跳 + 遥测 + 资源采样线程）。"""
        self._running = True
        self._start_time = time.time()
        self._heartbeat_thread = threading.Thread(target=self._heartbeat_loop, daemon=True, name="heartbeat")
        self._telemetry_thread = threading.Thread(target=self._telemetry_loop, daemon=True, name="telemetry")
        self._resource_thread = threading.Thread(target=self._resource_loop, daemon=True, name="resource")
        self._heartbeat_thread.start()
        self._telemetry_thread.start()
        self._resource_thread.start()
        print(f"[AGENT] TaskAgent started: sat={self.sat_id}, task={self.task_id}, run={self.run_id}", flush=True)

    def stop(self):
        """停止 Agent（P1修复：发布shutdown事件，看门狗看到正常shutdown后不判崩溃）。"""
        self._running = False
        shutdown_reason = "agent_fault" if getattr(self, "_crash_flag", False) else "normal_shutdown"
        try:
            self._log_event({
                "event_type": "agent_shutdown",
                "task_id": self.task_id,
                "sat_id": self.sat_id,
                "status": "stopped",
                "reason": shutdown_reason,
                "timestamp_ms": int(time.time() * 1000),
            })
        except Exception:
            pass
        print(f"[AGENT] TaskAgent stopped", flush=True)

    # ------------------------------------------------------------------
    # 任务执行主循环
    # ------------------------------------------------------------------
    def run_task(self, task_description: str = "fault_diagnosis_and_recovery") -> Dict[str, Any]:
        """
        执行一个任务（固定逻辑，无 LLM）。

        执行步骤：
          1. 读取最新遥测（并发布该帧，供下游独立重算哈希 —— P0-④）
          2. 故障诊断
          3. 可信性故障注入（诊断输出层）
          4. 根据诊断结论选择工具
          5. 调用工具（执行层故障注入 + repetitive_calls 在此触发 —— P1-⑥）
          6. 返回结构化结果
        """
        self.start()
        time.sleep(0.5)  # 等遥测和心跳线程启动

        # P1-⑩ 决策超时：任务级 deadline
        deadline = time.time() + self.decision_timeout_ms / 1000

        def _check_timeout() -> Optional[Dict]:
            if time.time() > deadline:
                timeout_result = {
                    "event_type": "agent_output",
                    "task_id": self.task_id,
                    "sat_id": self.sat_id,
                    "status": "timeout",
                    "error": f"Task exceeded decision_timeout_ms={self.decision_timeout_ms}",
                    "error_type": "DecisionTimeout",
                    "timestamp_ms": int(time.time() * 1000),
                }
                self._log_event(timeout_result)
                return timeout_result
            return None

        try:
            # ===== 步骤1: 读取遥测 =====
            if self.injector:
                self.injector.increment_step()
            # 任务步骤同步读取遥测，丢包时重试（最多5次）
            telemetry_frame = None
            for _retry in range(5):
                telemetry_frame = self.telemetry_source.get_telemetry()
                if telemetry_frame is not None:
                    break
                time.sleep(0.01)
            if telemetry_frame is None:
                # 极端情况：连续丢包，使用空帧兜底
                telemetry_frame = {
                    "seq": 0, "telemetry": {}, "status_summary": "PACKET_LOSS",
                    "timestamp_ms": int(time.time() * 1000),
                }
            # P0-④：发布被引用的遥测帧，确保 agent_output.evidence.telemetry_seq 可被下游独立重算
            self._log_event({
                "event_type": "telemetry",
                "sat_id": self.sat_id,
                "seq": telemetry_frame["seq"],
                "telemetry": telemetry_frame["telemetry"],
                "status_summary": telemetry_frame["status_summary"],
                "timestamp_ms": telemetry_frame["timestamp_ms"],
                "source": "task_read",  # 标记来源：任务步骤读取（与后台遥测线程区分）
            })
            self._log_event({
                "event_type": "task_progress",
                "task_id": self.task_id,
                "step": self.injector.get_step() if self.injector else 1,
                "action": "read_telemetry",
                "status": "done",
                "timestamp_ms": int(time.time() * 1000),
            })
            tmo = _check_timeout()
            if tmo:
                return tmo

            # ===== 步骤2: 故障诊断 =====
            if self.injector:
                self.injector.increment_step()
            faults = self.diagnosis_engine.diagnose(telemetry_frame)
            fault_dicts = [f.to_dict() for f in faults]

            # ===== 步骤3: 可信性故障注入（诊断输出层）=====
            if self.injector:
                fault_dicts = self.injector.inject_diagnosis_output(fault_dicts)

            # P1-⑪：更新活跃故障列表（排除 NO_FAULT）
            self._active_faults = [f["fault_code"] for f in fault_dicts if f.get("fault_code") != "NO_FAULT"]

            self._log_event({
                "event_type": "diagnosis",
                "task_id": self.task_id,
                "step": self.injector.get_step() if self.injector else 2,
                "faults": fault_dicts,
                "timestamp_ms": int(time.time() * 1000),
            })
            tmo = _check_timeout()
            if tmo:
                return tmo

            # ===== 步骤4: 根据诊断选择工具（固定决策逻辑）=====
            if self.injector:
                self.injector.increment_step()
            tool_plan = self._plan_tools(fault_dicts)
            self._log_event({
                "event_type": "task_progress",
                "task_id": self.task_id,
                "step": self.injector.get_step() if self.injector else 3,
                "action": "plan_tools",
                "tool_plan": tool_plan,
                "status": "done",
                "timestamp_ms": int(time.time() * 1000),
            })
            tmo = _check_timeout()
            if tmo:
                return tmo

            # ===== 步骤5: 调用工具（含 repetitive_calls 故障 —— P1-⑥）=====
            tool_results = []
            repeat_count = 1
            repeat_target = None

            # 检查 repetitive_calls 故障是否触发
            if self.injector and self.injector.should_trigger("repetitive_calls", "stability"):
                rep_cfg = self.injector.get_fault_config("repetitive_calls", "stability")
                repeat_count = rep_cfg.get("repeat_count", 3)
                repeat_target = rep_cfg.get("target_tool")
                self.injector.mark_active("repetitive_calls")
                self.injector.gt_logger.log(
                    fault_type="repetitive_calls",
                    target=f"tool:{repeat_target or 'first_in_plan'}",
                    trigger_step=self.injector.get_step(),
                    details={"repeat_count": repeat_count, "target_tool": repeat_target},
                    expected_behavior=f"对同一工具重复调用{repeat_count}次，RSM应检测到重复调用模式",
                )

            for i, plan_item in enumerate(tool_plan):
                if i >= self.max_tool_calls:
                    break
                if self.injector:
                    self.injector.increment_step()

                # 确定该工具调用次数（repetitive_calls 只对第一个匹配工具生效）
                calls_this_tool = 1
                if repeat_count > 1 and (repeat_target is None or plan_item["tool"] == repeat_target):
                    calls_this_tool = repeat_count
                    repeat_count = 1  # 只重复一次，后续工具恢复正常

                for _ in range(calls_this_tool):
                    result = self.tool_registry.call(plan_item["tool"], plan_item.get("args", {}))
                    tool_results.append(result.to_dict())

                tmo = _check_timeout()
                if tmo:
                    return tmo

            # ===== 步骤6: 返回结果 =====
            final_result = {
                "event_type": "agent_output",
                "task_id": self.task_id,
                "sat_id": self.sat_id,
                "status": "completed",
                "conclusion": self._generate_conclusion(fault_dicts, tool_results),
                "confidence": self._calculate_confidence(fault_dicts),
                "diagnosis": fault_dicts,
                "tool_calls": tool_results,
                "evidence": {
                    "telemetry_seq": telemetry_frame["seq"],
                    "telemetry_hash": self.telemetry_source.get_telemetry_hash(telemetry_frame["telemetry"]),
                    "supporting_metrics": [e for f in fault_dicts for e in f.get("supporting_evidence", [])],
                },
                "start_time_ms": int(self._start_time * 1000),
                "end_time_ms": int(time.time() * 1000),
                "duration_ms": int((time.time() - self._start_time) * 1000),
                "timestamp_ms": int(time.time() * 1000),
            }
            self._log_event(final_result)
            return final_result

        except Exception as e:
            self._crash_flag = True  # 异常路径标记：shutdown 事件携带 agent_fault 原因，看门狗仍判崩溃
            error_result = {
                "event_type": "agent_output",
                "task_id": self.task_id,
                "sat_id": self.sat_id,
                "status": "failed",
                "error": str(e),
                "error_type": type(e).__name__,
                "timestamp_ms": int(time.time() * 1000),
            }
            self._log_event(error_result)
            return error_result
        finally:
            self.stop()

    # ------------------------------------------------------------------
    # 辅助方法
    # ------------------------------------------------------------------
    def _plan_tools(self, faults: List[Dict]) -> List[Dict]:
        """根据诊断结论规划工具调用（固定决策逻辑，替代 LLM）。"""
        plan = []
        for fault in faults:
            code = fault.get("fault_code", "")
            if code == "NO_FAULT":
                continue
            elif code == "BCR_OPEN":
                plan.append({"tool": "tool_switch_bcr_backup", "args": {"action": "switch"}})
            elif code in ("SA_PARTIAL_OPEN", "SA_BRANCH_SHORT"):
                plan.append({"tool": "tool_switch_sa_branch", "args": {"branch": "A", "action": "off"}})
            elif code in ("BUS_OPEN", "BUS_SHORT"):
                plan.append({"tool": "tool_switch_bus_backup", "args": {"action": "switch"}})
            elif code == "BATTERY_LOW_SOC":
                plan.append({"tool": "tool_get_telemetry", "args": {"sensor": "battery"}})
            elif code == "OBC_OVERHEAT":
                plan.append({"tool": "tool_get_telemetry", "args": {"sensor": "temp_obc"}})

        # 如果没有规划任何工具，默认读一次遥测
        if not plan:
            plan.append({"tool": "tool_get_telemetry", "args": {"sensor": "all"}})
        return plan

    def _generate_conclusion(self, faults: List[Dict], tool_results: List[Dict]) -> str:
        """生成结论文本。"""
        fault_codes = [f["fault_code"] for f in faults if f["fault_code"] != "NO_FAULT"]
        if not fault_codes:
            return "系统正常，无故障"
        successful_tools = [r["tool_name"] for r in tool_results if r.get("ok")]
        return f"检测到故障: {', '.join(fault_codes)}; 已执行处置: {', '.join(successful_tools) if successful_tools else '无'}"

    def _calculate_confidence(self, faults: List[Dict]) -> float:
        """计算整体置信度（取所有故障置信度的平均值）。"""
        confs = [f.get("confidence", 0.5) for f in faults]
        return round(sum(confs) / len(confs), 4) if confs else 0.5

    def get_event_log(self) -> List[Dict]:
        return self._event_log

    def get_tool_call_history(self) -> List[Dict]:
        return self._tool_call_history
