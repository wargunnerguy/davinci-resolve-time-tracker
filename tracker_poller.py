#!/usr/bin/env python3
"""
tracker_poller.py - the LIVE tracker. Runs in SYSTEM Python as a separate
process (NOT inside Resolve's interpreter).

Why a separate process: Resolve's in-app UIManager window cannot update itself
on a timer (its event loop only runs while you interact with it, no ui.Timer,
no background thread can wake it). So accurate per-second / per-page tracking is
impossible from inside Resolve. This process connects to Resolve via the
external scripting API, polls the current page once a second, and shows a live
always-on-top Tkinter window (Tkinter has a real working timer via .after()).

It is the SOLE writer of sessions.json. The in-Resolve script reads that file
for its reports view. Per-project hourly rates live in config.json.

Launched by the in-Resolve script (Start Live Tracker), or directly:
    C:\\Python314\\python.exe tracker_poller.py
"""

import os
import sys
import csv
import json
import time
import datetime
import subprocess
import traceback

try:
    import ctypes
except Exception:
    ctypes = None
try:
    import winreg
except Exception:
    winreg = None

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

DATA_DIR = os.path.join(os.environ.get("APPDATA", ""), "ResolveTimeTracker")
SESSIONS_FILE = os.path.join(DATA_DIR, "sessions.json")
CONFIG_FILE = os.path.join(DATA_DIR, "config.json")
STATE_FILE = os.path.join(DATA_DIR, "state.json")  # reader -> UI: current project/page
RAISE_FILE = os.path.join(DATA_DIR, "raise.request")  # launcher -> UI: bring to front
LOCK_FILE = os.path.join(DATA_DIR, "poller.lock")
LOG_FILE = os.path.join(DATA_DIR, "poller.log")

os.makedirs(DATA_DIR, exist_ok=True)

# Defaults for the Resolve external scripting API (found on this machine).
DEFAULT_API = r"C:\ProgramData\Blackmagic Design\DaVinci Resolve\Support\Developer\Scripting"
DEFAULT_LIB = r"C:\Program Files\Blackmagic Design\DaVinci Resolve\fusionscript.dll"

PAGES = ["media", "cut", "edit", "fusion", "color", "fairlight", "deliver", "other"]
PAGE_LABELS = {
    "media": "Media", "cut": "Cut", "edit": "Edit", "fusion": "Fusion",
    "color": "Color", "fairlight": "Fairlight", "deliver": "Deliver", "other": "Other",
}

VERSION = "1.1.0"

TICK_INTERVAL_MS = 1000
POLL_INTERVAL_S = 1.0  # reader-subprocess Resolve poll cadence
BUSY_THRESHOLD = 1.5   # a reader poll blocking this long means Resolve is busy
SAVE_INTERVAL = 15.0
MAX_TICK_DELTA = 3600.0
LOCK_STALE = 5.0  # a lock newer than this means another poller is alive

NO_PROJECT = ("(no project)", "(unknown)", "", None)

# ---------------------------------------------------------------------------
# i18n  (page names stay English; everything else is translated)
# ---------------------------------------------------------------------------

LANGS = [("en", "English"), ("et", "Eesti")]

STRINGS = {
    "en": {
        "tracking": "Tracking", "paused": "Paused", "paused_idle": "Paused (idle)",
        "idle_label": "Idle auto-pause (min):", "idle_hint": "0 = off",
        "resume_label": "Auto-resume on:",
        "resume_any": "Any activity", "resume_resolve": "Only in Resolve",
        "connecting": "Connecting…",
        "connecting_long": "Connecting to Resolve… (is Resolve open?)",
        "total": "Total", "page": "Page", "today": "Today",
        "rate": "Rate ({} /hr):", "pause": "Pause", "resume": "Resume",
        "all_projects": "All Projects", "settings": "Settings",
        "currency": "Currency:", "rate_type": "Rate type:",
        "due_countdown": "Due countdown:", "show": "show",
        "due_project": "Due (this project):",
        "due_format": "format: YYYY-MM-DD HH:MM   (blank = none)",
        "language": "Language:", "theme": "Theme:",
        "theme_dark": "Dark", "theme_light": "Light", "theme_system": "System",
        "save": "Save", "cancel": "Cancel", "close": "Close",
        "no_projects": "No tracked projects yet.", "total_caps": "TOTAL",
        "due_in": "Due in", "overdue_by": "Overdue by",
        "u_d": "d", "u_h": "h", "u_m": "m",
        "rt_brutto": "Gross", "rt_netto": "Net", "rt_ettevotja_kulu": "Employer cost",
        "stats": "Stats / Export", "export_csv": "Export CSV",
        "project_col": "Project", "hours_col": "Time", "rate_col": "Rate",
        "amount_col": "Amount", "exported": "Exported to:", "export_failed": "Export failed",
    },
    "et": {
        "tracking": "Jälgin", "paused": "Peatatud", "paused_idle": "Peatatud (jõude)",
        "idle_label": "Jõudeoleku paus (min):", "idle_hint": "0 = väljas",
        "resume_label": "Automaatne jätk:",
        "resume_any": "Suvaline tegevus", "resume_resolve": "Ainult Resolve'is",
        "connecting": "Ühendan…",
        "connecting_long": "Ühendan Resolve'iga… (kas Resolve on avatud?)",
        "total": "Kokku", "page": "Leht", "today": "Täna",
        "rate": "Tariif ({} /h):", "pause": "Peata", "resume": "Jätka",
        "all_projects": "Kõik projektid", "settings": "Seaded",
        "currency": "Valuuta:", "rate_type": "Tariifi tüüp:",
        "due_countdown": "Tähtaja loendur:", "show": "näita",
        "due_project": "Tähtaeg (see projekt):",
        "due_format": "vorming: AAAA-KK-PP TT:MM   (tühi = puudub)",
        "language": "Keel:", "theme": "Teema:",
        "theme_dark": "Tume", "theme_light": "Hele", "theme_system": "Süsteem",
        "save": "Salvesta", "cancel": "Tühista", "close": "Sulge",
        "no_projects": "Projekte pole veel jälgitud.", "total_caps": "KOKKU",
        "due_in": "Tähtajani", "overdue_by": "Üle tähtaja",
        "u_d": "p", "u_h": "t", "u_m": "min",
        "rt_brutto": "Bruto", "rt_netto": "Neto", "rt_ettevotja_kulu": "Ettevõtja kulu",
        "stats": "Statistika / Eksport", "export_csv": "Ekspordi CSV",
        "project_col": "Projekt", "hours_col": "Aeg", "rate_col": "Tariif",
        "amount_col": "Summa", "exported": "Eksporditud:", "export_failed": "Eksport ebaõnnestus",
    },
}


