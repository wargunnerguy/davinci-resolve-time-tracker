# DaVinci Resolve Time Tracker

Per-project, **per-page** time tracking for DaVinci Resolve Studio (Edit / Color /
Fairlight / Deliver / …), with hourly rate → earnings.
---

## How it works (two parts)

A Resolve script window **cannot update itself on a timer** (Resolve limitation),
so the tracker is split:

| Part | Runs in | Role |
|------|---------|------|
| **`tracker_poller.py`** | System Python (`C:\Python314`), separate process | The whole app. Connects to Resolve, polls the current page every second, writes `sessions.json`, and shows a live always-on-top **Tkinter** window. |
| **`ResolveTimeTracker.py`** | Inside Resolve (Workspace → Scripts) | A tiny **launcher** — just starts the poller so you have one window. No window of its own. |

The poller is the **sole writer** of `sessions.json`; settings/rates live in `config.json`.

### Features (live window)

- Breathing status dot, big session clock + persistent **Total**
- Green **per-page % bars**, with the **active page row highlighted**
- **Pause / Resume**, **rate → earnings**
- **Forward-only rate**: changing the rate only affects time tracked from then on; past time keeps the rate it was tracked at (great for invoicing)
- Per-project **due-date countdown** beside the project name
- **⚙ Settings**: language (English / Eesti), theme (Dark / Light / System), currency, rate type (Gross/Net/Employer cost), due date
- **Stats / Export**: per-project table with editable rate + **CSV export** (Excel-ready, billing + per-page sections)

---

## Requirements

- DaVinci Resolve **Studio** (scripting API required) on **Windows 10/11**
- **External scripting enabled**: Resolve → Preferences → System → General →
  *External scripting using* = **Local**
- A **system Python** with Tkinter (e.g. `C:\Python314`) — used by the poller.
  (Tkinter ships with standard CPython on Windows.)
- No pip packages — stdlib + Resolve's bundled `DaVinciResolveScript` module only.

---

## Install

1. **Copy both `ResolveTimeTracker.py` and `tracker_poller.py`** into Resolve's
   Scripts/Utility folder (keep them together — the launcher finds the poller
   next to itself):
   ```
   C:\Users\<you>\AppData\Roaming\Blackmagic Design\DaVinci Resolve\Support\Fusion\Scripts\Utility\
   ```

2. **Restart Resolve.** The tracker now appears under
   **Workspace → Scripts → Utility → ResolveTimeTracker**.

That's it — the launcher locates a system Python automatically (it looks for
`C:\Python3*\pythonw.exe` and the per-user Python install, then `pythonw` on
`PATH`). If your Python lives somewhere unusual, set `PYTHONW` at the top of
`ResolveTimeTracker.py`.

> **Developers:** instead of copying, point Resolve at your working copy with a
> stub in the Utility folder so edits take effect without re-copying:
> ```python
> import os
> os.environ["RESOLVE_TIMETRACKER_REPO"] = r"<repo>"   # your local working copy
> exec(open(r"<repo>\ResolveTimeTracker.py", encoding="utf-8").read())
> ```
> `encoding="utf-8"` is required — the repo files contain `€`/`●`. The
> `RESOLVE_TIMETRACKER_REPO` env var lets the launcher find the poller when run
> this way, and keeps your personal path out of the repo.

---

## Usage

1. In Resolve: **Workspace → Scripts → Utility → ResolveTimeTracker** → launches
   the live window (always-on-top) and begins tracking the current project/page.
2. Work as normal. The window shows the session clock, total, per-page bars,
   earnings, and the due countdown. Use **Pause/Resume**, the **rate** field, the
   **⚙ Settings** gear, and **Stats / Export**.

You can also start the app directly:
```powershell
C:\Python314\pythonw.exe "<repo>\tracker_poller.py"
```

---

## Data files (`%APPDATA%\ResolveTimeTracker\`)

| File | Contents |
|------|----------|
| `sessions.json` | Per-project per-page time (poller is sole writer) |
| `config.json` | Settings: currency, rate type, language, theme, idle/resume, per-project rates & due dates |
| `state.json` | Current project/page/timecode (written by the reader process, read by the UI) |
| `poller.lock` | Single-instance lock (also drives the "Live: running" status) |
| `poller.log` | Live tracker log |
| `tracker.log` | Reports window log |

---

## Troubleshooting

**Reports window doesn't appear in Resolve** — confirm the stub is in
`Support\Fusion\Scripts\Utility\` and restart Resolve once.

**Live window won't start / "Live: stopped"** — check `poller.log`. Ensure
external scripting is enabled in Resolve, Resolve is open, and a system Python
with Tkinter is installed (if it's in an unusual place, set `PYTHONW` at the top
of `ResolveTimeTracker.py`).

**Times look wrong** — only one poller should run at a time (the lock enforces
this). The live window is the source of truth; the reports window is a snapshot.
