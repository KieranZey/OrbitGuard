# -*- coding: utf-8 -*-
"""
故障覆盖率量化报告生成器（阶段一1.4）。

定义故障空间维度，量化当前9类故障的覆盖率，识别空白区域。

故障空间定义：
- 稳定性维度（Stability）：
  - 时间域：超时、死循环、无输出
  - 资源域：内存泄漏、CPU耗尽、文件句柄泄漏
  - 控制流域：进程崩溃、异常退出、重复调用、状态死锁

- 可信性维度（Credibility）：
  - 结论域：错误诊断、漏报、误报
  - 置信度域：虚高、过低、校准偏差
  - 证据域：证据缺失、证据矛盾、证据伪造
  - 数据域：传感器矛盾、数据丢包、数据篡改
"""
import json
import os
import sys
from typing import Dict, List, Any, Tuple
from fault_injector.injector import (
    EXECUTION_FAULTS, CREDIBILITY_FAULTS,
    TERMINAL_FAULTS, NON_TERMINAL_FAULTS,
)


# 故障空间维度定义
FAULT_SPACE = {
    "stability": {
        "time": {
            "name": "时间域",
            "subdimensions": {
                "timeout": "执行超时",
                "infinite_loop": "死循环/活锁",
                "no_output": "无输出/挂起",
            }
        },
        "resource": {
            "name": "资源域",
            "subdimensions": {
                "memory_leak": "内存泄漏",
                "cpu_exhaustion": "CPU耗尽",
                "fd_leak": "文件句柄泄漏",
            }
        },
        "control_flow": {
            "name": "控制流域",
            "subdimensions": {
                "process_crash": "进程崩溃",
                "abnormal_exit": "异常退出",
                "repetitive_calls": "重复调用循环",
                "state_deadlock": "状态死锁",
            }
        },
    },
    "credibility": {
        "conclusion": {
            "name": "结论域",
            "subdimensions": {
                "wrong_diagnosis": "错误诊断结论",
                "false_negative": "漏报（有故障报无故障）",
                "false_positive": "误报（无故障报有故障）",
            }
        },
        "confidence": {
            "name": "置信度域",
            "subdimensions": {
                "inflated_confidence": "置信度虚高",
                "deflated_confidence": "置信度过低",
                "miscalibration": "置信度校准偏差",
            }
        },
        "evidence": {
            "name": "证据域",
            "subdimensions": {
                "missing_evidence": "证据缺失",
                "contradictory_evidence": "证据矛盾",
                "forged_evidence": "证据伪造",
            }
        },
        "data": {
            "name": "数据域",
            "subdimensions": {
                "contradictory_sensors": "多源传感器矛盾",
                "packet_loss": "数据丢包",
                "data_tampering": "数据篡改",
            }
        },
    },
}

# 当前10类故障到故障空间的映射
# （P2修复：packet_loss 已由遥测噪声模型实现——帧级丢包+packet_loss事件，计入覆盖矩阵）
FAULT_COVERAGE_MAP = {
    # 稳定性故障
    "infinite_loop": {"layer": "stability", "dimension": "time", "subdimension": "infinite_loop"},
    "no_output_timeout": {"layer": "stability", "dimension": "time", "subdimension": "timeout"},
    "memory_bloat": {"layer": "stability", "dimension": "resource", "subdimension": "memory_leak"},
    "process_crash": {"layer": "stability", "dimension": "control_flow", "subdimension": "process_crash"},
    "repetitive_calls": {"layer": "stability", "dimension": "control_flow", "subdimension": "repetitive_calls"},
    # 可信性故障
    "wrong_diagnosis": {"layer": "credibility", "dimension": "conclusion", "subdimension": "wrong_diagnosis"},
    "inflated_confidence": {"layer": "credibility", "dimension": "confidence", "subdimension": "inflated_confidence"},
    "missing_evidence": {"layer": "credibility", "dimension": "evidence", "subdimension": "missing_evidence"},
    "contradictory_sensors": {"layer": "credibility", "dimension": "data", "subdimension": "contradictory_sensors"},
    # 遥测噪声层已实现的能力（帧级丢包，agent/noise_model.py）
    "packet_loss": {"layer": "credibility", "dimension": "data", "subdimension": "packet_loss"},
}


