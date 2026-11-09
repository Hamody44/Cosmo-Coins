"""Python entry point for hosts that only allow Python startup files.

This wrapper starts the existing Bash supervisor from the repository root so
both bots keep their separate virtual environments and shared runtime path.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parent
SCRIPT_PATH = ROOT_DIR / "run_bots.sh"


def main() -> int:
    if not SCRIPT_PATH.is_file():
        print(f"خطأ: لم يتم العثور على ملف التشغيل: {SCRIPT_PATH}", file=sys.stderr)
        return 1

    process = subprocess.Popen(
        ["bash", str(SCRIPT_PATH)],
        cwd=ROOT_DIR,
        start_new_session=True,
    )

    def forward_signal(signum: int, _frame: object) -> None:
        if process.poll() is None:
            try:
                os.killpg(process.pid, signum)
            except ProcessLookupError:
                pass

    signal.signal(signal.SIGINT, forward_signal)
    signal.signal(signal.SIGTERM, forward_signal)

    try:
        return process.wait()
    except KeyboardInterrupt:
        forward_signal(signal.SIGINT, None)
        return process.wait()


if __name__ == "__main__":
    raise SystemExit(main())
