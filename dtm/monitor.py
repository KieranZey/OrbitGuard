# -*- coding: utf-8 -*-
"""
DTM 主监护器。

统一管理4个检查器，消费事件流，输出统一的 DTMResult。
计算 trust_score（0-1）和 risk_label（low/medium/high/critical）。
"""
import time
import json
from typing import Dict, Any, Optional, List
from .checkers import (
    BaseChecker,
    CheckResult,
    ConclusionConsistencyChecker,
    ConfidenceCalibrationChecker,
    EvidenceCompletenessChecker,
    MultiSourceConflictChecker,
)


class DTMResult:
    """
    DTM 统一输出结果。
    供 Gateway 汇合判定使用。
    """
    def __init__(self, run_id: str = "", task_id: str = "", sat_id: str = ""):
        self.monitor_type = "DTM"
        self.run_id = run_id
        self.task_id = task_id
        self.sat_id = sat_id
        self.trust_score = 1.0  # 0-1，1表示完全可信
        self.risk_label = "low"  # low / medium / high / critical
        self.issues: List[Dict] = []
        self.checker_states: Dict[str, Dict] = {}
        self.timestamp_ms = int(time.time() * 1000)

    def add_issue(self, result: CheckResult, checker_name: str):
        """添加一个检测到的问题，并更新trust_score。"""
        self.issues.append({
            "checker": checker_name,
            "issue_type": result.issue_type,
            "severity": result.severity,
            "confidence_penalty": result.confidence_penalty,
            "evidence": result.evidence,
            "description": result.description,
        })
        # 扣减trust_score
        self.trust_score = max(0.0, self.trust_score - result.confidence_penalty)
        # 更新risk_label
        self._update_risk_label()

    def _update_risk_label(self):
        """根据trust_score和问题严重程度更新risk_label。"""
        if self.trust_score >= 0.8:
            self.risk_label = "low"
        elif self.trust_score >= 0.6:
            self.risk_label = "medium"
        elif self.trust_score >= 0.4:
            self.risk_label = "high"
        else:
            self.risk_label = "critical"
        # 如果有critical级别的问题，直接升档
        for issue in self.issues:
            if issue["severity"] == "critical":
                self.risk_label = "critical"
                break

    def to_dict(self) -> Dict[str, Any]:
        return {
            "monitor_type": self.monitor_type,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "sat_id": self.sat_id,
            "trust_score": round(self.trust_score, 4),
            "risk_label": self.risk_label,
            "issues": self.issues,
            "checker_states": self.checker_states,
            "timestamp_ms": self.timestamp_ms,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)


class DecisionTrustMonitor:
    """
    输出可信度监护器（DTM）。

    统一管理4个检查器：
    - ConclusionConsistencyChecker：结论一致性检查
    - ConfidenceCalibrationChecker：置信度校准检查
    - EvidenceCompletenessChecker：证据完整性检查
    - MultiSourceConflictChecker：多源矛盾检查

    使用方式：
        dtm = DecisionTrustMonitor(config)
        for event in event_stream:
            result = dtm.process_event(event)
            if result and result.risk_label != "low":
                # 处理可信度问题
                pass
    """

    def __init__(self, config: Optional[Dict] = None):
        self.config = config or {}
        dtm_config = self.config.get("dtm", {})

        # 初始化4个检查器
        self.checkers: Dict[str, BaseChecker] = {
            "conclusion_consistency": ConclusionConsistencyChecker(dtm_config.get("conclusion_consistency", {})),
            "confidence_calibration": ConfidenceCalibrationChecker(dtm_config.get("confidence_calibration", {})),
            "evidence_completeness": EvidenceCompletenessChecker(dtm_config.get("evidence_completeness", {})),
            "multi_source_conflict": MultiSourceConflictChecker(dtm_config.get("multi_source_conflict", {})),
        }

        self._run_id = ""
        self._task_id = ""
        self._sat_id = ""
        self._event_count = 0
        self._last_result: Optional[DTMResult] = None
        self._cumulative_result: Optional[DTMResult] = None  # 累积式结果（P0修复：检查器结果不被覆盖）

    def set_context(self, run_id: str = "", task_id: str = "", sat_id: str = ""):
        """设置运行上下文。"""
        if run_id:
            self._run_id = run_id
        if task_id:
            self._task_id = task_id
        if sat_id:
            self._sat_id = sat_id

    def process_event(self, event: Dict[str, Any]) -> DTMResult:
        """
        处理一个事件，返回 DTMResult。
        DTM 总是返回结果（即使无问题，trust_score=1.0）。
        """
        self._event_count += 1

        # 从事件中提取上下文
        if not self._run_id and "run_id" in event:
            self._run_id = event["run_id"]
        if not self._task_id and "task_id" in event:
            self._task_id = event["task_id"]
        if not self._sat_id and "sat_id" in event:
            self._sat_id = event["sat_id"]

        # 累积式结果：持续追加所有检测到的问题，不被后续无问题事件覆盖
        if self._cumulative_result is None:
            self._cumulative_result = DTMResult(
                run_id=self._run_id, task_id=self._task_id, sat_id=self._sat_id,
            )

        for name, checker in self.checkers.items():
            check = checker.process_event(event)
            if check and check.issue_detected:
                self._cumulative_result.add_issue(check, name)
            self._cumulative_result.checker_states[name] = checker.get_state()

        self._cumulative_result.timestamp_ms = int(time.time() * 1000)
        self._last_result = self._cumulative_result
        return self._cumulative_result

    def process_events(self, events: List[Dict[str, Any]]) -> List[DTMResult]:
        """批量处理事件流，返回所有检测到问题的结果。"""
        results = []
        for event in events:
            result = self.process_event(event)
            if result.issues:
                results.append(result)
        return results

    def get_last_result(self) -> Optional[DTMResult]:
        """获取最近一次检测结果。"""
        return self._last_result

    def get_summary(self) -> Dict[str, Any]:
        """获取DTM运行摘要。"""
        return {
            "events_processed": self._event_count,
            "run_id": self._run_id,
            "task_id": self._task_id,
            "checkers": {name: c.get_state() for name, c in self.checkers.items()},
            "last_result": self._last_result.to_dict() if self._last_result else None,
        }

    def reset(self):
        """重置所有检查器状态。"""
        for checker in self.checkers.values():
            checker.reset()
        self._event_count = 0
        self._last_result = None
        self._cumulative_result = None