def tr(lang, key):
    return STRINGS.get(lang, STRINGS["en"]).get(key) or STRINGS["en"].get(key, key)


# Rate-type keys are stable in config; display labels come from STRINGS.
RATE_TYPE_KEYS = ["brutto", "netto", "ettevotja_kulu"]

# ---------------------------------------------------------------------------
# Themes
# ---------------------------------------------------------------------------

THEMES = {
    "dark": {
        "BG": "#1b1b1d", "FG": "#ececec", "MUTED": "#9aa0a6",
        "BAR_BG": "#34343a", "BAR_FILL": "#1faa59", "ROW_HL": "#2a2a31",
        "ENTRY_BG": "#2c2c30", "BTN_BG": "#2c2c30", "BTN_ACTIVE": "#3a3a40",
        "OVERDUE": "#ff5252", "DARK_TITLEBAR": True,
    },
    "light": {
        "BG": "#f3f3f3", "FG": "#1b1b1d", "MUTED": "#5f6368",
        "BAR_BG": "#d8d8dc", "BAR_FILL": "#1faa59", "ROW_HL": "#e2e2ea",
        "ENTRY_BG": "#ffffff", "BTN_BG": "#e3e3e6", "BTN_ACTIVE": "#d2d2d6",
        "OVERDUE": "#c62828", "DARK_TITLEBAR": False,
    },
}


def system_is_dark():
    if winreg is None:
        return True
    try:
        k = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize")
        val, _ = winreg.QueryValueEx(k, "AppsUseLightTheme")
        return val == 0
    except Exception:
        return True


def resolve_theme(setting):
    if setting == "light":
        return THEMES["light"]
    if setting == "system":
        return THEMES["dark"] if system_is_dark() else THEMES["light"]
    return THEMES["dark"]


def system_idle_seconds():
    """Seconds since the last system-wide keyboard/mouse input (Windows)."""
    if ctypes is None or sys.platform != "win32":
        return 0.0
    try:
        class LASTINPUTINFO(ctypes.Structure):
            _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]
        lii = LASTINPUTINFO()
        lii.cbSize = ctypes.sizeof(LASTINPUTINFO)
        if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(lii)):
            return 0.0
        tick = ctypes.windll.kernel32.GetTickCount()
        idle_ms = tick - lii.dwTime
        if idle_ms < 0:
            idle_ms += 2 ** 32  # GetTickCount wraps ~49.7 days
        return idle_ms / 1000.0
    except Exception:
        return 0.0


def resolve_is_foreground():
    """True if DaVinci Resolve is the active (foreground) window (Windows)."""
    if ctypes is None or sys.platform != "win32":
        return False
    try:
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return False
        pid = ctypes.c_ulong(0)
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if not pid.value:
            return False
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
        if not h:
            return False
        try:
            buf = ctypes.create_unicode_buffer(4096)
            size = ctypes.c_ulong(4096)
            if not kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                return False
            return os.path.basename(buf.value).lower() == "resolve.exe"
        finally:
            kernel32.CloseHandle(h)
    except Exception:
        return False


def set_titlebar_dark(root, dark):
    """Dark (or light) Windows title bar via the DWM immersive-dark-mode attr."""
    if ctypes is None or sys.platform != "win32":
        return
    try:
        root.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(root.winfo_id())
        val = ctypes.c_int(1 if dark else 0)
        for attr in (20, 19):  # 20 = Win10 20H1+/Win11, 19 = older builds
            try:
                ctypes.windll.dwmapi.DwmSetWindowAttribute(
                    hwnd, attr, ctypes.byref(val), ctypes.sizeof(val))
            except Exception:
                pass
        # Force the frame to repaint so the change shows immediately.
        root.withdraw()
        root.deiconify()
    except Exception:
        pass


def log(msg):
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write("[{}] {}\n".format(ts, msg))
    except Exception:
        pass


def log_exception(label="Unhandled exception"):
    log("{}:\n{}".format(label, traceback.format_exc()))


# ---------------------------------------------------------------------------
# Single-instance lock (freshness based; refreshed every tick)
# ---------------------------------------------------------------------------

def lock_is_held_by_other():
    try:
        if not os.path.exists(LOCK_FILE):
            return False
        age = time.time() - os.path.getmtime(LOCK_FILE)
        if age >= LOCK_STALE:
            return False  # stale -> previous instance is gone
        # Fresh timestamp, but a crashed instance can leave a fresh-looking lock
        # that would wrongly block relaunch. Trust it only if the PID is alive.
        try:
            with open(LOCK_FILE, "r", encoding="utf-8") as f:
                pid = int((f.read() or "0").strip())
        except Exception:
            pid = 0
        if pid and pid != os.getpid() and not pid_alive(pid):
            return False
        return True
    except Exception:
        return False


