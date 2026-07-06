"""Unit tests for the accounting core of ResolveTimeTracker.

Only the Tk-free parts are tested (Poller, data/config IO, CSV export,
formatting). The module stores its data under %APPDATA%\\ResolveTimeTracker,
so APPDATA is pointed at a throwaway directory BEFORE the import — the paths
are computed at import time.

Run from the repo root:  python -m unittest discover tests -v
"""

import datetime
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

_TMP = tempfile.mkdtemp(prefix="rtt_test_")
os.environ["APPDATA"] = _TMP
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ResolveTimeTracker as rtt  # noqa: E402  (needs APPDATA set first)


def _fresh_poller(project="TestProject", page="edit"):
    """A Poller with clean data files, idle-pause off, and the reader faked
    out to report a fixed project/page (no Resolve involved)."""
    for f in (rtt.SESSIONS_FILE, rtt.CONFIG_FILE, rtt.STATE_FILE):
        if os.path.exists(f):
            os.remove(f)
    p = rtt.Poller()
    p.config["idle_minutes"] = 0  # determinism: never auto-pause in tests
    p.refresh_from_reader = lambda: (project, page)
    return p


def _tick_seconds(p, secs):
    """Run one tick() that credits exactly `secs` seconds."""
    p._last_tick = time.time() - secs
    p.tick()


class TestFormatting(unittest.TestCase):
    def test_fmt_hms(self):
        self.assertEqual(rtt.fmt_hms(0), "00:00:00")
        self.assertEqual(rtt.fmt_hms(3661), "01:01:01")
        self.assertEqual(rtt.fmt_hms(-5), "00:00:00")
        self.assertEqual(rtt.fmt_hms(359999), "99:59:59")

    def test_fmt_hm(self):
        self.assertEqual(rtt.fmt_hm(59), "0m")
        self.assertEqual(rtt.fmt_hm(60), "1m")
        self.assertEqual(rtt.fmt_hm(3660), "1h 1m")


class TestTickAccounting(unittest.TestCase):
    def test_tick_credits_delta_to_page_daily_and_rate_bucket(self):
        p = _fresh_poller()
        _tick_seconds(p, 2.0)
        r = p.data["projects"]["TestProject"]
        today = datetime.date.today().isoformat()
        # delta tolerance: a slow test runner adds a little real time on top
        self.assertAlmostEqual(r["pages"]["edit"], 2.0, delta=0.5)
        self.assertAlmostEqual(r["daily"][today]["edit"], 2.0, delta=0.5)
        self.assertAlmostEqual(r["rate_seconds"]["0"], 2.0, delta=0.5)

    def test_gap_credits_only_one_tick(self):
        # Sleep/hibernate: a huge delta must NOT be billed as work.
        p = _fresh_poller()
        _tick_seconds(p, 1800.0)  # "slept" 30 minutes
        r = p.data["projects"]["TestProject"]
        self.assertAlmostEqual(r["pages"]["edit"],
                               rtt.TICK_INTERVAL_MS / 1000.0, places=2)

    def test_gap_does_not_inflate_session_clock(self):
        p = _fresh_poller()
        _tick_seconds(p, 2.0)  # establishes the project/session
        p._session_start = time.time() - 2.0
        _tick_seconds(p, 1800.0)
        # Session grew by roughly one tick, not by the 30-minute gap.
        self.assertLess(p.session_seconds(), 10.0)

    def test_no_project_does_not_count_session_time(self):
        p = _fresh_poller()
        p.refresh_from_reader = lambda: (None, None)
        p._session_start = time.time() - 50.0
        _tick_seconds(p, 50.0)
        self.assertLess(p.session_seconds(), 5.0)

    def test_untitled_project_is_never_tracked(self):
        # Resolve's auto-created placeholder (flashes by on startup/shutdown).
        p = _fresh_poller(project="Untitled Project")
        _tick_seconds(p, 5.0)
        self.assertEqual(p.data["projects"], {})

    def test_paused_ticks_credit_nothing(self):
        p = _fresh_poller()
        p.pause()
        _tick_seconds(p, 5.0)
        self.assertNotIn("TestProject", p.data["projects"])

    def test_rate_change_is_forward_only(self):
        p = _fresh_poller()
        p.set_rate_for("TestProject", 10.0)
        _tick_seconds(p, 2.0)
        p.set_rate_for("TestProject", 20.0)
        _tick_seconds(p, 3.0)
        rs = p.data["projects"]["TestProject"]["rate_seconds"]
        self.assertAlmostEqual(rs["10"], 2.0, delta=0.5)
        self.assertAlmostEqual(rs["20"], 3.0, delta=0.5)
        # earnings sum exactly over the recorded buckets
        self.assertAlmostEqual(
            p.gross_earnings("TestProject"),
            (rs["10"] / 3600.0) * 10 + (rs["20"] / 3600.0) * 20, places=9)

    def test_legacy_time_seeded_into_current_bucket_once(self):
        p = _fresh_poller()
        p.set_rate_for("TestProject", 15.0)
        rec = p.rec("TestProject")
        rec["pages"]["color"] = 100.0  # pre-rate-history time
        _tick_seconds(p, 1.0)
        rs = p.data["projects"]["TestProject"]["rate_seconds"]
        self.assertAlmostEqual(rs["15"], 101.0, delta=0.5)


