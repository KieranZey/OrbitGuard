# -*- coding: utf-8 -*-
"""
模拟工具集。
真实环境中替换为星载工具执行器 (executor.py)。
每个工具对应一个硬件操作，返回结构化结果。
"""
import time
import hashlib
import json
from typing import Dict, Any, Callable, Optional


class ToolResult:
    """工具执行结果。"""
    def __init__(self, call_id: str, tool_name: str, ok: bool,
                 observation: str = "", error_message: str = "",
                 data: Optional[Dict] = None, duration_ms: int = 0):
        self.call_id = call_id
        self.tool_name = tool_name
        self.ok = ok
        self.observation = observation
        self.error_message = error_message
        self.data = data or {}
        self.duration_ms = duration_ms
        self.timestamp_ms = int(time.time() * 1000)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "call_id": self.call_id,
            "tool_name": self.tool_name,
            "ok": self.ok,
            "observation": self.observation,
            "error_message": self.error_message,
            "data": self.data,
            "duration_ms": self.duration_ms,
            "timestamp_ms": self.timestamp_ms,
        }


class ToolRegistry:
    """
    工具注册表（模拟 tool_manifest.py）。
    注册工具名 -> 处理函数的映射，提供统一调用接口。
    """

    # 工具风险等级定义（与安全沙箱 tool_manifest 对齐）
    # 动作意图元数据（供 Gateway action_intent_snapshot 提取）
    TOOL_MANIFEST = {
        "tool_get_telemetry": {
            "risk_level": "low",
            "mutation": False,
            "enabled": True,
            "description": "读取卫星遥测数据",
            "parameters": {"type": "object", "properties": {"sensor": {"type": "string"}}},
            "action_category": "PAYLOAD_OPERATION",
            "is_reversible": True,
            "requires_approval": False,
            "power_delta_w": 0.0,
            "thermal_delta_c": 0.0,
            "estimated_duration_s": 1.0,
        },
        "tool_switch_sa_branch": {
            "risk_level": "medium",
            "mutation": True,
            "enabled": True,
            "description": "切换太阳阵支路",
            "parameters": {"type": "object", "properties": {"branch": {"type": "string"}, "action": {"type": "string"}}},
            "action_category": "PAYLOAD_OPERATION",
            "is_reversible": True,
            "requires_approval": False,
            "power_delta_w": 5.0,
            "thermal_delta_c": 2.0,
            "estimated_duration_s": 60.0,
        },
        "tool_switch_bcr_backup": {
            "risk_level": "medium",
            "mutation": True,
            "enabled": True,
            "description": "切换BCR备份",
            "parameters": {"type": "object", "properties": {"action": {"type": "string"}}},
            "action_category": "PAYLOAD_OPERATION",
            "is_reversible": True,
            "requires_approval": False,
            "power_delta_w": 8.0,
            "thermal_delta_c": 3.0,
            "estimated_duration_s": 60.0,
        },
        "tool_switch_bus_backup": {
            "risk_level": "high",
            "mutation": True,
            "enabled": True,
            "requires_approval": True,
            "description": "切换母线备份（高风险，需审批）",
            "parameters": {"type": "object", "properties": {"action": {"type": "string"}}},
            "action_category": "PAYLOAD_OPERATION",
            "is_reversible": True,
            "power_delta_w": 12.0,
            "thermal_delta_c": 5.0,
            "estimated_duration_s": 120.0,
        },
    }

    def __init__(self):
        self._handlers: Dict[str, Callable] = {}
        self._call_count = 0
        self._register_default_tools()

    def _register_default_tools(self):
        """注册默认工具处理函数。"""
        self.register("tool_get_telemetry", self._handler_get_telemetry)
        self.register("tool_switch_sa_branch", self._handler_switch_sa_branch)
        self.register("tool_switch_bcr_backup", self._handler_switch_bcr_backup)
        self.register("tool_switch_bus_backup", self._handler_switch_bus_backup)

    def register(self, tool_name: str, handler: Callable):
        """注册工具处理函数。"""
        self._handlers[tool_name] = handler

    def is_registered(self, tool_name: str) -> bool:
        return tool_name in self._handlers

    def is_enabled(self, tool_name: str) -> bool:
        manifest = self.TOOL_MANIFEST.get(tool_name, {})
        return manifest.get("enabled", False)

    def get_risk_level(self, tool_name: str) -> str:
        return self.TOOL_MANIFEST.get(tool_name, {}).get("risk_level", "unknown")

    def call(self, tool_name: str, args: Optional[Dict] = None, call_id: Optional[str] = None) -> ToolResult:
        """
        调用工具（裸版，无安全检查 —— 与主应用 StarNet-Mind/executor.py 一致）。
        安全检查由故障注入框架的装饰器在外部叠加。
        """
        self._call_count += 1
        if call_id is None:
            call_id = f"CALL-{self._call_count:04d}-{hashlib.md5(str(time.time()).encode()).hexdigest()[:6]}"
        args = args or {}
        start = time.time()

        handler = self._handlers.get(tool_name)
        if handler is None:
            return ToolResult(call_id, tool_name, ok=False,
                              error_message=f"未注册的工具: {tool_name}",
                              duration_ms=int((time.time() - start) * 1000))

        try:
            result = handler(args)
            duration = int((time.time() - start) * 1000)
            if isinstance(result, ToolResult):
                result.call_id = call_id
                result.duration_ms = duration
                return result
            return ToolResult(call_id, tool_name, ok=True,
                              observation=str(result), data=result if isinstance(result, dict) else {},
                              duration_ms=duration)
        except Exception as e:
            duration = int((time.time() - start) * 1000)
            return ToolResult(call_id, tool_name, ok=False,
                              error_message=f"工具执行异常: {str(e)}",
                              duration_ms=duration)

    def get_call_count(self) -> int:
        return self._call_count

    # ===== 默认工具处理函数 =====

    def _handler_get_telemetry(self, args: Dict) -> Dict:
        """读取遥测（实际由 Agent 传入当前遥测帧，这里返回模拟数据）。"""
        sensor = args.get("sensor", "all")
        return {
            "sensor": sensor,
            "bus_voltage": 28.15,
            "bus_current": 5.2,
            "battery_soc": 84.5,
            "status": "ok",
        }

    def _handler_switch_sa_branch(self, args: Dict) -> Dict:
        branch = args.get("branch", "A")
        action = args.get("action", "on")
        time.sleep(0.05)  # 模拟硬件操作延迟
        return {"branch": branch, "action": action, "status": "switched", "new_state": action}

    def _handler_switch_bcr_backup(self, args: Dict) -> Dict:
        action = args.get("action", "switch")
        time.sleep(0.08)
        return {"action": action, "status": "switched", "active_bcr": "BACKUP"}

    def _handler_switch_bus_backup(self, args: Dict) -> Dict:
        action = args.get("action", "switch")
        time.sleep(0.1)
        return {"action": action, "status": "switched", "active_bus": "BACKUP", "warning": "high_risk_action"}