def touch_lock():
    try:
        with open(LOCK_FILE, "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
    except Exception:
        pass


def release_lock():
    try:
        if os.path.exists(LOCK_FILE):
            os.remove(LOCK_FILE)
    except Exception:
        pass


def pid_alive(pid):
    """True if the given PID is a running process (Windows)."""
    if ctypes is None or sys.platform != "win32":
        return True
    try:
        SYNCHRONIZE = 0x00100000
        h = ctypes.windll.kernel32.OpenProcess(SYNCHRONIZE, False, int(pid))
        if not h:
            return False
        try:
            # WAIT_TIMEOUT (0x102) => still running; WAIT_OBJECT_0 (0) => exited
            return ctypes.windll.kernel32.WaitForSingleObject(h, 0) != 0
        finally:
            ctypes.windll.kernel32.CloseHandle(h)
    except Exception:
        return True


# ---------------------------------------------------------------------------
# Data (sessions.json: poller is the sole writer; config.json: rates/currency)
# ---------------------------------------------------------------------------

def new_data():
    return {"version": 3, "projects": {}}


def _ensure_project(rec):
    rec.setdefault("pages", {})
    rec.setdefault("daily", {})
    # rate_seconds: seconds tracked at each rate (key = rate as a string), so a
    # rate change only affects time tracked AFTER it. Earnings sum over these.
    rec.setdefault("rate_seconds", {})
    return rec


def load_data():
    if not os.path.exists(SESSIONS_FILE):
        return new_data(), {}
    try:
        with open(SESSIONS_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except Exception:
        log_exception("load_data")
        return new_data(), {}

    migrated_rates = {}
    if isinstance(raw, list):
        return new_data(), {}
    if not isinstance(raw, dict):
        return new_data(), {}
    raw.setdefault("version", 3)
    raw.setdefault("projects", {})
    for name, rec in raw["projects"].items():
        _ensure_project(rec)
        # v2 stored rate inside the project; lift it into config.
        if "rate" in rec:
            try:
                migrated_rates[name] = float(rec.pop("rate") or 0)
            except Exception:
                pass
    return raw, migrated_rates


def save_data(data):
    tmp = SESSIONS_FILE + "." + str(os.getpid()) + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        for _ in range(3):
            try:
                os.replace(tmp, SESSIONS_FILE)
                return
            except PermissionError:
                time.sleep(0.1)
    except Exception:
        log_exception("save_data")
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def default_config():
    return {"currency": "€", "rate_type": "brutto", "show_due": True,
            "lang": "en", "theme": "dark", "idle_minutes": 5,
            "resume_scope": "any", "rates": {}, "due": {}}


def load_config():
    cfg = default_config()
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                raw = json.load(f)
            if isinstance(raw, dict):
                cfg["currency"] = raw.get("currency", cfg["currency"])
                cfg["rate_type"] = raw.get("rate_type", cfg["rate_type"])
                cfg["show_due"] = bool(raw.get("show_due", cfg["show_due"]))
                cfg["lang"] = raw.get("lang", cfg["lang"])
                cfg["theme"] = raw.get("theme", cfg["theme"])
                cfg["idle_minutes"] = raw.get("idle_minutes", cfg["idle_minutes"])
                cfg["resume_scope"] = raw.get("resume_scope", cfg["resume_scope"])
                cfg["rates"] = raw.get("rates", {}) or {}
                cfg["due"] = raw.get("due", {}) or {}
        except Exception:
            log_exception("load_config")
    return cfg


def save_config(cfg):
    tmp = CONFIG_FILE + "." + str(os.getpid()) + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
        for _ in range(3):
            try:
                os.replace(tmp, CONFIG_FILE)
                return
            except PermissionError:
                time.sleep(0.1)
    except Exception:
        log_exception("save_config")
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

def fmt_hms(secs):
    s = int(round(secs))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return "{:02d}:{:02d}:{:02d}".format(h, m, sec)


# ---------------------------------------------------------------------------
# Resolve connection
# ---------------------------------------------------------------------------

def connect_resolve():
    api = os.environ.get("RESOLVE_SCRIPT_API") or DEFAULT_API
    lib = os.environ.get("RESOLVE_SCRIPT_LIB") or DEFAULT_LIB
    os.environ["RESOLVE_SCRIPT_API"] = api
    os.environ["RESOLVE_SCRIPT_LIB"] = lib
    modules = os.path.join(api, "Modules")
    if modules not in sys.path:
        sys.path.append(modules)
    try:
        import DaVinciResolveScript as dvr
        return dvr.scriptapp("Resolve")
    except Exception:
        log_exception("connect_resolve")
        return None


# ---------------------------------------------------------------------------
# Reader subprocess  (the ONLY thing that talks to Resolve)
# ---------------------------------------------------------------------------

def write_state(d):
    tmp = STATE_FILE + "." + str(os.getpid()) + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f)
        for _ in range(3):
            try:
                os.replace(tmp, STATE_FILE)
                return
            except PermissionError:
                time.sleep(0.05)
    except Exception:
        log_exception("write_state")
    finally:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass


def read_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def run_reader(parent_pid=None):
    """Separate process: connect to Resolve and report the current project,
    page and timeline timecode into state.json once a second. Runs alone so a
    blocking Resolve call (which holds the GIL) can't freeze the UI process.
    Exits when its parent (the UI process) dies, or the lock goes stale."""
    log("=== reader starting (pid {}, parent {}) ===".format(os.getpid(), parent_pid))
    resolve = None
    while True:
        # Self-terminate if the main poller is gone.
        if parent_pid is not None and not pid_alive(parent_pid):
            log("reader: parent {} gone; exiting".format(parent_pid))
            return
        try:
            if (not os.path.exists(LOCK_FILE) or
                    (time.time() - os.path.getmtime(LOCK_FILE)) > LOCK_STALE * 3):
                log("reader: main poller lock stale; exiting")
                return
        except Exception:
            pass

        t0 = time.time()
        name = page = tc = None
        try:
            if resolve is None:
                resolve = connect_resolve()
            if resolve is not None:
                pm = resolve.GetProjectManager()
                proj = pm.GetCurrentProject() if pm else None
                name = proj.GetName() if proj else None
                page = resolve.GetCurrentPage()
                page = page if page in PAGES else ("other" if page else None)
                try:
                    tl = proj.GetCurrentTimeline() if proj else None
                    tc = tl.GetCurrentTimecode() if tl else None
                except Exception:
                    tc = None
        except Exception:
            log_exception("reader poll")
            resolve = None  # force reconnect
        t1 = time.time()

        write_state({"name": name, "page": page, "tc": tc,
                     "blocked": t1 - t0, "ts": t1})
        time.sleep(POLL_INTERVAL_S)


# ---------------------------------------------------------------------------
# Tracker
# ---------------------------------------------------------------------------

class Poller:
    def __init__(self):
        self.data, migrated = load_data()
        self.config = load_config()
        if migrated:
            for name, rate in migrated.items():
                self.config["rates"].setdefault(name, rate)
            save_config(self.config)
            save_data(self.data)
        self.resolve = None
        self.connected = False
        self.running = True
        self.idle_paused = False  # paused automatically by the idle timer
        self.current_project = None
        self.current_page = None
        self._last_tick = time.time()
        self._last_save = time.time()
        self._session_start = time.time()
        self._session_frozen = 0.0
        # Resolve is polled on a BACKGROUND thread: its scripting calls block
        # while Resolve is busy (e.g. playing a clip), and we must not let that
        # freeze the Tkinter UI. The UI thread reads these last-known values.
        # Resolve is read by a SEPARATE PROCESS (see start_reader). This process
        # only reads state.json; it never calls Resolve, so a blocking Resolve
        # call (playback/render) can't freeze the UI.
        self._reader_proc = None
        # Activity from Resolve (playhead movement / busy) — watching footage
        # produces no keyboard/mouse input, so we count it as activity too,
        # otherwise reviewing a clip would trip the idle pause.
        self._last_tc = None
        self._last_resolve_activity = time.time()

    # -- record access ------------------------------------------------------

    def rec(self, name):
        if name in NO_PROJECT:
            return None
        r = self.data["projects"].get(name)
        if r is None:
            r = {"pages": {}, "daily": {}, "rate_seconds": {}}
            self.data["projects"][name] = r
        return _ensure_project(r)

    def rate(self, name):
        try:
            return float(self.config["rates"].get(name, 0) or 0)
        except Exception:
            return 0.0

    def currency(self):
        return self.config.get("currency", "€")

    def rate_type(self):
        return self.config.get("rate_type", "brutto")

    def lang(self):
        return self.config.get("lang", "en")

    def theme(self):
        return self.config.get("theme", "dark")

    def show_due(self):
        return bool(self.config.get("show_due", True))

    def idle_limit_seconds(self):
        try:
            return max(0.0, float(self.config.get("idle_minutes", 0) or 0)) * 60.0
        except Exception:
            return 0.0

    def due_for(self, name):
        return (self.config.get("due", {}) or {}).get(name)

    def due_countdown(self, name):
        """Return (days, hrs, mins, overdue_bool) for the due date, or None.
        Formatting/translation is done in the UI."""
        iso = self.due_for(name)
        if not iso:
            return None
        due = None
        for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M"):
            try:
                due = datetime.datetime.strptime(iso, fmt)
                break
            except Exception:
                pass
        if due is None:
            try:
                due = datetime.datetime.fromisoformat(iso)
            except Exception:
                return None
        secs = (due - datetime.datetime.now()).total_seconds()
        overdue = secs < 0
        secs = abs(secs)
        return (int(secs // 86400), int((secs % 86400) // 3600),
                int((secs % 3600) // 60), overdue)

    def update_config(self, **kw):
        self.config.update(kw)
        save_config(self.config)

    def set_due(self, name, iso_or_none):
        due = self.config.setdefault("due", {})
        if iso_or_none:
            due[name] = iso_or_none
        else:
            due.pop(name, None)
        save_config(self.config)

    # -- aggregates ---------------------------------------------------------

    def total_seconds(self, name):
        r = self.rec(name)
        return sum(r["pages"].values()) if r else 0

    def today_seconds(self, name):
        r = self.rec(name)
        if not r:
            return 0
        today = datetime.date.today().isoformat()
        return sum(r["daily"].get(today, {}).values())

    def rate_breakdown(self, name):
        """List of (rate_float, seconds) sorted by rate, from the rate buckets.
        Falls back to (current_rate, total) if no buckets exist yet."""
        r = self.rec(name)
        if not r:
            return []
        rs = r.get("rate_seconds", {})
        if not rs:
            total = self.total_seconds(name)
            return [(self.rate(name), total)] if total > 0 else []
        out = []
        for k, sec in rs.items():
            try:
                out.append((float(k), sec))
            except Exception:
                pass
        return sorted(out, key=lambda x: x[0])

    def earnings(self, name):
        return sum((sec / 3600.0) * rate for rate, sec in self.rate_breakdown(name))

    def session_seconds(self):
        if self.running and self._session_start is not None:
            return time.time() - self._session_start
        return self._session_frozen

    # -- control ------------------------------------------------------------

    def pause(self):
        if not self.running:
            return
        self._session_frozen = self.session_seconds()
        self.running = False
        self.save(force=True)

    def resume(self):
        if self.running:
            return
        self.running = True
        self.idle_paused = False
        now = time.time()
        self._last_tick = now
        self._session_start = now
        self._session_frozen = 0.0

    def toggle(self):
        if self.running:
            self.pause()
        else:
            self.resume()

    def _subtract_idle(self, name, page, idle):
        """Remove idle seconds that were counted into the current buckets."""
        r = self.rec(name)
        if not r or not page:
            return
        r["pages"][page] = max(0, r["pages"].get(page, 0) - idle)
        today = datetime.date.today().isoformat()
        d = r["daily"].get(today)
        if d and page in d:
            d[page] = max(0, d[page] - idle)
        rk = "{:g}".format(self.rate(name))
        rs = r["rate_seconds"]
        if rk in rs:
            rs[rk] = max(0, rs[rk] - idle)
        if self._session_start is not None:
            self._session_start += idle  # exclude idle from the session clock

    def set_rate(self, value):
        self.set_rate_for(self.current_project, value)

    def set_rate_for(self, name, value):
        if name in NO_PROJECT:
            return
        self.config.setdefault("rates", {})[name] = value
        save_config(self.config)

    # -- the tick -----------------------------------------------------------

    def start_reader(self):
        """Spawn the Resolve-reader SUBPROCESS. It must be a separate process,
        not a thread: a Resolve scripting call blocks while Resolve is busy
        (playback/render) and holds Python's GIL, which would freeze the whole
        UI even from a background thread. The reader writes state.json; this
        process only ever reads that file (never calls Resolve)."""
        if self._reader_proc is not None and self._reader_proc.poll() is None:
            return
        try:
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            self._reader_proc = subprocess.Popen(
                [sys.executable, os.path.abspath(__file__),
                 "--reader", str(os.getpid())],
                cwd=os.path.dirname(os.path.abspath(__file__)),
                creationflags=flags)
            log("Started reader subprocess (pid {})".format(self._reader_proc.pid))
        except Exception:
            log_exception("start_reader")

    def stop_reader(self):
        if self._reader_proc is not None:
            try:
                self._reader_proc.terminate()
            except Exception:
                pass

    def refresh_from_reader(self):
        """Read the latest project/page the reader subprocess saw, and fold its
        signals into activity tracking. Returns (name, page). Fast file read —
        never blocks. Respawns the reader if it has died."""
        if self._reader_proc is not None and self._reader_proc.poll() is not None:
            log("Reader subprocess exited; restarting")
            self._reader_proc = None
            self.start_reader()

        st = read_state()
        if not st:
            self.connected = False
            return None, None

        fresh = (time.time() - st.get("ts", 0)) < (POLL_INTERVAL_S * 4)
        name = st.get("name")
        page = st.get("page")
        self.connected = bool(fresh and name)

        # Playhead movement (playback/scrubbing) = Resolve activity.
        tc = st.get("tc")
        if tc is not None:
            if self._last_tc is not None and tc != self._last_tc:
                self._last_resolve_activity = time.time()
            self._last_tc = tc

        # Resolve being BUSY (the reader's poll call blocked noticeably) also
        # means work is happening — e.g. playback that doesn't move the API's
        # reported timecode. Treat that as activity too.
        if st.get("blocked", 0) >= BUSY_THRESHOLD:
            self._last_resolve_activity = time.time()

        return name, page

    def activity_idle_seconds(self):
        """Seconds since the last activity that counts as 'working': keyboard/
        mouse input OR Resolve activity (playback/scrubbing/busy)."""
        return min(system_idle_seconds(),
                   time.time() - self._last_resolve_activity)

    def tick(self):
        now = time.time()
        delta = now - self._last_tick
        self._last_tick = now

        # Read the last project/page seen by the reader subprocess. Never call
        # Resolve here — it can block (playback) and freeze the UI.
        name, page = self.refresh_from_reader()

        if not self.running:
            # Auto-resume: if we stopped because of idle and the user has since
            # used the keyboard/mouse again, pick tracking back up by itself.
            # (Only after an idle auto-pause — a manual pause stays paused.)
            if self.idle_paused:
                limit = self.idle_limit_seconds()
                if limit > 0:
                    # Playhead movement is always Resolve activity (counts for
                    # both scopes). Keyboard/mouse counts when scope is "any",
                    # or when Resolve is the active window for scope "resolve".
                    scope = self.config.get("resume_scope", "any")
                    playhead_recent = (now - self._last_resolve_activity) < limit
                    input_recent = system_idle_seconds() < limit
                    if scope == "resolve":
                        input_ok = input_recent and resolve_is_foreground()
                    else:
                        input_ok = input_recent
                    if playhead_recent or input_ok:
                        log("Auto-resumed: activity detected after idle pause")
                        self.resume()
            return
        if delta <= 0 or delta > MAX_TICK_DELTA:
            return
        if name in NO_PROJECT or not page:
            return

        if name != self.current_project:
            log("Active project: {}".format(name))
            self.current_project = name
            self._session_start = now
        self.current_page = page

        r = self.rec(name)

        # Rate bucket: time tracked now is billed at the CURRENT rate; past time
        # keeps whatever rate it was tracked at. Seed legacy time (tracked before
        # rate history existed) into the current rate bucket once.
        rs = r["rate_seconds"]
        rate_key = "{:g}".format(self.rate(name))
        if not rs:
            legacy = sum(r["pages"].values())
            if legacy > 0:
                rs[rate_key] = legacy
        rs[rate_key] = rs.get(rate_key, 0) + delta

        r["pages"][page] = r["pages"].get(page, 0) + delta
        today = datetime.date.today().isoformat()
        d = r["daily"].setdefault(today, {})
        d[page] = d.get(page, 0) + delta

        # Idle auto-pause: if there's been no activity (keyboard/mouse OR
        # playhead movement) for the configured time, stop tracking and remove
        # the idle time we just counted (it wasn't real work).
        limit = self.idle_limit_seconds()
        if limit > 0:
            idle = self.activity_idle_seconds()
            if idle >= limit:
                self._subtract_idle(name, page, idle)
                self.idle_paused = True
                log("Auto-paused: idle {:.0f}s >= {:.0f}s".format(idle, limit))
                self.pause()
                return

        if now - self._last_save >= SAVE_INTERVAL:
            self.save()

    def save(self, force=False):
        save_data(self.data)
        self._last_save = time.time()


# ---------------------------------------------------------------------------
# CSV export (Excel opens it directly; utf-8-sig keeps €/ä correct)
# ---------------------------------------------------------------------------

def export_csv(poller, path):
    cur = poller.currency()
    projects = sorted(poller.data.get("projects", {}).keys())
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        # Billing section: one line per project per rate bucket.
        w.writerow(["Project", "Rate", "Time", "Hours", "Amount ({})".format(cur)])
        gsec = gamt = 0.0
        for name in projects:
            for rate, secs in poller.rate_breakdown(name):
                hours = secs / 3600.0
                amt = hours * rate
                gsec += secs
                gamt += amt
                w.writerow([name, "{:g}".format(rate), fmt_hms(secs),
                            "{:.2f}".format(hours), "{:.2f}".format(amt)])
        w.writerow([])
        w.writerow(["TOTAL", "", fmt_hms(gsec), "{:.2f}".format(gsec / 3600.0),
                    "{:.2f}".format(gamt)])
        # Per-page section for precise stats.
        w.writerow([])
        w.writerow(["Project", "Page", "Time", "Hours"])
        for name in projects:
            r = poller.rec(name)
            for p in PAGES:
                secs = r["pages"].get(p, 0)
                if secs > 0:
                    w.writerow([name, PAGE_LABELS.get(p, p), fmt_hms(secs),
                                "{:.2f}".format(secs / 3600.0)])


# ---------------------------------------------------------------------------
# Tkinter UI
# ---------------------------------------------------------------------------

def run_ui(poller):
    """Build and run the window. Returns True if the UI should be rebuilt
    (language/theme changed), False if the user quit."""
    import math
    import tkinter as tk

    lang = poller.lang()

    def T(key):
        return tr(lang, key)

    pal = resolve_theme(poller.theme())
    BG, FG, MUTED = pal["BG"], pal["FG"], pal["MUTED"]
    BAR_BG, BAR_FILL, ROW_HL = pal["BAR_BG"], pal["BAR_FILL"], pal["ROW_HL"]
    ENTRY_BG, BTN_BG, BTN_ACTIVE = pal["ENTRY_BG"], pal["BTN_BG"], pal["BTN_ACTIVE"]
    OVERDUE = pal["OVERDUE"]

    result = {"restart": False}
    built_system_dark = system_is_dark()  # to detect live OS theme changes

    root = tk.Tk()
    root.title("Resolve Time Tracker")
    root.attributes("-topmost", True)
    root.geometry("380x500")
    root.minsize(340, 440)
    root.configure(bg=BG)
    set_titlebar_dark(root, pal["DARK_TITLEBAR"])

    def L(parent, **kw):
        kw.setdefault("bg", BG)
        kw.setdefault("fg", FG)
        return tk.Label(parent, **kw)

    def B(parent, **kw):
        kw.setdefault("bg", BTN_BG)
        kw.setdefault("fg", FG)
        kw.setdefault("activebackground", BTN_ACTIVE)
        kw.setdefault("activeforeground", FG)
        kw.setdefault("relief", "flat")
        kw.setdefault("bd", 0)
        return tk.Button(parent, **kw)

    pad = {"padx": 10, "pady": 2}

    status_var = tk.StringVar(value=T("connecting"))
    project_var = tk.StringVar(value="—")
    session_var = tk.StringVar(value="00:00:00")
    total_big_var = tk.StringVar(value="")
    countdown_var = tk.StringVar(value="")
    today_var = tk.StringVar(value="")
    earned_var = tk.StringVar(value="")
    rate_var = tk.StringVar(value="0")
    ratelabel_var = tk.StringVar(value=T("rate").format(""))
    pause_label = tk.StringVar(value=T("pause"))

    # -- status row with breathing dot + gear --
    statusrow = tk.Frame(root, bg=BG)
    statusrow.pack(fill="x", **pad)
    DOT = 14
    dot_canvas = tk.Canvas(statusrow, width=DOT + 4, height=DOT + 4, bg=BG,
                           highlightthickness=0)
    dot_canvas.pack(side="left")
    dot = dot_canvas.create_oval(2, 2, DOT + 2, DOT + 2, fill=BAR_FILL, outline=BAR_FILL)
    L(statusrow, textvariable=status_var, font=("Segoe UI", 10)).pack(side="left", padx=6)

    # project row: name on the left, due countdown on the right (it belongs to
    # the project, so it reads as "<project>            Due in 3d 5h").
    projrow = tk.Frame(root, bg=BG)
    projrow.pack(fill="x", **pad)
    L(projrow, textvariable=project_var, anchor="w",
      font=("Segoe UI", 11, "bold")).pack(side="left")
    countdown_lbl = L(projrow, textvariable=countdown_var, fg=MUTED,
                      font=("Segoe UI", 9, "bold"))
    countdown_lbl.pack(side="right")

    # -- big session counter + persistent total underneath --
    L(root, textvariable=session_var, font=("Consolas", 32, "bold")).pack(pady=(6, 0))
    L(root, textvariable=total_big_var, fg=MUTED,
      font=("Consolas", 13)).pack(pady=(0, 6))

    # -- per-page bars (Canvas: green bar with % inside) --
    pages_canvas = tk.Canvas(root, bg=BG, highlightthickness=0, height=200)
    pages_canvas.pack(fill="both", expand=True, **pad)

    bottom = tk.Frame(root, bg=BG)
    bottom.pack(fill="x", **pad)
    L(bottom, textvariable=today_var, fg=MUTED).pack(side="left")

    ratebar = tk.Frame(root, bg=BG)
    ratebar.pack(fill="x", **pad)
    L(ratebar, textvariable=ratelabel_var).pack(side="left")
    rate_entry = tk.Entry(ratebar, textvariable=rate_var, width=8, bg=ENTRY_BG, fg=FG,
                          insertbackground=FG, relief="flat", justify="right")
    rate_entry.pack(side="left", padx=4)
    L(ratebar, textvariable=earned_var, fg=BAR_FILL,
      font=("Segoe UI", 12, "bold")).pack(side="right")

    def apply_rate(_event=None):
        try:
            txt = rate_var.get().strip().replace(",", ".")
            poller.set_rate(float(txt) if txt else 0.0)
        except ValueError:
            rate_var.set("{:g}".format(poller.rate(poller.current_project)))
        except Exception:
            log_exception("apply_rate")

    rate_entry.bind("<Return>", apply_rate)
    rate_entry.bind("<FocusOut>", apply_rate)

    def on_toggle():
        poller.toggle()
        pause_label.set(T("pause") if poller.running else T("resume"))

    def style_option_menu(om):
        om.config(bg=ENTRY_BG, fg=FG, activebackground=BTN_ACTIVE, activeforeground=FG,
                  relief="flat", highlightthickness=0)
        om["menu"].config(bg=ENTRY_BG, fg=FG)

    def open_stats():
        top = tk.Toplevel(root)
        top.title(T("stats"))
        top.configure(bg=BG)
        top.attributes("-topmost", True)
        top.geometry("540x420")
        set_titlebar_dark(top, pal["DARK_TITLEBAR"])
        cur = poller.currency()

        hdr = tk.Frame(top, bg=BG)
        hdr.pack(fill="x", padx=10, pady=(10, 2))
        for txt, w in ((T("project_col"), 20), (T("hours_col"), 11),
                       (T("rate_col"), 8), (T("amount_col"), 12)):
            L(hdr, text=txt, width=w, anchor="w",
              font=("Segoe UI", 9, "bold")).pack(side="left")

        body = tk.Frame(top, bg=BG)
        body.pack(fill="both", expand=True, padx=10)
        names = sorted(poller.data.get("projects", {}).keys())

        def make_row(name):
            f = tk.Frame(body, bg=BG)
            f.pack(fill="x", pady=1)
            L(f, text=name, width=20, anchor="w").pack(side="left")
            L(f, text=fmt_hms(poller.total_seconds(name)), width=11, anchor="w",
              fg=MUTED).pack(side="left")
            rv = tk.StringVar(value="{:g}".format(poller.rate(name)))
            e = tk.Entry(f, textvariable=rv, width=7, bg=ENTRY_BG, fg=FG,
                         insertbackground=FG, relief="flat", justify="right")
            e.pack(side="left", padx=(0, 6))
            L(f, text="{}{:,.2f}".format(cur, poller.earnings(name)), width=12,
              anchor="w", fg=BAR_FILL).pack(side="left")

            def apply(_e=None, n=name, var=rv):
                # Sets the FORWARD rate for this project (past time keeps its rate).
                try:
                    v = var.get().strip().replace(",", ".")
                    poller.set_rate_for(n, float(v) if v else 0.0)
                except ValueError:
                    var.set("{:g}".format(poller.rate(n)))

            e.bind("<Return>", apply)
            e.bind("<FocusOut>", apply)

        if names:
            for n in names:
                make_row(n)
        else:
            L(body, text=T("no_projects"), fg=MUTED).pack(anchor="w", pady=6)

        note = L(top, text="", fg=MUTED, font=("Segoe UI", 8))
        note.pack(anchor="w", padx=12)

        def do_export():
            from tkinter import filedialog
            path = filedialog.asksaveasfilename(
                defaultextension=".csv", filetypes=[("CSV", "*.csv")],
                initialfile="time_tracker.csv")
            if not path:
                return
            try:
                export_csv(poller, path)
                note.config(text="{} {}".format(T("exported"), path))
            except Exception:
                log_exception("export_csv")
                note.config(text=T("export_failed"))

        btns = tk.Frame(top, bg=BG)
        btns.pack(fill="x", padx=10, pady=10)
        B(btns, text=T("export_csv"), command=do_export, pady=6).pack(
            side="left", expand=True, fill="x", padx=(0, 4))
        B(btns, text=T("close"), command=top.destroy, pady=6).pack(
            side="left", expand=True, fill="x", padx=(4, 0))

    def open_settings():
        top = tk.Toplevel(root)
        top.title(T("settings"))
        top.configure(bg=BG)
        top.attributes("-topmost", True)
        top.geometry("380x470")
        set_titlebar_dark(top, pal["DARK_TITLEBAR"])

        cur_v = tk.StringVar(value=poller.currency())
        rt_labels = [T("rt_" + k) for k in RATE_TYPE_KEYS]
        rt_label_to_key = {T("rt_" + k): k for k in RATE_TYPE_KEYS}
        type_v = tk.StringVar(value=T("rt_" + poller.rate_type()))
        lang_label_to_code = {name: code for code, name in LANGS}
        lang_v = tk.StringVar(value=dict(LANGS).get(lang, "English"))
        theme_keys = ["dark", "light", "system"]
        theme_label_to_key = {T("theme_" + k): k for k in theme_keys}
        theme_v = tk.StringVar(value=T("theme_" + poller.theme()))
        showdue_v = tk.BooleanVar(value=poller.show_due())
        due_v = tk.StringVar(value=poller.due_for(poller.current_project) or "")
        idle_v = tk.StringVar(value="{:g}".format(
            float(poller.config.get("idle_minutes", 0) or 0)))
        resume_keys = ["any", "resolve"]
        resume_label_to_key = {T("resume_" + k): k for k in resume_keys}
        resume_v = tk.StringVar(
            value=T("resume_" + poller.config.get("resume_scope", "any")))

        def row(label):
            f = tk.Frame(top, bg=BG)
            f.pack(fill="x", padx=10, pady=4)
            L(f, text=label, width=16, anchor="w").pack(side="left")
            return f

        f = row(T("language"))
        style_option_menu(tk.OptionMenu(f, lang_v, *[n for _, n in LANGS]))
        f.winfo_children()[-1].pack(side="left")

        f = row(T("theme"))
        style_option_menu(tk.OptionMenu(f, theme_v, *[T("theme_" + k) for k in theme_keys]))
        f.winfo_children()[-1].pack(side="left")

        f = row(T("currency"))
        tk.Entry(f, textvariable=cur_v, width=8, bg=ENTRY_BG, fg=FG,
                 insertbackground=FG, relief="flat").pack(side="left")

        f = row(T("rate_type"))
        style_option_menu(tk.OptionMenu(f, type_v, *rt_labels))
        f.winfo_children()[-1].pack(side="left")

        f = row(T("due_countdown"))
        tk.Checkbutton(f, variable=showdue_v, text=T("show"), bg=BG, fg=FG,
                       selectcolor=ENTRY_BG, activebackground=BG,
                       activeforeground=FG).pack(side="left")

        f = row(T("due_project"))
        tk.Entry(f, textvariable=due_v, width=18, bg=ENTRY_BG, fg=FG,
                 insertbackground=FG, relief="flat").pack(side="left")
        L(top, text=T("due_format"), fg=MUTED, font=("Segoe UI", 8)).pack(padx=12, anchor="w")

        f = row(T("idle_label"))
        tk.Entry(f, textvariable=idle_v, width=6, bg=ENTRY_BG, fg=FG,
                 insertbackground=FG, relief="flat", justify="right").pack(side="left")
        L(f, text=T("idle_hint"), fg=MUTED, font=("Segoe UI", 8)).pack(side="left", padx=6)

        f = row(T("resume_label"))
        style_option_menu(tk.OptionMenu(f, resume_v, *[T("resume_" + k) for k in resume_keys]))
        f.winfo_children()[-1].pack(side="left")

        def save_settings():
            old_lang, old_theme = poller.lang(), poller.theme()
            new_lang = lang_label_to_code.get(lang_v.get(), "en")
            new_theme = theme_label_to_key.get(theme_v.get(), "dark")
            try:
                idle_min = max(0.0, float(idle_v.get().strip().replace(",", ".") or 0))
            except ValueError:
                idle_min = float(poller.config.get("idle_minutes", 0) or 0)
            poller.update_config(
                currency=cur_v.get().strip() or "€",
                rate_type=rt_label_to_key.get(type_v.get(), "brutto"),
                show_due=bool(showdue_v.get()),
                lang=new_lang,
                theme=new_theme,
                idle_minutes=idle_min,
                resume_scope=resume_label_to_key.get(resume_v.get(), "any"),
            )
            dd = due_v.get().strip()
            valid = False
            if dd:
                for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M"):
                    try:
                        datetime.datetime.strptime(dd, fmt)
                        valid = True
                        break
                    except Exception:
                        pass
            if poller.current_project:
                poller.set_due(poller.current_project, dd if valid else None)

            if new_lang != old_lang or new_theme != old_theme:
                # Rebuild the whole window with the new language/palette.
                result["restart"] = True
                root.destroy()
            else:
                refresh()
                top.destroy()

        btns = tk.Frame(top, bg=BG)
        btns.pack(fill="x", padx=10, pady=12)
        B(btns, text=T("save"), command=save_settings, pady=4).pack(
            side="left", expand=True, fill="x", padx=(0, 4))
        B(btns, text=T("cancel"), command=top.destroy, pady=4).pack(
            side="left", expand=True, fill="x", padx=(4, 0))

        # version, small and out of the way
        L(top, text="v" + VERSION, fg=MUTED,
          font=("Segoe UI", 7)).pack(side="bottom", anchor="e", padx=6, pady=(0, 3))

    # gear button at the right of the status row
    B(statusrow, text="⚙", command=open_settings, bg=BG, activebackground=BG,
      fg=MUTED, font=("Segoe UI", 13)).pack(side="right")

    # action buttons
    btnrow = tk.Frame(root, bg=BG)
    btnrow.pack(fill="x", **pad)
    B(btnrow, textvariable=pause_label, command=on_toggle, pady=6).pack(
        side="left", expand=True, fill="x", padx=(0, 4))
    B(btnrow, text=T("stats"), command=open_stats, pady=6).pack(
        side="left", expand=True, fill="x", padx=(4, 0))

    shown = {"project": None}
    phase = {"v": 0.0}

    def draw_pages(proj):
        c = pages_canvas
        c.delete("all")
        w = c.winfo_width()
        if w <= 1:
            w = 360
        r = poller.rec(proj) if proj else None
        pages = r["pages"] if r else {}
        total = sum(pages.values()) if pages else 0
        rows = [(p, pages.get(p, 0)) for p in PAGES if pages.get(p, 0) > 0]
        rowh = 28
        barh = 20
        namex, timex, barx0, barx1 = 10, 78, 158, w - 10
        active = poller.current_page if (poller.connected and poller.running) else None
        for i, (p, secs) in enumerate(rows):
            y = i * rowh + 4
            midy = y + barh / 2
            if p == active:
                # subtle highlight behind the active page's row
                c.create_rectangle(2, y - 4, w - 2, y + barh + 4,
                                   fill=ROW_HL, outline="")
            c.create_text(namex, midy, anchor="w", text=PAGE_LABELS.get(p, p),
                          fill=FG, font=("Segoe UI", 9))
            c.create_text(timex, midy, anchor="w", text=fmt_hms(secs),
                          fill=MUTED, font=("Consolas", 9))
            share = (secs / total) if total > 0 else 0.0
            c.create_rectangle(barx0, y, barx1, y + barh, fill=BAR_BG, outline="")
            fillx = barx0 + (barx1 - barx0) * share
            if fillx > barx0 + 1:
                c.create_rectangle(barx0, y, fillx, y + barh, fill=BAR_FILL, outline="")
            c.create_text((barx0 + barx1) / 2, midy, text="{:.0f}%".format(share * 100),
                          fill="#ffffff", font=("Segoe UI", 8, "bold"))

    def refresh():
        proj = poller.current_project
        if poller.connected:
            if poller.running:
                status_var.set(T("tracking"))
            else:
                status_var.set(T("paused_idle") if poller.idle_paused else T("paused"))
        else:
            status_var.set(T("connecting_long"))
        project_var.set(proj or "—")
        root.title("{} — Resolve Time Tracker".format(proj) if proj
                   else "Resolve Time Tracker")
        session_var.set(fmt_hms(poller.session_seconds()))
        total = poller.total_seconds(proj) if proj else 0
        total_big_var.set("{}  {}".format(T("total"), fmt_hms(total)))
        today_var.set("{}: {}".format(T("today"), fmt_hms(poller.today_seconds(proj))))
        earned_var.set("{}{:,.2f}".format(poller.currency(), poller.earnings(proj)))
        ratelabel_var.set(T("rate").format(T("rt_" + poller.rate_type()).lower()))

        cd = poller.due_countdown(proj) if (proj and poller.show_due()) else None
        if cd:
            days, hrs, mins, overdue = cd
            ud, uh, um = T("u_d"), T("u_h"), T("u_m")
            if days > 0:
                part = "{}{} {}{}".format(days, ud, hrs, uh)
            elif hrs > 0:
                part = "{}{} {}{}".format(hrs, uh, mins, um)
            else:
                part = "{}{}".format(mins, um)
            countdown_var.set("{} {}".format(T("overdue_by") if overdue else T("due_in"), part))
            countdown_lbl.config(fg=OVERDUE if overdue else MUTED)
        else:
            countdown_var.set("")

        draw_pages(proj)

        if proj != shown["project"]:
            shown["project"] = proj
            if root.focus_get() is not rate_entry:
                rate_var.set("{:g}".format(poller.rate(proj)))

    def animate():
        # Breathing green dot while tracking; amber when paused; gray when off.
        try:
            if poller.connected and poller.running:
                t = (math.sin(phase["v"]) + 1) / 2.0
                g = int(80 + t * 160)
                col = "#%02x%02x%02x" % (8, g, 46)
                phase["v"] += 0.18
            elif poller.connected:
                col = "#e0a83a"
            else:
                col = "#777777"
            dot_canvas.itemconfig(dot, fill=col, outline=col)
        except Exception:
            pass
        root.after(60, animate)

    def bring_to_front():
        # Clicking the Resolve menu item while we're already running touches
        # RAISE_FILE; pull the existing window up and focus it (nice UX vs. a
        # silent no-op).
        try:
            if os.path.exists(RAISE_FILE):
                os.remove(RAISE_FILE)
                root.deiconify()
                root.lift()
                root.attributes("-topmost", True)
                root.after(50, root.focus_force)
        except Exception:
            pass

    def loop():
        try:
            poller.tick()
            touch_lock()
            refresh()
            bring_to_front()
            # Follow the OS theme live when set to "System".
            if poller.theme() == "system" and system_is_dark() != built_system_dark:
                log("System theme changed; rebuilding UI")
                result["restart"] = True
                root.destroy()
                return
        except Exception:
            log_exception("ui loop")
        root.after(TICK_INTERVAL_MS, loop)

    def on_quit():
        try:
            poller.save(force=True)
            poller.stop_reader()
        except Exception:
            log_exception("on_quit")
        result["restart"] = False
        root.destroy()

    pause_label.set(T("pause") if poller.running else T("resume"))
    root.protocol("WM_DELETE_WINDOW", on_quit)
    animate()
    loop()
    root.mainloop()
    return result["restart"]


def main():
    log("=== tracker_poller starting (pid {}) ===".format(os.getpid()))
    if lock_is_held_by_other():
        log("Another poller instance appears to be running; exiting.")
        return
    touch_lock()
    poller = Poller()
    poller.start_reader()  # separate process polls Resolve (keeps UI responsive)
    try:
        while True:
            restart = run_ui(poller)
            if not restart:
                break
            log("Rebuilding UI (language/theme change)")
    except Exception:
        log_exception("Fatal")
    finally:
        poller.stop_reader()
        release_lock()
        log("=== tracker_poller stopped ===")


if __name__ == "__main__":
    if "--reader" in sys.argv:
        ppid = None
        idx = sys.argv.index("--reader")
        if idx + 1 < len(sys.argv):
            try:
                ppid = int(sys.argv[idx + 1])
            except ValueError:
                ppid = None
        run_reader(ppid)
    else:
        main()
