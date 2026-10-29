import base64
import hashlib
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from cryptography.fernet import Fernet

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "talaba_bot.db"
CONFIRMED_PATH = ROOT / "runtime" / "confirmed_channel_users.json"
MAX_RUNTIME_SECONDS = 5 * 3600 + 25 * 60  # 5h 25m


def get_fernet() -> Fernet:
    key = os.getenv("HEMIS_SESSION_KEY", "").strip()
    if not key:
        raise RuntimeError("HEMIS_SESSION_KEY is required for cloud state encryption")
    return Fernet(key.encode("utf-8"))


def restore_state() -> None:
    fernet = get_fernet()
    enc_data = None

    # 1. Try fetching latest bot_state.enc from origin/bot-state branch
    try:
        res = subprocess.run(
            ["git", "fetch", "origin", "bot-state"],
            cwd=str(ROOT),
            capture_output=True,
            timeout=20,
        )
        if res.returncode == 0:
            show = subprocess.run(
                ["git", "show", "origin/bot-state:bot_state.enc"],
                cwd=str(ROOT),
                capture_output=True,
                timeout=15,
            )
            if show.returncode == 0 and show.stdout.strip():
                enc_data = show.stdout.strip()
                print("[cloud_runner] Loaded state from origin/bot-state branch.", flush=True)
    except Exception as exc:
        print(f"[cloud_runner] Branch state fetch skipped: {exc}", flush=True)

    # 2. Fallback to INITIAL_BOT_STATE_ENC secret
    if not enc_data:
        init_secret = os.getenv("INITIAL_BOT_STATE_ENC", "").strip()
        if init_secret:
            enc_data = init_secret.encode("utf-8")
            print("[cloud_runner] Loaded state from INITIAL_BOT_STATE_ENC secret.", flush=True)

    if not enc_data:
        print("[cloud_runner] No prior state found; starting with fresh database.", flush=True)
        return

    try:
        raw_json = fernet.decrypt(enc_data).decode("utf-8")
        payload = json.loads(raw_json)
        if payload.get("bot_db_b64"):
            DB_PATH.write_bytes(base64.b64decode(payload["bot_db_b64"]))
            print(f"[cloud_runner] Restored bot.db ({DB_PATH.stat().st_size} bytes).", flush=True)
        if payload.get("confirmed_users"):
            CONFIRMED_PATH.parent.mkdir(parents=True, exist_ok=True)
            CONFIRMED_PATH.write_text(payload["confirmed_users"], encoding="utf-8")
            print("[cloud_runner] Restored confirmed_channel_users.json.", flush=True)
    except Exception as exc:
        print(f"[cloud_runner] Failed to decrypt/restore state: {exc}", flush=True)


def compute_state_digest() -> str:
    h = hashlib.sha256()
    if DB_PATH.exists():
        h.update(DB_PATH.read_bytes())
    if CONFIRMED_PATH.exists():
        h.update(CONFIRMED_PATH.read_bytes())
    return h.hexdigest()


def save_state() -> None:
    if not DB_PATH.exists():
        return
    try:
        fernet = get_fernet()
        payload = {
            "bot_db_b64": base64.b64encode(DB_PATH.read_bytes()).decode("ascii"),
            "confirmed_users": CONFIRMED_PATH.read_text(encoding="utf-8") if CONFIRMED_PATH.exists() else "[]",
            "updated_at": int(time.time()),
        }
        enc_bytes = fernet.encrypt(json.dumps(payload).encode("utf-8"))

        with tempfile.TemporaryDirectory() as tmpdir:
            subprocess.run(["git", "init", "-q"], cwd=tmpdir, check=True)
            subprocess.run(["git", "config", "user.name", "talaba-bot-cloud"], cwd=tmpdir, check=True)
            subprocess.run(["git", "config", "user.email", "bot@users.noreply.github.com"], cwd=tmpdir, check=True)
            (Path(tmpdir) / "bot_state.enc").write_bytes(enc_bytes)
            subprocess.run(["git", "add", "bot_state.enc"], cwd=tmpdir, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "Sync encrypted bot state"], cwd=tmpdir, check=True)

            remote_res = subprocess.run(
                ["git", "remote", "get-url", "origin"],
                cwd=str(ROOT),
                capture_output=True,
                text=True,
                check=True,
            )
            remote_url = remote_res.stdout.strip()
            subprocess.run(
                ["git", "push", "-q", "--force", remote_url, "HEAD:refs/heads/bot-state"],
                cwd=tmpdir,
                timeout=30,
                check=True,
            )
            print("[cloud_runner] Synced encrypted state to bot-state branch.", flush=True)
    except Exception as exc:
        print(f"[cloud_runner] State sync warning: {exc}", flush=True)


def trigger_next_workflow() -> None:
    token = os.getenv("GH_WORKFLOW_TOKEN") or os.getenv("GITHUB_TOKEN")
    repo = os.getenv("GITHUB_REPOSITORY", "Xusanovich-MTX/talaba_yordamchisi")
    if not token:
        return
    url = f"https://api.github.com/repos/{repo}/actions/workflows/bot_24_7.yml/dispatches"
    data = json.dumps({"ref": "main"}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
            "User-Agent": "talaba-bot-cloud-runner",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            print(f"[cloud_runner] Triggered next workflow run (HTTP {resp.status}).", flush=True)
    except Exception as exc:
        print(f"[cloud_runner] Next workflow dispatch warning: {exc}", flush=True)


def main() -> int:
    restore_state()
    last_digest = compute_state_digest()
    start_ts = time.time()

    proc = subprocess.Popen([sys.executable, str(ROOT / "main.py")], cwd=str(ROOT))
    print(f"[cloud_runner] Started main.py (PID {proc.pid}).", flush=True)

    stop_requested = False

    def handle_sig(signum, frame):
        nonlocal stop_requested
        stop_requested = True

    signal.signal(signal.SIGTERM, handle_sig)
    signal.signal(signal.SIGINT, handle_sig)

    last_sync = time.time()
    try:
        while not stop_requested:
            ret = proc.poll()
            if ret is not None:
                print(f"[cloud_runner] main.py exited with code {ret}; restarting in 3s...", flush=True)
                time.sleep(3)
                proc = subprocess.Popen([sys.executable, str(ROOT / "main.py")], cwd=str(ROOT))

            now = time.time()
            if now - last_sync >= 45:
                current_digest = compute_state_digest()
                if current_digest != last_digest:
                    save_state()
                    last_digest = current_digest
                last_sync = now

            if now - start_ts >= MAX_RUNTIME_SECONDS:
                print("[cloud_runner] Reached planned session rotation window; rotating cleanly...", flush=True)
                break

            time.sleep(2)
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        save_state()
        trigger_next_workflow()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
