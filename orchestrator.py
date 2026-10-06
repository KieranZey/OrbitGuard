# -*- coding: utf-8 -*-
"""
四模块全链路编排器（阶段三）。

链路：故障注入测试床 → RSM → DTM → Gateway → 输出决策

使用方式：
    python orchestrator.py                          # 正常场景
    python orchestrator.py --fault memory_bloat    # 注入指定故障
    python orchestrator.py --fault infinite_loop --fault wrong_diagnosis  # 多故障
"""
import sys
import os
import json
import time
import argparse
from typing import Dict, Any, Optional, List

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from agent.task_agent import TaskAgent
from fault_injector.injector import FaultInjector
from rsm.monitor import RuntimeStabilityMonitor
from dtm.monitor import DecisionTrustMonitor
from gateway.gateway import DecisionGateway


class EventCapture:
    """事件捕获器：替代事件总线，收集所有事件。"""
    def __init__(self):
        self.events: List[Dict] = []

    def publish(self, event: Dict[str, Any]):
        self.events.append(event)


class FourModuleOrchestrator:
    """
    四模块全链路编排器。

    1. 运行故障注入测试床（TaskAgent + FaultInjector），捕获事件流
    2. 将事件流喂给 RSM 和 DTM
    3. 收集 RSM/DTM 最终结果 + 遥测快照
    4. 传给 Gateway 进行汇合判定
    5. 输出完整决策报告
    """

    def __init__(self, config: Optional[Dict] = None):
        self.config = config or {}
        self.rsm = RuntimeStabilityMonitor(self.config)
        self.dtm = DecisionTrustMonitor(self.config)
        self.gateway = DecisionGateway(self.config)

    def run(self, fault_names: Optional[List[str]] = None) -> Dict[str, Any]:
        """
        执行完整四模块链路。

        Args:
            fault_names: 要注入的故障名称列表（None表示正常模式）

        Returns:
            完整的链路执行报告
        """
        start_time = time.time()

        # 1. 构建配置
        config = self._build_config(fault_names)

        # 2. 运行故障注入测试床，捕获事件
        capture = EventCapture()
        injector = FaultInjector(config) if fault_names else None
        agent = TaskAgent(config, injector, event_bus=capture)
        try:
            agent_result = agent.run_task("fault_diagnosis_and_recovery")
        except Exception as e:
            agent_result = {"status": "failed", "error": str(e), "error_type": type(e).__name__}

        events = capture.events
        run_id = events[0].get("run_id", "") if events else ""

        # 3. RSM + DTM 消费事件流
        rsm_final = None
        dtm_final = None
        for event in events:
            rsm_result = self.rsm.process_event(event)
            if rsm_result and rsm_result.fault_detected:
                rsm_final = rsm_result
            dtm_result = self.dtm.process_event(event)
            if dtm_result and dtm_result.issues:
                dtm_final = dtm_result

        # 如果没有检测到问题，取最后一次结果
        if rsm_final is None:
            rsm_final = self.rsm.get_last_result()
        if dtm_final is None:
            dtm_final = self.dtm.get_last_result()

        # 4. 获取遥测快照（最后一个telemetry事件）
        telemetry_snapshot = None
        for event in reversed(events):
            if event.get("event_type") == "telemetry":
                telemetry_snapshot = event
                break

        # 5. Gateway 汇合判定
        gateway_decision = self.gateway.decide(
            rsm_result=rsm_final.to_dict() if rsm_final else None,
            dtm_result=dtm_final.to_dict() if dtm_final else None,
            telemetry=telemetry_snapshot,
        )

        elapsed = time.time() - start_time

        # 6. 组装完整报告
        report = {
            "orchestrator": "FourModuleOrchestrator",
            "run_id": run_id,
            "injected_faults": fault_names or [],
            "elapsed_s": round(elapsed, 3),
            "event_count": len(events),
            "agent_result": {
                "status": agent_result.get("status"),
                "conclusion": agent_result.get("conclusion"),
                "confidence": agent_result.get("confidence"),
                "duration_ms": agent_result.get("duration_ms"),
            },
            "rsm_result": rsm_final.to_dict() if rsm_final else None,
            "dtm_result": dtm_final.to_dict() if dtm_final else None,
            "gateway_decision": gateway_decision.to_dict(),
            "event_summary": self._summarize_events(events),
        }
        return report

    def _build_config(self, fault_names: Optional[List[str]]) -> Dict[str, Any]:
        """根据故障名称构建配置。"""
        config = {
            "agent": {
                "task_id": "ORCH-TEST",
                "sat_id": "SAT-ORCH",
                "telemetry_interval_ms": 100,
                "heartbeat_interval_ms": 200,
            },
            "fault_injector": {
                "global_enabled": bool(fault_names),
                "stability_faults": {},
                "credibility_faults": {},
            },
            "ground_truth": {
                "log_dir": "logs",
                "log_file": "orchestrator_gt.jsonl",
            },
        }
        if fault_names:
            stability = {"infinite_loop", "process_crash", "no_output_timeout",
                         "memory_bloat", "repetitive_calls"}
            for name in fault_names:
                if name in stability:
                    config["fault_injector"]["stability_faults"][name] = {"enabled": True}
                else:
                    config["fault_injector"]["credibility_faults"][name] = {"enabled": True}
        return config

    def _summarize_events(self, events: List[Dict]) -> Dict[str, int]:
        """统计事件类型分布。"""
        summary = {}
        for event in events:
            etype = event.get("event_type", "unknown")
            summary[etype] = summary.get(etype, 0) + 1
        return summary