class TestIdleSubtraction(unittest.TestCase):
    def test_subtract_idle_capped_to_session(self):
        p = _fresh_poller()
        _tick_seconds(p, 8.0)
        p._session_start = time.time() - 8.0
        before = p.data["projects"]["TestProject"]["pages"]["edit"]
        p._subtract_idle("TestProject", "edit", 99999.0)  # machine idle for "hours"
        after = p.data["projects"]["TestProject"]["pages"]["edit"]
        self.assertGreaterEqual(after, 0.0)
        self.assertAlmostEqual(before - after, 8.0, delta=0.5)

    def test_subtract_idle_never_negative(self):
        p = _fresh_poller()
        _tick_seconds(p, 1.0)
        p._session_start = None  # uncapped path
        p._subtract_idle("TestProject", "edit", 500.0)
        self.assertGreaterEqual(
            p.data["projects"]["TestProject"]["pages"]["edit"], 0.0)


class TestIdleVerdict(unittest.TestCase):
    """The "welcome back — keep or discard?" flow around idle auto-pause."""

    def _idle_pause(self, p, idle=120.0):
        """Drive one tick that trips the idle auto-pause with `idle` idle secs."""
        p.config["idle_minutes"] = 1  # 60 s limit
        p.activity_idle_seconds = lambda: idle
        _tick_seconds(p, 2.0)

    def test_auto_pause_captures_pending_idle(self):
        p = _fresh_poller()
        self._idle_pause(p)
        self.assertFalse(p.running)
        self.assertTrue(p.idle_paused)
        self.assertIsNotNone(p._pending_idle)
        self.assertEqual(p._pending_idle["project"], "TestProject")
        self.assertEqual(p._pending_idle["page"], "edit")
        self.assertAlmostEqual(p._pending_idle["since"], p._idle_since)

    def test_resume_turns_pending_into_verdict(self):
        p = _fresh_poller()
        self._idle_pause(p, idle=120.0)
        p.resume()
        v = p.idle_verdict
        self.assertIsNotNone(v)
        self.assertIsNone(p._pending_idle)
        # Away span = idle before the pause + (here: none) paused time.
        self.assertAlmostEqual(v["until"] - v["since"], 120.0, delta=2.0)

    def test_restore_idle_credits_all_buckets(self):
        p = _fresh_poller()
        _tick_seconds(p, 2.0)
        r = p.data["projects"]["TestProject"]
        today = datetime.date.today().isoformat()
        before = (r["pages"]["edit"], r["daily"][today]["edit"],
                  r["rate_seconds"]["0"])
        now = time.time()
        p.restore_idle({"project": "TestProject", "page": "edit",
                        "rate_key": "0", "date": today,
                        "since": now - 300.0, "until": now})
        self.assertAlmostEqual(r["pages"]["edit"] - before[0], 300.0, places=2)
        self.assertAlmostEqual(r["daily"][today]["edit"] - before[1], 300.0, places=2)
        self.assertAlmostEqual(r["rate_seconds"]["0"] - before[2], 300.0, places=2)

    def test_restore_idle_creates_missing_daily_bucket(self):
        p = _fresh_poller()
        _tick_seconds(p, 2.0)
        r = p.data["projects"]["TestProject"]
        now = time.time()
        p.restore_idle({"project": "TestProject", "page": "edit",
                        "rate_key": "0", "date": "2000-01-01",
                        "since": now - 60.0, "until": now})
        self.assertAlmostEqual(r["daily"]["2000-01-01"]["edit"], 60.0, places=2)

    def test_prompt_disabled_stays_silent(self):
        p = _fresh_poller()
        p.config["idle_prompt"] = False
        self._idle_pause(p)
        self.assertIsNone(p._pending_idle)
        p.resume()
        self.assertIsNone(p.idle_verdict)

    def test_manual_pause_resume_has_no_verdict(self):
        p = _fresh_poller()
        _tick_seconds(p, 2.0)
        p.pause()
        p.resume()
        self.assertIsNone(p.idle_verdict)


