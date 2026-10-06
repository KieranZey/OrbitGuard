# -*- coding: utf-8 -*-
"""
DTM 检查器集合。

每个检查器独立维护内部状态，消费事件，输出检查结果。
基类 BaseChecker 定义统一接口。
"""
import time
from typing import Dict, Any, Optional, List
from abc import ABC, abstractmethod


class CheckResult:
    """单个检查器的检查结果。"""
    def __init__(self, issue_detected: bool, issue_type: str = "",
                 severity: str = "low", confidence_penalty: float = 0.0,
                 evidence: Optional[Dict] = None, description: str = ""):
        self.issue_detected = issue_detected
        self.issue_type = issue_type
        self.severity = severity
        self.confidence_penalty = confidence_penalty  # 对trust_score的扣减（0-1）
        self.evidence = evidence or {}
        self.description = description

    def to_dict(self) -> Dict[str, Any]:
        return {
            "issue_detected": self.issue_detected,
            "issue_type": self.issue_type,
            "severity": self.severity,
            "confidence_penalty": self.confidence_penalty,
            "evidence": self.evidence,
            "description": self.description,
        }


class BaseChecker(ABC):
    """检查器基类。"""

    def __init__(self, config: Optional[Dict] = None):
        self.config = config or {}
        self._events_processed = 0

    @abstractmethod
    def process_event(self, event: Dict[str, Any]) -> Optional[CheckResult]:
        """处理一个事件，返回检查结果（无问题时返回None）。"""
        pass

    @abstractmethod
    def get_state(self) -> Dict[str, Any]:
        """获取检查器内部状态。"""
        pass

    def reset(self):
        """重置检查器状态。"""
        self._events_processed = 0


class ConclusionConsistencyChecker(BaseChecker):
    """
    结论一致性检查器。
    验证诊断结论是否有遥测证据支持。
    例如：诊断为 BCR_OPEN 但 bcr_current 不为 0 → 结论与证据不一致。
    """
    # 故障码到遥测验证规则的映射
    FAULT_VERIFICATION_RULES = {
        "BCR_OPEN": {"metric": "bcr_current", "operator": "approx_zero", "tolerance": 0.1},
        "SA_BRANCH_SHORT": {"metric": "sa_current", "operator": "high", "threshold": 5.0},
        "BATTERY_LOW": {"metric": "battery_soc", "operator": "below", "threshold": 20.0},
        "OVERHEAT": {"metric": "temp_obc", "operator": "above", "threshold": 60.0},
        "OVERPOWER": {"metric": "power_consumption_w", "operator": "above", "threshold": 200.0},
    }

    def __init__(self, config: Optional[Dict] = None):
        super().__init__(config)
        self.last_telemetry: Optional[Dict] = None
        self.last_diagnosis: Optional[Dict] = None
        self._inconsistency_found = False

    def process_event(self, event: Dict[str, Any]) -> Optional[CheckResult]:
        self._events_processed += 1
        etype = event.get("event_type")

        if etype == "telemetry":
            self.last_telemetry = event.get("telemetry", {})

        elif etype == "diagnosis":
            faults = event.get("faults", [])
            for fault in faults:
                fault_code = fault.get("fault_code", "")
                if fault_code == "NO_FAULT":
                    continue
                # 验证结论是否有遥测证据支持
                if fault_code in self.FAULT_VERIFICATION_RULES and self.last_telemetry:
                    rule = self.FAULT_VERIFICATION_RULES[fault_code]
                    metric = rule["metric"]
                    actual_value = self.last_telemetry.get(metric)
                    if actual_value is None:
                        continue
                    consistent = self._check_value(actual_value, rule)
                    if not consistent and not self._inconsistency_found:
                        self._inconsistency_found = True
                        return CheckResult(
                            issue_detected=True,
                            issue_type="CONCLUSION_EVIDENCE_MISMATCH",
                            severity="high",
                            confidence_penalty=0.3,
                            evidence={"fault_code": fault_code,
                                      "metric": metric,
                                      "actual_value": actual_value,
                                      "expected_rule": rule},
                            description=f"诊断为 {fault_code} 但 {metric}={actual_value} 不支持该结论",
                        )
        return None

    def _check_value(self, value: float, rule: Dict) -> bool:
        """根据规则检查值是否一致。"""
        op = rule["operator"]
        if op == "approx_zero":
            return abs(value) < rule.get("tolerance", 0.1)
        elif op == "high":
            return value > rule["threshold"]
        elif op == "above":
            return value > rule["threshold"]
        elif op == "below":
            return value < rule["threshold"]
        return True

    def get_state(self) -> Dict[str, Any]:
        return {
            "has_telemetry": self.last_telemetry is not None,
            "inconsistency_found": self._inconsistency_found,
        }

    def reset(self):
        super().reset()
        self.last_telemetry = None
        self.last_diagnosis = None
        self._inconsistency_found = False


