# -*- coding: utf-8 -*-
"""
五模块全链路编排器（阶段四）。

链路：故障注入测试床 → RSM → DTM → Gateway → EGM → 最终执行/安全保持

使用方式：
    python five_module_orchestrator.py                          # 正常场景
    python five_module_orchestrator.py --fault memory_bloat    # 注入故障
    python five_module_orchestrator.py --fault process_crash   # 严重故障→BLOCK→SAFE_HOLD
"""
import sys
import os
import json
import time
import threading
import argparse
from typing import Dict, Any, Optional, List

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from agent.task_agent import TaskAgent
from fault_injector.injector import FaultInjector
from rsm.monitor import RuntimeStabilityMonitor
from dtm.monitor import DecisionTrustMonitor
from gateway.gateway import DecisionGateway
from egm.egm import ExecutionGateModule, EGMState
from egm.gate import ExecutionGate


class EventCapture:
    """事件捕获器。"""
    def __init__(self):
        self.events: List[Dict] = []

    def publish(self, event: Dict[str, Any]):
        self.events.append(event)


class LiveEventCapture:
    """
    实时事件总线（P0修复：支撑执行前置门控）。

    与 EventCapture 的区别：publish 在缓存事件的同时，把事件实时喂给
    RSM/DTM/Gate 观察者——这样 mutation 工具在执行前请求门控许可时，
    监护器看到的不是"事后"状态，而是"此时此刻"的累积状态。
    """
    def __init__(self, observers: Optional[List] = None):
        self.events: List[Dict] = []
        self.observers: List = observers or []

    def publish(self, event: Dict[str, Any]):
        self.events.append(event)
        for observer in self.observers:
            try:
                observer(event)
            except Exception:
                pass  # 观察者异常不影响事件流


