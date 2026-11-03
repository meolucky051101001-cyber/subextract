"""Test bộ lọc log noise ConnectionResetError trong lifespan của app.

Kiểm tra trực tiếp trên event loop: đúng loại noise phải bị bỏ qua, còn lỗi khác
vẫn được ghi bình thường (để chắc rằng bộ lọc không che lỗi thật). Sau khi exit,
handler cũ phải được khôi phục.

Chạy: .venv/Scripts/python.exe data/test/log_noise_filter_test.py
"""
import asyncio
import logging
import sys
from pathlib import Path

for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import app.main as M  # noqa: E402

NOISE_MESSAGE = (
    "Exception in callback _ProactorBasePipeTransport._call_connection_lost(None)\n"
    "handle: <Handle _ProactorBasePipeTransport._call_connection_lost(None)>"
)


class Capture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.messages = []

    def emit(self, record):
        self.messages.append(record.getMessage())


async def exercise():
    capture = Capture()
    logger = logging.getLogger("asyncio")
    previous_handlers = logger.handlers
    previous_level = logger.level
    logger.handlers = [capture]
    logger.setLevel(logging.ERROR)
    try:
        async with M._lifespan(M.app):
            loop = asyncio.get_running_loop()
            handler = loop.get_exception_handler()
            if handler is not M._quiet_connection_noise:
                return False, "lifespan không cài bộ lọc", capture.messages
            loop.call_exception_handler({"message": NOISE_MESSAGE, "exception": ConnectionResetError(10054, "reset")})
            loop.call_exception_handler({"message": f"{NOISE_MESSAGE} [MARKER_RUNTIME]", "exception": RuntimeError("khác")})
            loop.call_exception_handler({"message": "Exception in callback something.else [MARKER_VALUE]", "exception": ValueError("loi that")})
        return True, "", capture.messages
    finally:
        logger.handlers = previous_handlers
        logger.setLevel(previous_level)


def main() -> int:
    installed, install_error, messages = asyncio.run(exercise())
    if not installed:
        print("FAIL:", install_error)
        return 1

    joined = "\n".join(messages)
    reset_logged = [m for m in messages if "ConnectionResetError" in m and "[MARKER" not in m]
    runtime_logged = "[MARKER_RUNTIME]" in joined
    value_logged = "[MARKER_VALUE]" in joined

    print(f"captured asyncio records: {len(messages)}")
    print(f"  ConnectionResetError (noise)  = {len(reset_logged)} (mong đợi 0)")
    print(f"  RuntimeError cùng callback    = {runtime_logged} (mong đợi True)")
    print(f"  ValueError ở callback khác    = {value_logged} (mong đợi True)")

    checks = {
        "noise_suppressed": not reset_logged,
        "other_exception_in_same_callback_still_logged": runtime_logged,
        "unrelated_error_still_logged": value_logged,
    }
    failed = [name for name, ok in checks.items() if not ok]
    print("RESULTS:", checks)
    if failed:
        print("FAILED:", ", ".join(failed))
        for message in messages:
            print("---\n", message[:200])
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
