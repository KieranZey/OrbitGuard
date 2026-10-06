# -*- coding: utf-8 -*-
"""
RSM（Runtime Stability Monitor）运行稳定性监护器。

消费Agent事件流，检测5类稳定性异常：
1. 心跳检测（HeartbeatMonitor）：超过阈值无心跳 → 进程崩溃
2. 无进展检测（StallMonitor）：uptime增长但step不变 → 死循环/活锁
3. 工具超时检测（ToolTimeoutMonitor）：tool_call后超时无tool_result → 工具卡住
4. 内存检测（MemoryMonitor）：内存持续增长超阈值 → 内存泄漏
5. 重复调用检测（RepetitiveCallMonitor）：同一工具连续调用超N次 → 重复循环

输出统一的 RSMResult，供 Gateway 汇合判定使用。
"""
from .monitor import RuntimeStabilityMonitor, RSMResult
from .detectors import (
    HeartbeatMonitor,
    StallMonitor,
    ToolTimeoutMonitor,
    MemoryMonitor,
    RepetitiveCallMonitor,
)

__all__ = [
    "RuntimeStabilityMonitor",
    "RSMResult",
    "HeartbeatMonitor",
    "StallMonitor",
    "ToolTimeoutMonitor",
    "MemoryMonitor",
    "RepetitiveCallMonitor",
]
