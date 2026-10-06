# -*- coding: utf-8 -*-
"""
DTM（Decision Trust Monitor）输出可信度监护器。

消费Agent事件流，评估输出结果的可信度，检测4类可信性问题：
1. 结论一致性检查（ConclusionConsistencyChecker）：诊断结论是否有遥测证据支持
2. 置信度校准检查（ConfidenceCalibrationChecker）：置信度是否与证据强度匹配
3. 证据完整性检查（EvidenceCompletenessChecker）：支持证据是否为空或数量不足
4. 多源矛盾检查（MultiSourceConflictChecker）：多传感器数据是否互相矛盾

输出统一的 DTMResult，包含 trust_score（0-1）和 risk_label。
"""
from .monitor import DecisionTrustMonitor, DTMResult
from .checkers import (
    ConclusionConsistencyChecker,
    ConfidenceCalibrationChecker,
    EvidenceCompletenessChecker,
    MultiSourceConflictChecker,
)

__all__ = [
    "DecisionTrustMonitor",
    "DTMResult",
    "ConclusionConsistencyChecker",
    "ConfidenceCalibrationChecker",
    "EvidenceCompletenessChecker",
    "MultiSourceConflictChecker",
]