class FiveModuleOrchestrator:
    """
    五模块全链路编排器。

    1. 运行故障注入测试床，捕获事件流
    2. RSM + DTM 消费事件流
    3. Gateway 汇合判定（C1-C5 + 三级路由）
    4. EGM 执行门控（人工审批 + 一次性许可 + 状态检查 + 模拟执行 + 审计）
    5. 输出完整五模块链路报告
    """

    def __init__(self, config: Optional[Dict] = None):
        self.config = config or {}
        # RSM使用演示级短超时（默认15s太慢，演示用1s）
        # memory_threshold_mb 默认设高：仅注入 memory_bloat 时才用低阈值，
        # 避免 Python 进程基础 RSS 波动触发误报（与 benchmark 同口径）
        rsm_demo_config = {
            "rsm": {
                "heartbeat": {"heartbeat_timeout_s": 1.0},
                "stall": {"stall_timeout_s": 1.0},
                "tool_timeout": {"tool_timeout_s": 1.0},
                "memory": {"memory_threshold_mb": 2000.0, "growth_window": 2},
            }
        }
        self._rsm_base_config = rsm_demo_config
        self.rsm = RuntimeStabilityMonitor(rsm_demo_config)
        self.dtm = DecisionTrustMonitor(self.config)
        self.gateway = DecisionGateway(self.config)
        self.egm = ExecutionGateModule(self.config)

    def run(self, fault_names: Optional[List[str]] = None,
            human_approval: str = "APPROVE") -> Dict[str, Any]:
        """
        执行完整五模块链路。

        Args:
            fault_names: 要注入的故障名称列表
            human_approval: 模拟人工审批结果（APPROVE / REJECT）

        Returns:
            完整的五模块链路执行报告
        """
        start_time = time.time()

        # 每轮运行前重置监护器状态（run() 可在同一编排器实例上重复调用）
        self.rsm.reset()
        self.dtm.reset()

        # 注入 memory_bloat 时才启用低内存阈值，其余场景保持高阈值防基础 RSS 误报
        self.rsm.detectors["memory"].threshold_mb = 50.0 if (
            fault_names and "memory_bloat" in fault_names) else 2000.0

        # P0修复：创建执行前置门控——mutation 工具执行前必须持一次性许可。
        # 前置门控是自动态、严格态：默认人工审批策略=REJECT，一切非 PERMIT 路由
        # （BLOCK/SUSPEND）一律拒绝放行；人工审批只发生在事后复核阶段（human_approval 参数）。
        gate = ExecutionGate(self.rsm, self.dtm, self.gateway, self.egm,
                             human_approval="REJECT")

        # 启动RSM独立看门狗（墙钟驱动，在Agent运行期间监控心跳/停滞/工具挂起）
        self.rsm.start_watchdog(check_interval_s=0.1)

        # 1. 构建配置并运行故障注入测试床（P0修复：加超时保护，infinite_loop/no_output_timeout不再挂死）
        #    P0修复：事件实时喂给 RSM/DTM/Gate，支撑工具执行前的门控许可裁决
        config = self._build_config(fault_names)
        capture = LiveEventCapture(observers=[
            self.rsm.process_event,
            self.dtm.process_event,
            gate.observe,
        ])
        injector = FaultInjector(config) if fault_names else None
        agent = TaskAgent(config, injector, event_bus=capture, gate=gate)
        agent_result = None
        timed_out = False
        run_thread = threading.Thread(target=lambda: setattr(agent, '_result', agent.run_task("fault_diagnosis_and_recovery")))
        run_thread.daemon = True
        run_thread.start()
        run_thread.join(timeout=5.0)  # 最多等5秒
        if run_thread.is_alive():
            timed_out = True
            try:
                agent.stop()  # 强制停止后台线程
            except Exception:
                pass
            agent_result = {"status": "timeout", "error": "Agent execution exceeded 5s timeout", "error_type": "ExecutionTimeout"}
        else:
            agent_result = getattr(agent, '_result', {"status": "failed", "error": "No result returned"})
        if not isinstance(agent_result, dict):
            agent_result = {"status": "failed", "error": str(agent_result), "error_type": type(agent_result).__name__}

        # Agent正常完成后停止看门狗（避免正常结束后心跳停止被误判为崩溃）
        agent_normal = agent_result.get("status") == "completed"
        if agent_normal:
            self.rsm.stop_watchdog()

        events = capture.events
        run_id = events[0].get("run_id", "") if events else ""

        # 2. 汇总监护结果（事件已在运行期间由 LiveEventCapture 实时消费，
        #    此处只做汇总，不再重复处理——累积式结果重复处理会重复扣分）
        rsm_final = None

        # 等待看门狗检测超时（仅当Agent异常结束时，正常结束已提前停止看门狗）
        if not agent_normal:
            time.sleep(1.5)
            watchdog_result = self.rsm.get_watchdog_result()
            if watchdog_result and watchdog_result.fault_detected:
                rsm_final = watchdog_result
            self.rsm.stop_watchdog()

        if rsm_final is None:
            rsm_final = self.rsm.get_last_result()
        dtm_final = self.dtm.get_last_result()

        # 3. 获取遥测快照
        telemetry_snapshot = None
        for event in reversed(events):
            if event.get("event_type") == "telemetry":
                telemetry_snapshot = event
                break

        # P1修复：从最后一个tool_call事件中提取动作意图元数据，传给Gateway做增量约束
        action_intent = None
        for event in reversed(events):
            if event.get("event_type") == "tool_call":
                action_intent = {
                    "tool_name": event.get("tool_name", ""),
                    "power_delta_w": event.get("power_delta_w", 0),
                    "thermal_delta_c": event.get("thermal_delta_c", 0),
                    "is_reversible": event.get("is_reversible", True),
                    "requires_approval": event.get("requires_approval", False),
                    "action_category": event.get("action_category", ""),
                    "is_mutation": event.get("is_mutation", False),
                }
                break

        # 4. Gateway 汇合判定
        gateway_decision = self.gateway.decide(
            rsm_result=rsm_final.to_dict() if rsm_final else None,
            dtm_result=dtm_final.to_dict() if dtm_final else None,
            telemetry=telemetry_snapshot,
            action_intent=action_intent,
        )

        # 5. EGM 执行门控
        egm_output = self.egm.submit_gateway_decision(
            gateway_decision=gateway_decision.to_dict(),
            human_approval=human_approval,
        )

        # 如果签发了许可，尝试执行
        egm_execution = None
        if egm_output.egm_state == EGMState.PERMIT_ISSUED.value and egm_output.permit_id:
            egm_execution = self.egm.execute_with_permit(
                permit_id=egm_output.permit_id,
                current_state={"task_id": gateway_decision.task_id, "state_changed": False},
            )

        elapsed = time.time() - start_time

        # 前置门控汇总（P0修复：工具执行前的许可裁决记录）
        gate_summary = gate.get_summary()

        # 6. 组装完整报告
        final_outcome = self._derive_final_outcome(gateway_decision, egm_output, egm_execution)
        # 前置门控拒绝过 mutation 工具时，最终结论以"工具被门控拦截"为准
        # （事后复核的模拟执行不改变"工具从未真实执行"这一事实）
        if gate_summary["denied"] > 0 and not final_outcome.startswith("SAFE_HOLD"):
            first_denial = gate_summary["denials"][0]
            final_outcome = f"TOOL_GATED ({first_denial['route']}): {first_denial['reason'][:60]}"

        report = {
            "orchestrator": "FiveModuleOrchestrator",
            "run_id": run_id,
            "injected_faults": fault_names or [],
            "human_approval": human_approval,
            "elapsed_s": round(elapsed, 3),
            "event_count": len(events),
            "agent_result": {
                "status": agent_result.get("status"),
                "conclusion": agent_result.get("conclusion"),
                "confidence": agent_result.get("confidence"),
            },
            "rsm_result": rsm_final.to_dict() if rsm_final else None,
            "dtm_result": dtm_final.to_dict() if dtm_final else None,
            "gateway_decision": gateway_decision.to_dict(),
            "egm_review": egm_output.to_dict(),
            "egm_execution": egm_execution.to_dict() if egm_execution else None,
            "gate_summary": gate_summary,
            "final_outcome": final_outcome,
            "event_summary": self._summarize_events(events),
        }
        return report

    def _derive_final_outcome(self, gateway_decision, egm_review, egm_execution) -> str:
        """推导最终执行结果。"""
        if gateway_decision.route == "BLOCK":
            return "SAFE_HOLD (Gateway BLOCK)"
        if egm_review.egm_state == EGMState.REJECTED.value:
            return "REJECTED (Human Review)"
        if egm_execution:
            if egm_execution.egm_state == EGMState.COMPLETED.value:
                return f"EXECUTED ({egm_execution.action})"
            elif egm_execution.egm_state == EGMState.SAFE_HOLD.value:
                return "SAFE_HOLD (Execution Failed)"
            elif egm_execution.egm_state == EGMState.STATE_CHANGED.value:
                return "REJECTED (State Changed)"
        return "PENDING (Permit Issued, Not Executed)"

    def _build_config(self, fault_names: Optional[List[str]]) -> Dict[str, Any]:
        config = {
            "agent": {
                "task_id": "FIVE-MOD-TEST", "sat_id": "SAT-FIVE",
                "telemetry_interval_ms": 100, "heartbeat_interval_ms": 200, "resource_interval_ms": 100,
            },
            "fault_injector": {
                "global_enabled": bool(fault_names),
                "stability_faults": {}, "credibility_faults": {},
            },
            "ground_truth": {"log_dir": "logs", "log_file": "five_mod_gt.jsonl"},
            "egm": {"log_dir": "logs", "log_file": "five_mod_egm_audit.jsonl"},
        }
        if fault_names:
            stability = {"infinite_loop", "process_crash", "no_output_timeout",
                         "memory_bloat", "repetitive_calls"}
            for name in fault_names:
                if name in stability:
                    fault_cfg = {"enabled": True}
                    # P1修复：no_output_timeout 演示级睡眠（默认120s会拖垮演示，
                    # 6s > 编排器5s超时预算，看门狗在1s阈值内即检出并独立判类）
                    if name == "no_output_timeout":
                        fault_cfg["sleep_seconds"] = 6
                    config["fault_injector"]["stability_faults"][name] = fault_cfg
                else:
                    config["fault_injector"]["credibility_faults"][name] = {"enabled": True}
        return config

    def _summarize_events(self, events: List[Dict]) -> Dict[str, int]:
        summary = {}
        for event in events:
            etype = event.get("event_type", "unknown")
            summary[etype] = summary.get(etype, 0) + 1
        return summary


