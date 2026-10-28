"""Watchdog runner that keeps Talaba Bot running forever in the background."""

import ctypes
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
ES_DISPLAY_REQUIRED = 0x00000002
ES_AWAYMODE_REQUIRED = 0x00000040

PROJECT_DIR = Path(__file__).resolve().parents[1]
RUNTIME_DIR = PROJECT_DIR / "runtime"
RUNTIME_DIR.mkdir(exist_ok=True)

LOG_FILE = RUNTIME_DIR / "bot_service.log"
PID_FILE = RUNTIME_DIR / "bot_service.pid"
PYTHON_EXE = Path(r"C:\Users\Ma'murov Tohir\.virtualenvs\2-loyiha-LPnabA3I\Scripts\python.exe")
if not PYTHON_EXE.exists():
    PYTHON_EXE = Path(sys.executable)


def log(msg: str) -> None:
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(f"[{timestamp}] {msg}\n")


def prevent_windows_sleep() -> None:
    try:
        ctypes.windll.kernel32.SetThreadExecutionState(
            ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED | ES_AWAYMODE_REQUIRED
        )
    except Exception:
        pass


def main() -> None:
    prevent_windows_sleep()
    PID_FILE.write_text(str(os.getpid()), encoding="utf-8")
    log(f"Watchdog started (PID={os.getpid()}) using {PYTHON_EXE}")

    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)

    while True:
        try:
            with open(LOG_FILE, "a", encoding="utf-8") as out:
                log("Launching main.py...")
                proc = subprocess.Popen(
                    [str(PYTHON_EXE), str(PROJECT_DIR / "main.py")],
                    cwd=str(PROJECT_DIR),
                    stdout=out,
                    stderr=out,
                    creationflags=creationflags,
                )
                exit_code = proc.wait()
                log(f"main.py exited with code {exit_code}. Restarting in 5 seconds...")
        except Exception as exc:
            log(f"Watchdog error: {exc}. Retrying in 5 seconds...")
        time.sleep(5)


if __name__ == "__main__":
    main()
