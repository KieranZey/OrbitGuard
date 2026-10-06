# -*- coding: utf-8 -*-
"""
Gateway 汇合判定模块。

接收 RSMResult + DTMResult + 物理状态快照，执行：
1. C1-C5 物理约束检查（功率、热、碰撞、电池、辐射）
2. 三级路由判定（PERMIT / SUSPEND / BLOCK）
3. 输出 GatewayDecision，供 EGM 执行门控使用。
"""
from .gateway import GatewayDecision, DecisionGateway

__all__ = ["GatewayDecision", "DecisionGateway"]
