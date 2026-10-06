# -*- coding: utf-8 -*-
"""
模拟故障诊断引擎。
真实环境中替换为 CLIPS 规则引擎 (clipsEngine.py)。
基于遥测数据做简单阈值判断，输出结构化诊断结果。
"""
import time
import hashlib
import json
from typing import Dict, Any, List, Optional


class FaultEvent:
    """故障事件数据结构。"""
    def __init__(self, fault_code: str, severity: str, description: str,
                 supporting_evidence: List[Dict], conflicting_evidence: List[Dict],
                 confidence: float, source: str = "rule_based"):
        self.fault_code = fault_code
        self.severity = severity  # low / medium / high / critical
        self.description = description
        self.supporting_evidence = supporting_evidence
        self.conflicting_evidence = conflicting_evidence
        self.confidence = confidence
        self.source = source
        self.timestamp_ms = int(time.time() * 1000)
        self.event_id = f"FAULT-{hashlib.md5(str(self.timestamp_ms).encode()).hexdigest()[:8]}"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "event_id": self.event_id,
            "fault_code": self.fault_code,
            "severity": self.severity,
            "description": self.description,
            "confidence": round(self.confidence, 4),
            "source": self.source,
            "timestamp_ms": self.timestamp_ms,
            "supporting_evidence": self.supporting_evidence,
            "conflicting_evidence": self.conflicting_evidence,
        }


class DiagnosisEngine:
    """
    基于规则的故障诊断引擎（模拟 CLIPS）。
    输入遥测数据，输出故障事件列表。
    """

    # 故障码定义
    FAULT_CODES = {
        "BCR_OPEN": "BCR支路开路",
        "BCR_SHORT": "BCR支路短路",
        "SA_PARTIAL_OPEN": "太阳阵部分开路",
        "SA_BRANCH_SHORT": "太阳阵支路短路",
        "BUS_OPEN": "母线开路",
        "BUS_SHORT": "母线短路",
        "BATTERY_LOW_SOC": "电池SOC过低",
        "OBC_OVERHEAT": "星务计算机过热",
        "NO_FAULT": "无故障",
    }

    def __init__(self):
        self._diagnosis_count = 0

    def diagnose(self, telemetry_frame: Dict[str, Any]) -> List[FaultEvent]:
        """
        对一帧遥测数据做故障诊断。
        返回故障事件列表（空列表表示无故障）。
        """
        self._diagnosis_count += 1
        tel = telemetry_frame.get("telemetry", {})
        faults = []

        # 规则1: BCR开路 —— BCR电流接近0但SA电流正常
        if tel.get("bcr_current", 999) < 0.1 and tel.get("sa_current", 0) > 1.0:
            faults.append(FaultEvent(
                fault_code="BCR_OPEN",
                severity="high",
                description="BCR支路电流接近0，太阳阵电流正常，判定BCR支路开路",
                supporting_evidence=[
                    {"metric": "bcr_current", "value": tel.get("bcr_current"), "threshold": "< 0.1A", "status": "violated"},
                    {"metric": "sa_current", "value": tel.get("sa_current"), "threshold": "> 1.0A", "status": "normal"},
                ],
                conflicting_evidence=[],
                confidence=0.92,
            ))

        # 规则2: 母线短路 —— 母线电压接近0但电流很大
        if tel.get("bus_voltage", 999) < 1.0 and tel.get("bus_current", 0) > 10.0:
            faults.append(FaultEvent(
                fault_code="BUS_SHORT",
                severity="critical",
                description="母线电压接近0且电流异常增大，判定母线短路",
                supporting_evidence=[
                    {"metric": "bus_voltage", "value": tel.get("bus_voltage"), "threshold": "< 1.0V", "status": "violated"},
                    {"metric": "bus_current", "value": tel.get("bus_current"), "threshold": "> 10.0A", "status": "violated"},
                ],
                conflicting_evidence=[],
                confidence=0.97,
            ))

        # 规则3: 电池SOC过低
        if tel.get("battery_soc", 100) < 20.0:
            faults.append(FaultEvent(
                fault_code="BATTERY_LOW_SOC",
                severity="medium",
                description=f"电池SOC={tel.get('battery_soc')}%低于20%阈值",
                supporting_evidence=[
                    {"metric": "battery_soc", "value": tel.get("battery_soc"), "threshold": "< 20%", "status": "violated"},
                ],
                conflicting_evidence=[],
                confidence=0.99,
            ))

        # 规则4: OBC过热
        if tel.get("temp_obc", 0) > 60.0:
            faults.append(FaultEvent(
                fault_code="OBC_OVERHEAT",
                severity="high",
                description=f"OBC温度={tel.get('temp_obc')}°C超过60°C阈值",
                supporting_evidence=[
                    {"metric": "temp_obc", "value": tel.get("temp_obc"), "threshold": "> 60°C", "status": "violated"},
                ],
                conflicting_evidence=[],
                confidence=0.95,
            ))

        # 无故障时返回 NO_FAULT 事件
        if not faults:
            faults.append(FaultEvent(
                fault_code="NO_FAULT",
                severity="low",
                description="遥测数据全部在正常范围内，无故障",
                supporting_evidence=[
                    {"metric": "bus_voltage", "value": tel.get("bus_voltage"), "threshold": "27-29V", "status": "normal"},
                    {"metric": "battery_soc", "value": tel.get("battery_soc"), "threshold": "> 20%", "status": "normal"},
                ],
                conflicting_evidence=[],
                confidence=0.99,
            ))

        return faults

    def get_diagnosis_count(self) -> int:
        return self._diagnosis_count