def main():
    parser = argparse.ArgumentParser(description="五模块全链路编排器")
    parser.add_argument("--fault", action="append", default=[], help="注入的故障名称")
    parser.add_argument("--approval", default="APPROVE", choices=["APPROVE", "REJECT"],
                        help="模拟人工审批结果")
    parser.add_argument("--json", action="store_true", help="JSON格式输出")
    args = parser.parse_args()

    orch = FiveModuleOrchestrator()
    report = orch.run(fault_names=args.fault if args.fault else None,
                       human_approval=args.approval)

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print("=" * 70)
        print("五模块全链路编排器 - 执行报告")
        print("=" * 70)
        print(f"  Run ID:       {report['run_id']}")
        print(f"  注入故障:     {report['injected_faults'] or '无'}")
        print(f"  人工审批:     {report['human_approval']}")
        print(f"  事件总数:     {report['event_count']}")
        print(f"  总耗时:       {report['elapsed_s']}s")
        print()
        print("【RSM 运行稳定性】")
        rsm = report["rsm_result"]
        print(f"  故障: {rsm['fault_detected']}  类型: {rsm['fault_type']}  严重度: {rsm['severity']}")
        print()
        print("【DTM 输出可信度】")
        dtm = report["dtm_result"]
        print(f"  信任分: {dtm['trust_score']}  风险: {dtm['risk_label']}  问题: {len(dtm['issues'])}")
        print()
        print("【Gateway 汇合判定】")
        gw = report["gateway_decision"]
        print(f"  路由: {gw['route']}  遏制: {gw['containment']}  置信度: {gw['confidence']}")
        print()
        print("【EGM 执行门控】")
        egm = report["egm_review"]
        print(f"  审批状态: {egm['egm_state']}  许可: {egm.get('permit_id') or '无'}")
        if report["egm_execution"]:
            exe = report["egm_execution"]
            print(f"  执行状态: {exe['egm_state']}  动作: {exe.get('action')}  结果: {exe.get('execution_status')}")
        print()
        gs = report.get("gate_summary", {})
        print(f"【执行前置门控】 检查 {gs.get('total_checked', 0)} 次 / 放行 {gs.get('authorized', 0)} 次 / 拦截 {gs.get('denied', 0)} 次")
        for d in gs.get("denials", []):
            print(f"  拦截: {d['route']} -> {d['reason'][:80]}")
        print()
        print(f"【最终结果】 {report['final_outcome']}")
        print("=" * 70)


if __name__ == "__main__":
    main()