def main():
    parser = argparse.ArgumentParser(description="四模块全链路编排器")
    parser.add_argument("--fault", action="append", default=[],
                        help="注入的故障名称（可多次指定）")
    parser.add_argument("--json", action="store_true", help="以JSON格式输出")
    args = parser.parse_args()

    orchestrator = FourModuleOrchestrator()
    report = orchestrator.run(fault_names=args.fault if args.fault else None)

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print("=" * 70)
        print("四模块全链路编排器 - 执行报告")
        print("=" * 70)
        print(f"  Run ID:       {report['run_id']}")
        print(f"  注入故障:     {report['injected_faults'] or '无（正常模式）'}")
        print(f"  事件总数:     {report['event_count']}")
        print(f"  总耗时:       {report['elapsed_s']}s")
        print()
        print("【Agent 执行结果】")
        ar = report["agent_result"]
        print(f"  状态: {ar['status']}  结论: {ar.get('conclusion')}  置信度: {ar.get('confidence')}")
        print()
        print("【RSM 运行稳定性】")
        rsm = report["rsm_result"]
        if rsm:
            print(f"  故障检测: {rsm['fault_detected']}  类型: {rsm['fault_type']}  严重度: {rsm['severity']}")
            print(f"  问题数: {len(rsm['issues'])}")
        else:
            print("  无结果")
        print()
        print("【DTM 输出可信度】")
        dtm = report["dtm_result"]
        if dtm:
            print(f"  信任分: {dtm['trust_score']}  风险标签: {dtm['risk_label']}  问题数: {len(dtm['issues'])}")
        else:
            print("  无结果")
        print()
        print("【Gateway 汇合判定】")
        gw = report["gateway_decision"]
        print(f"  路由: {gw['route']}  遏制: {gw['containment']}  置信度: {gw['confidence']}")
        print(f"  理由:")
        for reason in gw["reasons"]:
            print(f"    - {reason}")
        print(f"  物理约束:")
        for cid, cr in gw["physical_checks"].items():
            status = "PASS" if cr["passed"] else ("FAIL" if cr["passed"] is False else "SKIP")
            print(f"    {cid}: {status}")
        print()
        print("【事件分布】")
        for etype, count in report["event_summary"].items():
            print(f"  {etype}: {count}")
        print("=" * 70)


if __name__ == "__main__":
    main()