class TestPageRates(unittest.TestCase):
    def test_effective_rate_fallback_chain(self):
        p = _fresh_poller()
        p.set_rate_for("TestProject", 10.0)
        p.config["page_rates"] = {"TestProject": {"color": 30.0}}
        p.config["page_rates_on"] = False  # feature off -> project rate wins
        self.assertEqual(p.effective_rate("TestProject", "color"), 10.0)
        p.config["page_rates_on"] = True
        self.assertEqual(p.effective_rate("TestProject", "color"), 30.0)
        self.assertEqual(p.effective_rate("TestProject", "edit"), 10.0)

    def test_tick_buckets_at_override_rate(self):
        p = _fresh_poller(page="color")
        p.set_rate_for("TestProject", 10.0)
        p.config["page_rates_on"] = True
        p.config["page_rates"] = {"TestProject": {"color": 30.0}}
        _tick_seconds(p, 2.0)
        rs = p.data["projects"]["TestProject"]["rate_seconds"]
        self.assertIn("30", rs)
        self.assertAlmostEqual(rs["30"], 2.0, delta=0.5)
        # earnings integrate the override bucket exactly
        self.assertAlmostEqual(
            p.gross_earnings("TestProject"), (rs["30"] / 3600.0) * 30, places=9)

    def test_config_round_trip(self):
        if os.path.exists(rtt.CONFIG_FILE):
            os.remove(rtt.CONFIG_FILE)
        with open(rtt.CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump({"page_rates_on": True,
                       "page_rates": {"P": {"color": 25}},
                       "recap": False, "last_recap": "2026-05"}, f)
        cfg = rtt.load_config()
        self.assertTrue(cfg["page_rates_on"])
        self.assertEqual(cfg["page_rates"]["P"]["color"], 25)
        self.assertFalse(cfg["recap"])
        self.assertEqual(cfg["last_recap"], "2026-05")


class TestIdleDaily(unittest.TestCase):
    def _idle_pause(self, p, idle=120.0):
        p.config["idle_minutes"] = 1
        p.activity_idle_seconds = lambda: idle
        _tick_seconds(p, 2.0)

    def test_idle_span_recorded_on_resume(self):
        p = _fresh_poller()
        self._idle_pause(p)
        p.resume()
        idle = p.data.get("idle_daily", {})
        self.assertAlmostEqual(sum(idle.values()), 120.0, delta=2.0)

    def test_keep_verdict_moves_idle_back_to_work(self):
        p = _fresh_poller()
        self._idle_pause(p)
        p.resume()
        p.restore_idle(p.idle_verdict)
        self.assertLessEqual(sum(p.data.get("idle_daily", {}).values()), 1.0)


class TestMonthStats(unittest.TestCase):
    def test_aggregates_one_month(self):
        p = _fresh_poller()
        a, b = p.rec("A"), p.rec("B")
        a["daily"]["2026-06-01"] = {"edit": 3600, "color": 1800}
        a["daily"]["2026-06-15"] = {"color": 1800}
        a["daily"]["2026-07-01"] = {"edit": 999}  # different month: excluded
        b["daily"]["2026-06-15"] = {"fusion": 600}
        p.data["idle_daily"] = {"2026-06-02": 300, "2026-07-01": 50}
        p.set_rate_for("A", 10.0)
        p.set_rate_for("B", 20.0)
        st = p.month_stats(2026, 6)
        self.assertEqual(st["total"], 3600 + 1800 + 1800 + 600)
        self.assertEqual(st["pages"]["color"], 3600)
        self.assertEqual(st["projects"]["B"], 600)
        self.assertEqual(st["active_days"], 2)
        self.assertEqual(st["busiest"][0], "2026-06-01")
        self.assertEqual(st["idle"], 300)
        self.assertAlmostEqual(
            st["earned"], (7200 / 3600.0) * 10 + (600 / 3600.0) * 20, places=6)

    def test_respects_page_rate_overrides(self):
        p = _fresh_poller()
        p.rec("A")["daily"]["2026-06-01"] = {"color": 3600}
        p.set_rate_for("A", 10.0)
        p.config["page_rates_on"] = True
        p.config["page_rates"] = {"A": {"color": 30.0}}
        self.assertAlmostEqual(p.month_stats(2026, 6)["earned"], 30.0, places=6)

    def test_empty_month_is_zeroed(self):
        p = _fresh_poller()
        st = p.month_stats(1999, 1)
        self.assertEqual(st["total"], 0)
        self.assertEqual(st["active_days"], 0)
        self.assertIsNone(st["busiest"])


class TestWatchRole(unittest.TestCase):
    """The --watch role's Tk-free parts (config plumbing, locks, helpers)."""

    def setUp(self):
        for f in (rtt.CONFIG_FILE, rtt.WATCH_LOCK, rtt.LOCK_FILE):
            if os.path.exists(f):
                os.remove(f)

    def test_watch_mode_defaults_off(self):
        self.assertEqual(rtt.load_config()["watch_mode"], "off")

    def test_watch_mode_invalid_value_falls_back_to_off(self):
        with open(rtt.CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump({"watch_mode": "banana"}, f)
        self.assertEqual(rtt.load_config()["watch_mode"], "off")

    def test_watch_mode_round_trips(self):
        with open(rtt.CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump({"watch_mode": "auto"}, f)
        self.assertEqual(rtt.load_config()["watch_mode"], "auto")

    def test_watch_run_command_launches_this_file_with_flag(self):
        cmd = rtt.watch_run_command()
        self.assertIn("--watch", cmd)
        self.assertIn("ResolveTimeTracker.py", cmd)

    def test_run_watch_exits_when_mode_off_and_cleans_lock(self):
        rtt.run_watch()  # default config = off -> must return immediately
        self.assertFalse(os.path.exists(rtt.WATCH_LOCK))

    def test_stale_watch_lock_does_not_count_as_running(self):
        with open(rtt.WATCH_LOCK, "w", encoding="utf-8") as f:
            f.write("999999999")  # dead pid
        self.assertFalse(rtt.watcher_is_running())

    def test_resolve_process_check_returns_bool(self):
        self.assertIn(rtt.resolve_process_running(), (True, False))


class TestNamedRuntime(unittest.TestCase):
    """The renamed-interpreter copy (ResolveTimeTracker.exe in Task Manager)."""

    def _source(self):
        src = rtt.find_pythonw()
        if not src:
            self.skipTest("no pythonw.exe on this machine")
        return src

    def test_named_exe_builds_and_runs_with_stdlib(self):
        exe = rtt.ensure_named_exe(self._source())
        self.assertTrue(exe and os.path.exists(exe))
        r = subprocess.run(
            [exe, "-c",
             "import sys, json, ctypes, tkinter; sys.stdout.write(sys.executable)"],
            capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("ResolveTimeTracker.exe", r.stdout)

    def test_named_exe_is_cached_until_source_changes(self):
        src = self._source()
        exe = rtt.ensure_named_exe(src)
        mtime = os.path.getmtime(exe)
        self.assertEqual(rtt.ensure_named_exe(src), exe)
        self.assertEqual(os.path.getmtime(exe), mtime)  # not rebuilt

    def test_missing_python_falls_back_to_none(self):
        self.assertIsNone(rtt.ensure_named_exe(""))
        self.assertIsNone(rtt.ensure_named_exe(r"C:\nope\pythonw.exe"))


class TestSpawnTracker(unittest.TestCase):
    """spawn_tracker(): startup verification and the antivirus-kill fallback.
    Plain pythonw is the AV-quiet default; the renamed runtime exe (which
    behavioral AV can terminate at birth) is opt-in via "named_exe"."""

    def setUp(self):
        self._orig = {n: getattr(rtt, n) for n in
                      ("find_pythonw", "ensure_named_exe", "_spawn_detached",
                       "_tracker_started")}
        self.calls = []
        rtt.release_lock()
        if os.path.exists(rtt.CONFIG_FILE):
            os.remove(rtt.CONFIG_FILE)

    def tearDown(self):
        for n, f in self._orig.items():
            setattr(rtt, n, f)
        rtt.release_lock()
        if os.path.exists(rtt.CONFIG_FILE):
            os.remove(rtt.CONFIG_FILE)

    def _arm(self, started, named_exe=False):
        if named_exe:
            with open(rtt.CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump({"named_exe": True}, f)
        rtt.find_pythonw = lambda: "pythonw.exe"
        rtt.ensure_named_exe = lambda exe: "named.exe"
        rtt._spawn_detached = lambda exe, flag: self.calls.append(exe) or True
        rtt._tracker_started = started

    def test_no_python_reports_nopython(self):
        rtt.find_pythonw = lambda: ""
        self.assertEqual(rtt.spawn_tracker(), "nopython")

    def test_default_config_uses_plain_pythonw_only(self):
        self._arm(lambda timeout: True)
        rtt.ensure_named_exe = lambda exe: self.fail(
            "named exe must not be built unless opted in")
        self.assertEqual(rtt.spawn_tracker(), "ok")
        self.assertEqual(self.calls, ["pythonw.exe"])

    def test_opt_in_named_exe_success_spawns_once(self):
        self._arm(lambda timeout: True, named_exe=True)
        self.assertEqual(rtt.spawn_tracker(), "ok")
        self.assertEqual(self.calls, ["named.exe"])

    def test_killed_named_exe_falls_back_to_plain_pythonw(self):
        # Named exe spawns but never takes the lock -> retried with plain.
        self._arm(lambda timeout: len(self.calls) == 2, named_exe=True)
        self.assertEqual(rtt.spawn_tracker(), "ok")
        self.assertEqual(self.calls, ["named.exe", "pythonw.exe"])

    def test_both_killed_reports_blocked(self):
        self._arm(lambda timeout: False, named_exe=True)
        self.assertEqual(rtt.spawn_tracker(), "blocked")
        self.assertEqual(self.calls, ["named.exe", "pythonw.exe"])

    def test_tracker_python_defaults_to_plain_interpreter(self):
        rtt.find_pythonw = lambda: "pythonw.exe"
        rtt.ensure_named_exe = lambda exe: self.fail(
            "named exe must not be built unless opted in")
        self.assertEqual(rtt.tracker_python(), "pythonw.exe")

    def test_tracker_started_polls_the_lock(self):
        rtt.touch_lock()  # fresh lock with a live pid counts as started
        self.assertTrue(rtt._tracker_started(1.0))
        rtt.release_lock()
        self.assertFalse(rtt._tracker_started(0.6))


class TestDefaultRate(unittest.TestCase):
    """The last gross rate the user set anywhere becomes the default for
    projects that don't have their own rate yet."""

    def test_new_projects_inherit_last_set_rate(self):
        p = _fresh_poller()
        p.set_rate_for("Other", 30.0)
        self.assertEqual(p.rate("BrandNew"), 30.0)
        _tick_seconds(p, 2.0)  # TestProject has no explicit rate
        rs = p.data["projects"]["TestProject"]["rate_seconds"]
        self.assertIn("30", rs)  # billed at the inherited default

    def test_explicit_rate_wins_and_updates_default(self):
        p = _fresh_poller()
        p.set_rate_for("A", 30.0)
        p.set_rate_for("B", 10.0)
        self.assertEqual(p.rate("A"), 30.0)
        self.assertEqual(p.rate("B"), 10.0)
        self.assertEqual(p.rate("C"), 10.0)  # last set rate is the default

    def test_zeroing_a_project_does_not_clear_the_default(self):
        p = _fresh_poller()
        p.set_rate_for("A", 30.0)
        p.set_rate_for("B", 0)
        self.assertEqual(p.rate("B"), 0.0)   # explicit zero respected
        self.assertEqual(p.rate("C"), 30.0)  # default survives

    def test_default_rate_round_trips_config(self):
        p = _fresh_poller()
        p.set_rate_for("A", 25.0)
        self.assertEqual(rtt.load_config()["default_rate"], 25.0)


class TestProjectRename(unittest.TestCase):
    """Rename-following via Resolve's stable project unique id
    (sessions.json {"ids": {unique_id: last_known_name}})."""

    def test_rename_moves_history_rate_and_due(self):
        p = _fresh_poller()
        p.set_rate_for("TestProject", 25.0)
        p.set_due("TestProject", "2030-01-01 12:00")
        _tick_seconds(p, 2.0)
        self.assertEqual(p._reconcile_project_id("uid-1", "TestProject"),
                         "TestProject")
        self.assertEqual(p._reconcile_project_id("uid-1", "Renamed"),
                         "Renamed")
        self.assertNotIn("TestProject", p.data["projects"])
        self.assertAlmostEqual(
            p.data["projects"]["Renamed"]["pages"]["edit"], 2.0, delta=0.5)
        self.assertEqual(p.config["rates"]["Renamed"], 25.0)
        self.assertNotIn("TestProject", p.config["rates"])
        self.assertEqual(p.config["due"]["Renamed"], "2030-01-01 12:00")
        self.assertEqual(p.data["ids"]["uid-1"], "Renamed")

    def test_rename_does_not_reset_the_session(self):
        p = _fresh_poller()
        _tick_seconds(p, 2.0)
        start = p._session_start
        p._reconcile_project_id("u", "TestProject")
        p._reconcile_project_id("u", "Renamed")
        self.assertEqual(p.current_project, "Renamed")
        # Next tick reports the new name: same project, session keeps running.
        p.refresh_from_reader = lambda: ("Renamed", "edit")
        _tick_seconds(p, 1.0)
        self.assertEqual(p._session_start, start)

    def test_rename_onto_existing_row_merges_additively(self):
        p = _fresh_poller()
        a, b = p.rec("A"), p.rec("B")
        a["pages"]["edit"] = 100.0
        a["daily"]["2026-06-01"] = {"edit": 100.0}
        a["rate_seconds"]["10"] = 100.0
        b["pages"]["edit"] = 40.0
        b["daily"]["2026-06-01"] = {"edit": 40.0}
        b["rate_seconds"]["10"] = 40.0
        p.set_rate_for("A", 10.0)
        p.set_rate_for("B", 20.0)
        p._reconcile_project_id("u", "A")
        p._reconcile_project_id("u", "B")
        self.assertNotIn("A", p.data["projects"])
        r = p.data["projects"]["B"]
        self.assertEqual(r["pages"]["edit"], 140.0)
        self.assertEqual(r["daily"]["2026-06-01"]["edit"], 140.0)
        self.assertEqual(r["rate_seconds"]["10"], 140.0)
        self.assertEqual(p.config["rates"]["B"], 20.0)  # existing rate wins

    def test_unknown_or_missing_id_is_a_no_op(self):
        p = _fresh_poller()
        _tick_seconds(p, 2.0)
        self.assertEqual(p._reconcile_project_id(None, "TestProject"),
                         "TestProject")
        self.assertNotIn("ids", p.data)
        p._reconcile_project_id("u", "(no project)")
        self.assertNotIn("ids", p.data)

    def test_ids_map_survives_save_and_load(self):
        p = _fresh_poller()
        _tick_seconds(p, 1.0)
        p._reconcile_project_id("uid-9", "TestProject")
        rtt.save_data(p.data)
        data, _ = rtt.load_data()
        self.assertEqual(data["ids"]["uid-9"], "TestProject")


class TestBusyResolve(unittest.TestCase):
    """Playback/render wedges the reader inside a blocking Resolve call, so
    state.json goes stale. With the Resolve process alive that means BUSY,
    not disconnected — stay green and count it as activity."""

    def setUp(self):
        self._orig = rtt.resolve_process_running

    def tearDown(self):
        rtt.resolve_process_running = self._orig
        if os.path.exists(rtt.STATE_FILE):
            os.remove(rtt.STATE_FILE)

    def _stale_state(self):
        rtt.write_state({"name": "P", "page": "edit", "tc": None, "id": None,
                         "blocked": 0, "ts": time.time() - 60})

    def test_stale_state_with_live_process_stays_connected(self):
        p = rtt.Poller()
        self._stale_state()
        rtt.resolve_process_running = lambda: True
        p._last_resolve_activity = time.time() - 300
        name, page = p.refresh_from_reader()
        self.assertEqual((name, page), ("P", "edit"))
        self.assertTrue(p.connected)
        # busy counts as activity -> idle auto-pause can't trip mid-playback
        self.assertLess(time.time() - p._last_resolve_activity, 2.0)

    def test_stale_state_with_dead_process_disconnects(self):
        p = rtt.Poller()
        self._stale_state()
        rtt.resolve_process_running = lambda: False
        p.refresh_from_reader()
        self.assertFalse(p.connected)


class TestCloseWithResolve(unittest.TestCase):
    """Tracker follows Resolve down: after being connected once, a vanished
    Resolve PROCESS (not a mere API dropout) trips resolve_exited() once the
    grace period passes."""

    def setUp(self):
        self._orig_check = rtt.resolve_process_running

    def tearDown(self):
        rtt.resolve_process_running = self._orig_check

    def _gone(self, p):
        """Simulate: was connected, then the Resolve process disappears."""
        rtt.resolve_process_running = lambda: True
        p.connected = True
        _tick_seconds(p, 1.0)   # marks Resolve as seen
        p.connected = False
        rtt.resolve_process_running = lambda: False
        _tick_seconds(p, 1.0)   # starts the gone-clock

    def test_config_defaults_on_and_round_trips(self):
        self.assertTrue(rtt.load_config()["close_with_resolve"])
        with open(rtt.CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump({"close_with_resolve": False}, f)
        self.assertFalse(rtt.load_config()["close_with_resolve"])
        os.remove(rtt.CONFIG_FILE)

    def test_exits_only_after_grace(self):
        p = _fresh_poller()
        self._gone(p)
        self.assertFalse(p.resolve_exited())  # grace not elapsed yet
        p._resolve_gone_since -= rtt.RESOLVE_CLOSE_GRACE + 1
        self.assertTrue(p.resolve_exited())

    def test_resolve_coming_back_resets_the_clock(self):
        p = _fresh_poller()
        self._gone(p)
        rtt.resolve_process_running = lambda: True  # Resolve restarted in time
        _tick_seconds(p, 1.0)
        self.assertIsNone(p._resolve_gone_since)
        self.assertFalse(p.resolve_exited())

    def test_never_connected_never_exits(self):
        # Tracker started before Resolve: must keep waiting, not exit.
        p = _fresh_poller()
        p.connected = False
        rtt.resolve_process_running = lambda: False
        _tick_seconds(p, 1.0)
        self.assertIsNone(p._resolve_gone_since)

    def test_setting_off_disables_exit(self):
        p = _fresh_poller()
        p.config["close_with_resolve"] = False
        self._gone(p)
        self.assertIsNone(p._resolve_gone_since)
        self.assertFalse(p.resolve_exited())


class TestRateViews(unittest.TestCase):
    def test_fallback_view_is_full_gross(self):
        p = _fresh_poller()
        p.config["rate_mode"] = "advanced"
        p.config["rate_views"] = []  # broken/missing config
        self.assertEqual(p.rate_factor(), 1.0)

    def test_advanced_view_factor(self):
        p = _fresh_poller()
        p.config["rate_mode"] = "advanced"
        p.config["rate_views"] = [{"name": "Net", "pct": 80.0}]
        p.config["rate_view"] = "Net"
        self.assertAlmostEqual(p.rate_factor(), 0.8)

    def test_simple_mode_ignores_views(self):
        p = _fresh_poller()
        p.config["rate_mode"] = "simple"
        p.config["rate_views"] = [{"name": "Net", "pct": 80.0}]
        p.config["rate_view"] = "Net"
        self.assertEqual(p.rate_factor(), 1.0)


class TestDataMigration(unittest.TestCase):
    def test_v2_rate_lifted_into_config(self):
        with open(rtt.SESSIONS_FILE, "w", encoding="utf-8") as f:
            json.dump({"version": 2, "projects": {
                "Old": {"pages": {"edit": 60}, "daily": {}, "rate": 25}}}, f)
        data, migrated = rtt.load_data()
        self.assertEqual(migrated, {"Old": 25.0})
        self.assertNotIn("rate", data["projects"]["Old"])

    def test_corrupt_sessions_file_returns_empty(self):
        with open(rtt.SESSIONS_FILE, "w", encoding="utf-8") as f:
            f.write("{not json")
        data, migrated = rtt.load_data()
        self.assertEqual(data["projects"], {})
        self.assertEqual(migrated, {})

    def test_old_style_rate_views_converted(self):
        # Pre-"pct_of_gross" configs stored pct as a signed adjustment
        # (-20 meant "gross minus 20%"); they load as 100 + pct.
        with open(rtt.CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump({"rate_views": [{"name": "Net", "pct": -20}]}, f)
        cfg = rtt.load_config()
        self.assertEqual(cfg["rate_views"], [{"name": "Net", "pct": 80.0}])
        os.remove(rtt.CONFIG_FILE)


class TestDueCountdown(unittest.TestCase):
    def test_parses_and_reports_overdue(self):
        p = _fresh_poller()
        past = (datetime.datetime.now()
                - datetime.timedelta(days=1)).strftime("%Y-%m-%d %H:%M")
        p.set_due("TestProject", past)
        days, hrs, mins, overdue = p.due_countdown("TestProject")
        self.assertTrue(overdue)
        future = (datetime.datetime.now()
                  + datetime.timedelta(days=2)).strftime("%Y-%m-%dT%H:%M")
        p.set_due("TestProject", future)
        self.assertFalse(p.due_countdown("TestProject")[3])

    def test_garbage_due_is_none(self):
        p = _fresh_poller()
        p.set_due("TestProject", "not a date")
        self.assertIsNone(p.due_countdown("TestProject"))


class TestCsvExport(unittest.TestCase):
    def test_all_time_export_totals_and_injection_guard(self):
        p = _fresh_poller(project="=HYPERLINK(evil)")
        p.set_rate_for("=HYPERLINK(evil)", 60.0)
        _tick_seconds(p, 6.0)  # 6 s at 60/h
        path = os.path.join(_TMP, "out.csv")
        rtt.export_csv(p, path)
        with open(path, encoding="utf-8-sig") as f:
            text = f.read()
        self.assertIn("'=HYPERLINK(evil)", text)   # formula neutralized
        self.assertNotIn("\n=HYPERLINK", text)
        self.assertIn("TOTAL", text)

    def test_ranged_export_uses_daily_data(self):
        p = _fresh_poller()
        r = p.rec("TestProject")
        r["daily"]["2026-01-10"] = {"edit": 3600}
        r["daily"]["2026-02-10"] = {"edit": 7200}
        r["pages"]["edit"] = 10800
        p.set_rate_for("TestProject", 10.0)
        path = os.path.join(_TMP, "ranged.csv")
        rtt.export_csv(p, path,
                       start=datetime.date(2026, 2, 1),
                       end=datetime.date(2026, 2, 28))
        with open(path, encoding="utf-8-sig") as f:
            text = f.read()
        self.assertIn("02:00:00", text)   # only February's 2 h
        self.assertNotIn("03:00:00", text)


class TestScreenClamp(unittest.TestCase):
    def test_far_offscreen_position_comes_back(self):
        x, y = rtt.clamp_to_screen(99999, 99999)
        if sys.platform == "win32":
            self.assertLess(x, 99999)
            self.assertLess(y, 99999)
        else:  # non-Windows: helper is a pass-through
            self.assertEqual((x, y), (99999, 99999))


def tearDownModule():
    shutil.rmtree(_TMP, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
