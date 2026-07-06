# DaVinci Resolve Time Tracker

A lightweight, always-on-top **time tracker for DaVinci Resolve Studio** — tracks
how long you spend per project and **per page** (Media, Cut, Edit, Fusion, Color,
Fairlight, Deliver, Photo), turns it into **earnings** at your hourly rate, and
exports a tidy CSV for invoicing.

One file. No installs beyond Python. No accounts, no cloud — your data stays on
your machine.

---

## Screenshots

| Main window | Mini pill | Stats / Export |
|:---:|:---:|:---:|
| ![Main window](screenshots/main.png) | ![Mini pill](screenshots/mini.png) | ![Stats and export](screenshots/stats.png) |

---

## What it does

- ⏱️ **Live tracking** of the active project and page, second by second
- 📊 **Per-page bars** — see where your time goes; the active page lights up
- 💶 **Earnings** from your hourly rate, with **today's** and **total** shown
- ⏸️ **Idle auto-pause** (with a live "idle for 3m" readout) and **auto-resume**
- 🙋 **"Welcome back" prompt** — when you return from being idle, choose whether
  the time away counts as work or is discarded
- 🚀 **Start with Resolve** — optionally auto-start the tracker (or get asked)
  whenever DaVinci Resolve opens
- 🚪 **Closes with Resolve** — the tracker quits by itself shortly after you
  close Resolve (optional, on by default)
- 🏷️ **Rename-safe** — rename a project in Resolve and its tracked history,
  rate and due date follow along automatically
- 💰 **Default rate** — new projects inherit the last hourly rate you set
- 🪟 **Mini mode** — shrink to a slim pill showing just what you choose
- 🧾 **Stats & CSV export** with a history view and date ranges, ready for invoicing
- 🌗 **Themes & languages** — Dark / Light / System, and 13 UI languages
  (English, Eesti, Latviešu, Lietuvių, Español, Português, Français, Deutsch,
  中文, 日本語, 한국어, हिन्दी, العربية)
- 🔝 **Always-on-top by default** (can be turned off in Settings)

---

## Install

**Easiest — double-click `install.bat`.** It copies the app into Resolve's
Scripts folder, checks you have Python, and reminds you of the one Resolve setting.

**Or manually:** copy `ResolveTimeTracker.py` into
`…\Blackmagic Design\DaVinci Resolve\Support\Fusion\Scripts\Utility\`.

Then, once:

1. In Resolve: **Preferences → System → General → *External scripting using* = Local**
2. **Restart Resolve**

Launch it from **Workspace → Scripts → ResolveTimeTracker**.

### Requirements

- DaVinci Resolve **Studio** on **Windows 10/11**
- **Python 3** for Windows ([python.org](https://www.python.org/downloads/) — keep
  the defaults; Tkinter is included). If it's missing, the app tells you.

That's the whole list — no pip packages to install.

---

## Tips

- Click the **rate** field to change your hourly rate (past time keeps its old rate).
- Hit **Mini** to dock a compact pill out of the way; double-click it to expand.
- **Settings (⚙)** covers language, theme, currency, billing (simple or with tax
  views), idle behaviour, the mini pill, always-on-top, and more.
- **"When Resolve opens"** (in Settings → General) can start the tracker with
  Resolve automatically or ask you first — no need to visit the Scripts menu.
- **Stats** opens the per-project table, history, and CSV export.
- Prefer a named process? Set `"named_exe": true` in `config.json` to show up
  in Task Manager as **`ResolveTimeTracker.exe`** (a signed, byte-identical
  copy of your own Python). It's off by default because some antivirus
  products flag renamed programs — plain `pythonw.exe` passes quietly.

---

## Troubleshooting

**Not in the Scripts menu?** Re-run `install.bat` (or confirm the file is in the
`Scripts\Utility` folder) and restart Resolve.

**Clicking it does nothing?** Make sure external scripting is set to **Local**,
Resolve is open, and Python is installed. Details are logged to
`%APPDATA%\ResolveTimeTracker\poller.log`.

---

## For developers / how it works

<details>
<summary>Architecture, data files, and running from source</summary>

A Resolve script window can't update on a timer, so the single
`ResolveTimeTracker.py` plays four roles depending on how it's started:

| Started as | Runs in | Role |
|------------|---------|------|
| Scripts menu (no args) | Resolve's Python | **Launcher** — starts the tracker in system Python, or surfaces it if already open. No window of its own. |
| `--poller` | System Python | **The tracker** — the live Tkinter window; sole writer of `sessions.json`. |
| `--reader <pid>` | System Python | Polls Resolve into `state.json` (separate process so playback never freezes the UI). |
| `--watch` | System Python | **Watcher** — optional login-time sentinel behind Settings → "When Resolve opens"; watches for the Resolve process and starts (or offers to start) the tracker. |

Run from source (with a console, to see errors):
```powershell
C:\Python314\python.exe "<repo>\ResolveTimeTracker.py" --poller
```

Data lives in `%APPDATA%\ResolveTimeTracker\`:

| File | Contents |
|------|----------|
| `sessions.json` | Per-project, per-page time (tracker is sole writer) |
| `config.json` | Settings & per-project rates / due dates |
| `state.json` | Current project/page (reader → UI) |
| `poller.lock` | Single-instance lock (PID-verified) |
| `watcher.lock` | Single-instance lock for the `--watch` role |
| `poller.log` | Log for all roles |
| `runtime\` | Only with the opt-in `"named_exe": true` setting: a byte-identical copy of your `pythonw.exe` named `ResolveTimeTracker.exe` (plus its DLLs and a `._pth` file), so the app shows up in Task Manager under its own name. The Python Software Foundation code signature stays valid on the copy. Rebuilt automatically after a Python upgrade. |

No third-party dependencies — Python stdlib (incl. Tkinter) plus Resolve's bundled
`DaVinciResolveScript` module. Icons are original art embedded as base64 in the file.

</details>

---

## Contributing

Ideas, bug reports and pull requests are all welcome:

- **Found a bug or have an idea?** [Open an issue](../../issues) — a screenshot
  and the tail of `%APPDATA%\ResolveTimeTracker\poller.log` help a lot.
- **Want to add something yourself?** Fork, branch, and send a pull request.
  Please keep the project's ground rules: everything stays in the **one**
  `ResolveTimeTracker.py` file, **stdlib only** (no pip packages), and Tkinter
  is only imported inside the tracker role.
- **Run the tests** before sending a PR:

  ```powershell
  python -m unittest discover tests -v
  ```

  CI runs the same tests on every pull request.

### Roadmap / ideas

- macOS support (the Windows-specific bits are already isolated behind helpers)
- Weekly / monthly summary view

---

## License

[0BSD](LICENSE) — free for any use, no conditions, no attribution required.
Just download it and use it.

---

## Disclaimer

Not affiliated with, authorized, or endorsed by Blackmagic Design.
"DaVinci Resolve" is a trademark of Blackmagic Design Pty Ltd. This is an
independent, unofficial tool.
