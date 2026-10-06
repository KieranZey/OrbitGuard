# -*- coding: utf-8 -*-
"""
故障注入测试床 - 主入口

用法:
  python main.py                          # 正常模式（无故障）
  python main.py --fault infinite_loop    # 激活指定故障
  python main.py --fault process_crash    # 进程崩溃故障
  python main.py --fault wrong_diagnosis  # 诊断结论错误故障
  python main.py --fault memory_bloat,wrong_diagnosis  # 多故障叠加（逗号分隔）
  python main.py --list-faults            # 列出所有可用故障

支持的故障:
  稳定性: infinite_loop, process_crash, no_output_timeout, memory_bloat, repetitive_calls
  可信性: wrong_diagnosis, inflated_confidence, missing_evidence, contradictory_sensors
"""
import sys
import os
import json
import argparse
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from agent.task_agent import TaskAgent
from fault_injector.injector import FaultInjector


ALL_FAULTS = {
    "infinite_loop": ("stability", "死循环/活锁：进程存活但任务无进展"),
    "process_crash": ("stability", "进程崩溃：运行中未捕获异常"),
    "no_output_timeout": ("stability", "无输出/超时：函数卡住不返回"),
    "memory_bloat": ("stability", "内存暴涨：阶梯式内存泄漏"),
    "repetitive_calls": ("stability", "重复调用循环：连续相同工具调用"),
    "wrong_diagnosis": ("credibility", "诊断结论错误：正确结论被替换"),
    "inflated_confidence": ("credibility", "置信度虚高：错误结论报高置信度"),
    "missing_evidence": ("credibility", "证据缺失：有结论但支持证据为空"),
    "contradictory_sensors": ("credibility", "多源数据矛盾：传感器输出冲突"),
}


def load_config() -> dict:
    config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
    with open(config_path, "r", encoding="utf-8") as f:
        return json.load(f)


def enable_fault(config: dict, fault_name: str) -> dict:
    """在配置中启用指定故障。"""
    layer, _ = ALL_FAULTS.get(fault_name, (None, None))
    if layer is None:
        print(f"未知故障: {fault_name}")
        print(f"可用故障: {', '.join(ALL_FAULTS.keys())}")
        sys.exit(1)

    layer_key = "stability_faults" if layer == "stability" else "credibility_faults"
    config["fault_injector"]["global_enabled"] = True
    if fault_name in config["fault_injector"][layer_key]:
        config["fault_injector"][layer_key][fault_name]["enabled"] = True
    else:
        config["fault_injector"][layer_key][fault_name] = {"enabled": True}
    return config


def main():
    parser = argparse.ArgumentParser(description="故障注入测试床")
    parser.add_argument("--fault", type=str, default=None,
                        help="激活指定故障（如 infinite_loop, process_crash, wrong_diagnosis）")
    parser.add_argument("--list-faults", action="store_true", help="列出所有可用故障")
    parser.add_argument("--config", type=str, default=None, help="指定配置文件路径")
    args = parser.parse_args()

    if args.list_faults:
        print("\n=== 可用故障列表 ===")
        print("\n【稳定性故障】")
        for name, (layer, desc) in ALL_FAULTS.items():
            if layer == "stability":
                print(f"  {name:25s} - {desc}")
        print("\n【可信性故障】")
        for name, (layer, desc) in ALL_FAULTS.items():
            if layer == "credibility":
                print(f"  {name:25s} - {desc}")
        print()
        return

    # 加载配置
    if args.config:
        with open(args.config, "r", encoding="utf-8") as f:
            config = json.load(f)
    else:
        config = load_config()

    # 启用故障（支持逗号分隔的多故障叠加，如 --fault memory_bloat,wrong_diagnosis）
    if args.fault:
        fault_names = [f.strip() for f in args.fault.split(",") if f.strip()]
        for fn in fault_names:
            if fn in ALL_FAULTS:
                config = enable_fault(config, fn)
                print(f"[FAULT] 已激活: {fn} - {ALL_FAULTS[fn][1]}")
            else:
                print(f"[WARN] 未知故障，跳过: {fn}")
        print(f"[FAULT] 共激活 {len([f for f in fault_names if f in ALL_FAULTS])} 个故障（多故障叠加模式）")
    else:
        config["fault_injector"]["global_enabled"] = False
        print("\n[NORMAL] 正常模式，无故障注入")

    # 清空之前的 ground truth 日志
    gt_path = os.path.join("logs", "fault_injector_log.jsonl")
    if os.path.exists(gt_path):
        os.remove(gt_path)

    # 创建故障注入器和 Agent
    injector = FaultInjector(config)
    agent = TaskAgent(config, injector)

    # 运行任务
    print(f"\n[AGENT] 开始执行任务: {agent.task_id}")
    print("=" * 70)
    start = time.time()
    result = agent.run_task("fault_diagnosis_and_recovery")
    elapsed = time.time() - start

    # 输出结果摘要
    print("\n" + "=" * 70)
    print("[RESULT] 任务执行结果摘要:")
    print(f"  状态: {result.get('status')}")
    print(f"  结论: {result.get('conclusion', 'N/A')}")
    print(f"  置信度: {result.get('confidence', 'N/A')}")
    print(f"  耗时: {elapsed:.2f}s")
    if result.get("diagnosis"):
        for f in result["diagnosis"]:
            print(f"  故障: {f.get('fault_code')} (severity={f.get('severity')}, confidence={f.get('confidence')})")
    if result.get("error"):
        print(f"  错误: {result.get('error')}")

    # 输出 ground truth 记录
    gt_records = injector.gt_logger.get_records()
    if gt_records:
        print(f"\n[GROUND TRUTH] 共记录 {len(gt_records)} 次故障注入:")
        for r in gt_records:
            print(f"  - {r['fault_type']:25s} | step={r['trigger_step']} | {r['expected_behavior']}")
        print(f"  日志文件: logs/fault_injector_log.jsonl")

    print(f"\n[EVENT LOG] 共产生 {len(agent.get_event_log())} 个事件")
    print("[DONE]\n")


if __name__ == "__main__":
    main()