def calculate_coverage() -> Dict[str, Any]:
    """
    计算故障覆盖率。

    Returns:
        包含覆盖率统计、覆盖矩阵、空白区域的报告字典
    """
    total_subdims = 0
    covered_subdims = set()
    coverage_matrix = {}

    for layer_name, layer in FAULT_SPACE.items():
        coverage_matrix[layer_name] = {}
        for dim_name, dim in layer.items():
            coverage_matrix[layer_name][dim_name] = {
                "name": dim["name"],
                "subdimensions": {},
            }
            for subdim_key, subdim_name in dim["subdimensions"].items():
                total_subdims += 1
                is_covered = any(
                    mapping["layer"] == layer_name
                    and mapping["dimension"] == dim_name
                    and mapping["subdimension"] == subdim_key
                    for mapping in FAULT_COVERAGE_MAP.values()
                )
                if is_covered:
                    covered_subdims.add(f"{layer_name}.{dim_name}.{subdim_key}")
                coverage_matrix[layer_name][dim_name]["subdimensions"][subdim_key] = {
                    "name": subdim_name,
                    "covered": is_covered,
                    "fault": next(
                        (f for f, m in FAULT_COVERAGE_MAP.items()
                         if m["layer"] == layer_name and m["dimension"] == dim_name
                         and m["subdimension"] == subdim_key),
                        None,
                    ),
                }

    # 按层统计
    layer_stats = {}
    for layer_name in FAULT_SPACE:
        layer_total = sum(
            len(dim["subdimensions"]) for dim in FAULT_SPACE[layer_name].values()
        )
        layer_covered = sum(
            1 for sub in covered_subdims if sub.startswith(f"{layer_name}.")
        )
        layer_stats[layer_name] = {
            "total": layer_total,
            "covered": layer_covered,
            "coverage_rate": round(layer_covered / layer_total * 100, 1) if layer_total > 0 else 0,
        }

    # 空白区域（未覆盖的子维度）
    gaps = []
    for layer_name, layer in FAULT_SPACE.items():
        for dim_name, dim in layer.items():
            for subdim_key, subdim_name in dim["subdimensions"].items():
                key = f"{layer_name}.{dim_name}.{subdim_key}"
                if key not in covered_subdims:
                    gaps.append({
                        "layer": layer_name,
                        "dimension": dim_name,
                        "dimension_name": dim["name"],
                        "subdimension": subdim_key,
                        "subdimension_name": subdim_name,
                    })

    return {
        "summary": {
            "total_subdimensions": total_subdims,
            "covered_subdimensions": len(covered_subdims),
            "overall_coverage_rate": round(len(covered_subdims) / total_subdims * 100, 1),
            "total_faults_implemented": len(FAULT_COVERAGE_MAP),
        },
        "layer_stats": layer_stats,
        "coverage_matrix": coverage_matrix,
        "gaps": gaps,
        "fault_map": FAULT_COVERAGE_MAP,
    }


def generate_text_report(report: Dict[str, Any]) -> str:
    """生成文本格式的覆盖率报告。"""
    lines = []
    lines.append("=" * 70)
    lines.append("故障覆盖率量化报告")
    lines.append("=" * 70)
    lines.append("")

    # 总览
    s = report["summary"]
    lines.append("【总览】")
    lines.append(f"  故障空间子维度总数: {s['total_subdimensions']}")
    lines.append(f"  已覆盖子维度数:   {s['covered_subdimensions']}")
    lines.append(f"  整体覆盖率:       {s['overall_coverage_rate']}%")
    lines.append(f"  已实现故障类型数: {s['total_faults_implemented']}")
    lines.append("")

    # 分层统计
    lines.append("【分层覆盖率】")
    for layer_name, stats in report["layer_stats"].items():
        layer_cn = "稳定性" if layer_name == "stability" else "可信性"
        lines.append(f"  {layer_cn}({layer_name}): {stats['covered']}/{stats['total']} = {stats['coverage_rate']}%")
    lines.append("")

    # 覆盖矩阵
    lines.append("【覆盖矩阵】")
    for layer_name, layer in report["coverage_matrix"].items():
        layer_cn = "稳定性" if layer_name == "stability" else "可信性"
        lines.append(f"  [{layer_cn}]")
        for dim_name, dim in layer.items():
            lines.append(f"    {dim['name']}({dim_name}):")
            for sub_key, sub in dim["subdimensions"].items():
                mark = "✓" if sub["covered"] else "✗"
                fault_info = f" -> {sub['fault']}" if sub["fault"] else ""
                lines.append(f"      {mark} {sub['name']}({sub_key}){fault_info}")
    lines.append("")

    # 空白区域
    gaps = report["gaps"]
    lines.append(f"【空白区域】（共 {len(gaps)} 项未覆盖）")
    for gap in gaps:
        layer_cn = "稳定性" if gap["layer"] == "stability" else "可信性"
        lines.append(f"  ✗ [{layer_cn}] {gap['dimension_name']} > {gap['subdimension_name']}({gap['subdimension']})")
    lines.append("")

    # 建议
    lines.append("【扩展建议】")
    lines.append("  1. 稳定性-资源域-CPU耗尽：可注入CPU密集型死循环（当前infinite_loop已部分覆盖）")
    lines.append("  2. 稳定性-控制流域-状态死锁：可注入Agent状态机死锁（两个状态互相等待）")
    lines.append("  3. 可信性-结论域-漏报/误报：可注入有故障报无故障、无故障报有故障")
    lines.append("  4. 可信性-证据域-证据矛盾：可注入支持证据与结论互相矛盾")
    lines.append("  5. 可信性-数据域-数据篡改：可注入遥测数据被篡改（哈希校验失败）")
    lines.append("")
    lines.append("=" * 70)
    return "\n".join(lines)


def main():
    """生成覆盖率报告并输出到文件和控制台。"""
    # Windows 控制台默认 GBK，强制 UTF-8 输出避免 ✓ 等符号编码失败
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    report = calculate_coverage()

    # 输出文本报告
    text_report = generate_text_report(report)
    print(text_report)

    # 输出JSON报告
    output_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "docs")
    os.makedirs(output_dir, exist_ok=True)

    json_path = os.path.join(output_dir, "fault_coverage_report.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"\nJSON报告已保存: {json_path}")

    txt_path = os.path.join(output_dir, "fault_coverage_report.txt")
    with open(txt_path, "w", encoding="utf-8") as f:
        f.write(text_report)
    print(f"文本报告已保存: {txt_path}")


if __name__ == "__main__":
    main()
