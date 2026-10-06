# -*- coding: utf-8 -*-
"""
控制台输出编码兼容层。

背景（CI 真实故障）：
    GitHub Actions 的 windows-latest runner 使用**非 UTF-8 代码页**（cp1252），
    而本项目大量输出中文（测试报告、故障描述、事件流）。Python 在向非 UTF-8
    标准输出打印中文时会抛 UnicodeEncodeError，导致测试进程以 exit code 1 退出——
    同一份代码在 ubuntu（UTF-8 locale）全绿、在 windows 两个矩阵全红，就是这个原因。

处理策略：
    - 输出被重定向（CI 日志、管道、文件，isatty() 为 False）→ 强制 UTF-8 编码，
      保证 CI 日志里中文可读；errors="replace" 兜底，任何字符都不会再抛异常。
    - 输出是本地终端（isatty() 为 True）→ 保留控制台原有编码（中文 Windows 为 GBK，
      中文可正常显示，不会因强制 UTF-8 变成乱码），仅把无法编码的字符替换掉。

幂等：可重复调用，任何异常都静默忽略（不能因为改控制台而影响主流程）。
"""
import sys


def enable_utf8_stdout() -> None:
    """让标准输出/标准错误在任意代码页下都不会因中文与符号抛 UnicodeEncodeError。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            if not hasattr(stream, "reconfigure"):
                continue  # 被测试替身（StringIO 等）替换过，跳过
            if stream.isatty():
                # 本地终端：保留原编码，仅降级不可编码字符，避免乱码
                stream.reconfigure(errors="replace")
            else:
                # 重定向/CI：日志按 UTF-8 落盘，中文可读
                stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
