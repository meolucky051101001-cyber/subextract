import ctypes
import json
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
HOST = "127.0.0.1"
DEFAULT_PORT = 8787


def healthy(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://{HOST}:{port}/api/health", timeout=1.5) as resp:
            return json.loads(resp.read().decode("utf-8")).get("ok") is True
    except Exception:
        return False


def show_error(message: str) -> None:
    try:
        ctypes.windll.user32.MessageBoxW(0, message, "SubExtract", 0x10)
    except Exception:
        print(message, file=sys.stderr)


def main() -> int:
    args = sys.argv[1:]
    no_browser = "--no-browser" in args
    keep_alive = "--keep-alive" in args
    port = DEFAULT_PORT
    if "--port" in args:
        try:
            port = int(args[args.index("--port") + 1])
        except (IndexError, ValueError):
            show_error("Tham số --port không hợp lệ.")
            return 2

    url = f"http://{HOST}:{port}/"
    if healthy(port):
        if keep_alive:
            while healthy(port):
                time.sleep(5)
        if not no_browser:
            webbrowser.open(url)
        return 0

    log_dir = ROOT / "data"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "server.log"
    # Xoay vòng log: quá 5 MB thì dồn sang server.log.1 (giữ 1 thế hệ).
    if log_path.exists() and log_path.stat().st_size > 5 * 1024 * 1024:
        rotated = log_dir / "server.log.1"
        if rotated.exists():
            rotated.unlink()
        log_path.replace(rotated)
    venv_python = ROOT / ".venv" / "Scripts" / "python.exe"
    python_exe = str(venv_python) if venv_python.exists() else sys.executable
    with open(log_path, "ab") as log:
        flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        subprocess.Popen(
            [python_exe, "-m", "uvicorn", "app.main:app", "--host", HOST, "--port", str(port)],
            cwd=str(ROOT),
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            creationflags=flags,
            close_fds=True,
        )

    deadline = time.time() + 60
    while time.time() < deadline:
        if healthy(port):
            if not no_browser:
                webbrowser.open(url)
            if keep_alive:
                while healthy(port):
                    time.sleep(5)
            return 0
        time.sleep(0.5)

    show_error(
        f"Không khởi động được SubExtract trên cổng {port} sau 60 giây.\n"
        f"Xem log tại: {log_path}"
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
