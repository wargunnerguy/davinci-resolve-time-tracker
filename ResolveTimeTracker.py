"""
ResolveTimeTracker.py - LAUNCHER. Runs inside DaVinci Resolve via
Workspace -> Scripts. It has no window of its own: the whole app is
tracker_poller.py, a separate system-Python process with a single live
Tkinter window (live tracking, per-page bars, settings gear, due countdown,
All Projects, rate/earnings). This script just starts that process.

Why a launcher and not an in-Resolve window: a Resolve UIManager script window
cannot update on a timer, so it can't show live time. See CLAUDE.md.

Clicking the menu item again while the poller is already running is a no-op
(the poller's single-instance lock makes the second copy exit).

Install (end user): copy BOTH this file and tracker_poller.py into Resolve's
Scripts/Utility folder. The launcher finds tracker_poller.py next to itself and
locates a system Python automatically.

Developers using an exec() stub (where this file is NOT next to tracker_poller.py)
can point the launcher at their working copy by setting the environment variable
RESOLVE_TIMETRACKER_REPO to the repo path — keeping personal paths out of the repo.
"""

import os
import sys
import glob
import time
import shutil
import subprocess
import datetime
import traceback

# Optional dev fallback: repo path from an env var, used only when this file is
# run via an exec() stub (tracker_poller.py not sitting next to the launcher).
DEV_REPO_DIR = os.environ.get("RESOLVE_TIMETRACKER_REPO", "")


def _self_dir():
    """Directory this script lives in. May be undefined when run via exec()."""
    try:
        return os.path.dirname(os.path.abspath(__file__))
    except NameError:
        return None


def find_poller():
    """Locate tracker_poller.py: next to this file first, repo dir as fallback."""
    candidates = []
    sd = _self_dir()
    if sd:
        candidates.append(os.path.join(sd, "tracker_poller.py"))
    if DEV_REPO_DIR:
        candidates.append(os.path.join(DEV_REPO_DIR, "tracker_poller.py"))
    for c in candidates:
        if os.path.exists(c):
            return c
    return candidates[0] if candidates else "tracker_poller.py"


def find_pythonw():
    """Find a windowless system Python (Tkinter-capable). Best-effort."""
    cands = [r"C:\Python314\pythonw.exe"]
    cands += sorted(glob.glob(r"C:\Python3*\pythonw.exe"), reverse=True)
    cands += sorted(glob.glob(
        os.path.join(os.environ.get("LOCALAPPDATA", ""),
                     r"Programs\Python\Python3*\pythonw.exe"), reverse=True))
    for c in cands:
        if os.path.exists(c):
            return c
    return shutil.which("pythonw") or "pythonw"


POLLER_PATH = find_poller()
PYTHONW = find_pythonw()

DATA_DIR = os.path.join(os.environ.get("APPDATA", ""), "ResolveTimeTracker")
LOG_FILE = os.path.join(DATA_DIR, "tracker.log")
LOCK_FILE = os.path.join(DATA_DIR, "poller.lock")
os.makedirs(DATA_DIR, exist_ok=True)


def log(msg):
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write("[{}] {}\n".format(ts, msg))
    except Exception:
        pass


def _pid_alive(pid):
    """True if the given PID is a running process (Windows)."""
    try:
        import ctypes
        SYNCHRONIZE = 0x00100000
        h = ctypes.windll.kernel32.OpenProcess(SYNCHRONIZE, False, int(pid))
        if not h:
            return False
        try:
            return ctypes.windll.kernel32.WaitForSingleObject(h, 0) != 0
        finally:
            ctypes.windll.kernel32.CloseHandle(h)
    except Exception:
        return True


def poller_running():
    try:
        if not os.path.exists(LOCK_FILE):
            return False
        if (time.time() - os.path.getmtime(LOCK_FILE)) >= 5.0:
            return False  # stale -> previous instance is gone
        # Fresh timestamp, but a crashed instance can leave a fresh-looking lock.
        # Trust it only if the recorded PID is actually alive.
        try:
            with open(LOCK_FILE, "r", encoding="utf-8") as f:
                pid = int((f.read() or "0").strip())
        except Exception:
            pid = 0
        if pid and not _pid_alive(pid):
            return False
        return True
    except Exception:
        return False


def launch():
    log("=== Launcher invoked ===")
    if poller_running():
        log("Poller already running; nothing to do.")
        return
    if not os.path.exists(POLLER_PATH):
        log("Poller not found at {}".format(POLLER_PATH))
        return
    exe = PYTHONW
    try:
        flags = 0
        if hasattr(subprocess, "CREATE_NEW_PROCESS_GROUP"):
            # DETACHED_PROCESS (0x8) so it lives independently of Resolve.
            flags = subprocess.CREATE_NEW_PROCESS_GROUP | 0x00000008
        subprocess.Popen([exe, POLLER_PATH], cwd=os.path.dirname(POLLER_PATH),
                         close_fds=True, creationflags=flags)
        log("Launched poller: {} {}".format(exe, POLLER_PATH))
    except Exception:
        log("Launch failed:\n{}".format(traceback.format_exc()))


launch()
