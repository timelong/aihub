"""AI Hub。

最低 Python 版本 3.9（macOS 系统自带的就是 3.9.6，可直接跑）。
低于 3.9 会缺 zoneinfo / asyncio.to_thread / list[str] 这些用到的东西，
所以这里直接给一句人话提示，而不是让它在某个 import 里报奇怪的错。
"""
import sys

MIN_PYTHON = (3, 9)

if sys.version_info < MIN_PYTHON:
    raise RuntimeError(
        f"AI Hub 需要 Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]} 或更高版本，"
        f"当前是 {sys.version.split()[0]}（{sys.executable}）。\n"
        "macOS 可以用系统自带的 python3，或 brew install python@3.12 后重建虚拟环境：\n"
        "  rm -rf .venv .venv/.deps_ok && ./run.sh"
    )