class ConfidenceCalibrationChecker(BaseChecker):
    """
    置信度校准检查器。
    检查置信度是否与证据强度匹配。
    证据少但 confidence=0.99 → 置信度虚高。
    """
    def __init__(self, config: Optional[Dict] = None):
        super().__init__(config)
        self.min_evidence_for_high_conf = self.config.get("min_evidence_for_high_conf", 2)
        self.high_conf_threshold = self.config.get("high_conf_threshold", 0.90)
        self._inflated_found = False

    def process_event(self, event: Dict[str, Any]) -> Optional[CheckResult]:
        self._events_processed += 1
        etype = event.get("event_type")

        if etype == "diagnosis":
            faults = event.get("faults", [])
            for fault in faults:
                # NO_FAULT 是正常诊断，不参与置信度校准检查（避免误报）
                if fault.get("fault_code") == "NO_FAULT":
                    continue
                confidence = fault.get("confidence", 0.0)
                evidence = fault.get("supporting_evidence", [])
                evidence_count = len(evidence)

                # 高置信度但证据不足 → 虚高
                if (confidence >= self.high_conf_threshold
                        and evidence_count < self.min_evidence_for_high_conf
                        and not self._inflated_found):
                    self._inflated_found = True
                    return CheckResult(
                        issue_detected=True,
                        issue_type="INFLATED_CONFIDENCE",
                        severity="medium",
                        confidence_penalty=0.2,
                        evidence={"confidence": confidence,
                                  "evidence_count": evidence_count,
                                  "min_evidence_required": self.min_evidence_for_high_conf,
                                  "high_conf_threshold": self.high_conf_threshold},
                        description=f"置信度 {confidence:.2f} 但仅 {evidence_count} 条证据，疑似虚高",
                    )
        return None

    def get_state(self) -> Dict[str, Any]:
        return {
            "high_conf_threshold": self.high_conf_threshold,
            "min_evidence_for_high_conf": self.min_evidence_for_high_conf,
            "inflated_found": self._inflated_found,
        }

    def reset(self):
        super().reset()
        self._inflated_found = False


class EvidenceCompletenessChecker(BaseChecker):
    """
    证据完整性检查器。
    检查支持证据是否为空或数量不足。
    有结论但 supporting_evidence 为空 → 证据缺失。
    """
    def __init__(self, config: Optional[Dict] = None):
        super().__init__(config)
        self.min_evidence_count = self.config.get("min_evidence_count", 1)
        self._missing_found = False

    def process_event(self, event: Dict[str, Any]) -> Optional[CheckResult]:
        self._events_processed += 1
        if event.get("event_type") == "diagnosis":
            faults = event.get("faults", [])
            for fault in faults:
                if fault.get("fault_code") == "NO_FAULT":
                    continue
                evidence = fault.get("supporting_evidence", [])
                if len(evidence) < self.min_evidence_count and not self._missing_found:
                    self._missing_found = True
                    return CheckResult(
                        issue_detected=True,
                        issue_type="MISSING_EVIDENCE",
                        severity="medium",
                        confidence_penalty=0.25,
                        evidence={"fault_code": fault.get("fault_code"),
                                  "evidence_count": len(evidence),
                                  "min_required": self.min_evidence_count},
                        description=f"诊断为 {fault.get('fault_code')} 但支持证据不足（{len(evidence)}条）",
                    )
        return None

    def get_state(self) -> Dict[str, Any]:
        return {
            "min_evidence_count": self.min_evidence_count,
            "missing_found": self._missing_found,
        }

    def reset(self):
        super().reset()
        self._missing_found = False


class MultiSourceConflictChecker(BaseChecker):
    """
    多源矛盾检查器。
    检查多传感器数据是否互相矛盾。
    例如：bus_voltage 正常（28V）但 bus_current 为 0 → 数据矛盾。
    """
    # 矛盾检测规则：(metric_a, metric_b, condition)
    CONFLICT_RULES = [
        {
            "name": "voltage_current_contradiction",
            "metrics": ["bus_voltage", "bus_current"],
            "check": lambda t: abs(t.get("bus_voltage", 0) - 28) < 2 and t.get("bus_current", 1) < 0.1,
            "description": "母线电压正常但电流为0",
        },
        {
            "name": "power_calculation_contradiction",
            "metrics": ["bus_voltage", "bus_current", "power_consumption_w"],
            "check": lambda t: (abs(t.get("bus_voltage", 0) * t.get("bus_current", 0)
                                     - t.get("power_consumption_w", 0)) > 50
                                and t.get("power_consumption_w", 0) > 0),
            "description": "功率计算值与上报值差异过大",
        },
        {
            "name": "temperature_sensor_contradiction",
            "metrics": ["temp_battery", "temp_obc"],
            "check": lambda t: abs(t.get("temp_battery", 0) - t.get("temp_obc", 0)) > 40,
            "description": "电池温度与OBC温度差异异常（>40°C）",
        },
    ]

    def __init__(self, config: Optional[Dict] = None):
        super().__init__(config)
        self.last_telemetry: Optional[Dict] = None
        self._conflict_found = False

    def process_event(self, event: Dict[str, Any]) -> Optional[CheckResult]:
        self._events_processed += 1
        if event.get("event_type") == "telemetry":
            telemetry = event.get("telemetry", {})
            self.last_telemetry = telemetry

            for rule in self.CONFLICT_RULES:
                # 确保所有指标都存在
                if all(m in telemetry for m in rule["metrics"]):
                    try:
                        if rule["check"](telemetry) and not self._conflict_found:
                            self._conflict_found = True
                            return CheckResult(
                                issue_detected=True,
                                issue_type="MULTI_SOURCE_CONFLICT",
                                severity="high",
                                confidence_penalty=0.35,
                                evidence={"rule": rule["name"],
                                          "description": rule["description"],
                                          "values": {m: telemetry.get(m) for m in rule["metrics"]}},
                                description=f"多源数据矛盾：{rule['description']}",
                            )
                    except (TypeError, ZeroDivisionError):
                        continue
        return None

    def get_state(self) -> Dict[str, Any]:
        return {
            "has_telemetry": self.last_telemetry is not None,
            "conflict_found": self._conflict_found,
        }

    def reset(self):
        super().reset()
        self.last_telemetry = None
        self._conflict_found = False
