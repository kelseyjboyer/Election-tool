import io
import tempfile
import unittest
from pathlib import Path

from tracker import detect as d
from tracker.config import ConfigError, RaceConfig, load_config
from tracker.format import PROJECTION
from tracker.model import parse_race
from tracker.notify import ConsoleNotifier
from tracker.policy import decide
from tracker.runner import deliver_pending, process_race
from tracker.state import State

from .helpers import GOVERNOR, SENATE_CONTROL, load_raw, with_votes

ROOT = Path(__file__).resolve().parent.parent
T1, T2, T3 = "2026-11-04T04:30:00.000Z", "2026-11-04T05:00:00.000Z", "2026-11-04T05:30:00.000Z"


class FakeClient:
    """Returns queued responses per race id; repeats the last one when the queue runs out."""

    def __init__(self, responses: dict[int, list[dict]]):
        self.responses = responses

    def race(self, rid):
        q = self.responses[rid]
        return q.pop(0) if len(q) > 1 else q[0]

    def status(self):
        return {"status": "ok"}


def gov_cfg(tmp: Path, **race_kw):
    text = (ROOT / "config.toml").read_text()
    p = tmp / "config.toml"
    p.write_text(text)
    cfg = load_config(p)
    rc = RaceConfig(id=GOVERNOR, label="OR Governor", group="my_ballot", **race_kw)
    cfg.races = [rc]
    cfg.page.enabled = False
    return cfg, rc


class Policy(unittest.TestCase):
    def setUp(self):
        base = load_raw(GOVERNOR)
        self.prev = parse_race(with_votes(base, [1000, 900, 50, 10], 5, T1), GOVERNOR)
        self.cur = parse_race(with_votes(base, [2000, 1800, 60, 10], 9, T2), GOVERNOR)
        self.events = d.detect(self.prev, self.cur)
        self.rc = RaceConfig(id=GOVERNOR, label="OR Governor")

    def test_batch_throttled_inside_window(self):
        dec = decide(self.events, self.cur, self.rc, now=1000, last_batch_alert_at=900,
                     throttle_seconds=600)
        self.assertEqual(dec.alerts, [])
        self.assertEqual([e.kind for e in dec.suppressed], [d.NEW_BATCH])
        self.assertFalse(dec.advance_baseline)

    def test_batch_allowed_after_window(self):
        dec = decide(self.events, self.cur, self.rc, now=2000, last_batch_alert_at=900,
                     throttle_seconds=600)
        self.assertEqual([a.kind for a in dec.alerts], [d.NEW_BATCH])
        self.assertTrue(dec.advance_baseline)

    def test_call_and_lead_change_bypass_throttle(self):
        base = load_raw(GOVERNOR)
        cur = parse_race(with_votes(base, [2000, 2500, 60, 10], 9, T2, winners=(1,)), GOVERNOR)
        ev = d.detect(self.prev, cur)
        dec = decide(ev, cur, self.rc, now=1000, last_batch_alert_at=999, throttle_seconds=600)
        self.assertEqual(sorted(a.kind for a in dec.alerts), [d.CALL, d.LEAD_CHANGE])

    def test_critical_races_get_higher_priority(self):
        crit = RaceConfig(id=GOVERNOR, label="x", critical=True)
        a = decide(self.events, self.cur, self.rc, now=0, last_batch_alert_at=None, throttle_seconds=0)
        b = decide(self.events, self.cur, crit, now=0, last_batch_alert_at=None, throttle_seconds=0)
        self.assertGreater(b.alerts[0].priority, a.alerts[0].priority)

    def test_milestone_folded_into_batch(self):
        base = load_raw(GOVERNOR)
        cur = parse_race(with_votes(base, [9000, 8000, 60, 10], 30, T2), GOVERNOR)
        ev = d.detect(self.prev, cur)
        self.assertIn(d.MILESTONE, [e.kind for e in ev])
        dec = decide(ev, cur, self.rc, now=0, last_batch_alert_at=None, throttle_seconds=0)
        self.assertEqual([a.kind for a in dec.alerts], [d.NEW_BATCH])
        self.assertIn("passed 25%", dec.alerts[0].title)
        # When the batch is throttled, the milestone still goes out on its own.
        dec = decide(ev, cur, self.rc, now=10, last_batch_alert_at=5, throttle_seconds=600)
        self.assertEqual([a.kind for a in dec.alerts], [d.MILESTONE])

    def test_test_data_never_alerts(self):
        self.cur.test_data = True
        dec = decide(self.events, self.cur, self.rc, now=0, last_batch_alert_at=None, throttle_seconds=0)
        self.assertEqual(dec.alerts, [])


class Wording(unittest.TestCase):
    def _alerts(self, prev_raw, cur_raw, rid=GOVERNOR):
        prev, cur = parse_race(prev_raw, rid), parse_race(cur_raw, rid)
        ev = d.detect(prev, cur, majority=51)
        return decide(ev, cur, RaceConfig(id=rid, label="L"), now=0, last_batch_alert_at=None,
                      throttle_seconds=0).alerts

    def test_call_labeled_projection_and_shows_last_updated(self):
        base = load_raw(GOVERNOR)
        alerts = self._alerts(with_votes(base, [5, 4, 0, 0], 50, T1),
                              with_votes(base, [5, 4, 0, 0], 50, T2, winners=(0,)))
        a = alerts[0]
        self.assertIn(PROJECTION, a.title)
        self.assertIn("not an AP call", a.body)
        self.assertNotIn("official winner", a.body.lower())
        self.assertIn("last_updated", a.body)

    def test_every_alert_shows_last_updated_and_oregon_note(self):
        base = load_raw(GOVERNOR)
        alerts = self._alerts(with_votes(base, [1000, 900, 0, 0], 20, T1),
                              with_votes(base, [2000, 2400, 0, 0], 60, T2, winners=(1,)))
        self.assertGreaterEqual(len(alerts), 3)
        for a in alerts:
            self.assertIn("last_updated", a.body)
            self.assertIn("Oregon votes by mail", a.body)

    def test_anomaly_alert_text(self):
        base = load_raw(GOVERNOR)
        alerts = self._alerts(with_votes(base, [1000, 900, 0, 0], 20, T1),
                              with_votes(base, [800, 900, 0, 0], 25, T2))
        self.assertEqual([a.kind for a in alerts], [d.ANOMALY])
        self.assertIn("DATA ANOMALY", alerts[0].title)


class EndToEndState(unittest.TestCase):
    def test_restart_never_resends(self):
        base = load_raw(GOVERNOR)
        seq = [with_votes(base, [0, 0, 0, 0], 0, T1),
               with_votes(base, [1000, 900, 0, 0], 5, T2),
               with_votes(base, [1000, 1200, 0, 0], 9, T3, winners=(1,))]
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            cfg, rc = gov_cfg(tmp)
            db = tmp / "s.db"
            out = io.StringIO()
            client = FakeClient({GOVERNOR: list(seq)})

            state = State(db)
            for _ in range(3):
                process_race(cfg, client, state, rc)
                deliver_pending(cfg, state, ConsoleNotifier(out))
            first = out.getvalue()
            self.assertIn("first results", first)
            self.assertIn(PROJECTION, first)
            state.close()

            # Simulated restart: same db, the API keeps returning the final snapshot.
            out2 = io.StringIO()
            state = State(db)
            for _ in range(2):
                process_race(cfg, client, state, rc)
                deliver_pending(cfg, state, ConsoleNotifier(out2))
            self.assertEqual(out2.getvalue(), "")
            sent = [a for a in state.recent_alerts() if a["status"] == "sent"]
            self.assertEqual(len({a["alert_id"] for a in sent}), len(sent))
            state.close()

    def test_failed_delivery_is_retried_then_marked_sent(self):
        class Flaky:
            calls = 0

            def send(self, alert, click_url=None):
                Flaky.calls += 1
                return Flaky.calls > 1

        base = load_raw(GOVERNOR)
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            cfg, rc = gov_cfg(tmp)
            state = State(tmp / "s.db")
            client = FakeClient({GOVERNOR: [with_votes(base, [0, 0, 0, 0], 0, T1),
                                            with_votes(base, [10, 5, 0, 0], 1, T2)]})
            process_race(cfg, client, state, rc)
            process_race(cfg, client, state, rc)
            notifier = Flaky()
            deliver_pending(cfg, state, notifier)
            self.assertEqual(state.recent_alerts()[0]["status"], "failed")
            deliver_pending(cfg, state, notifier)
            self.assertEqual(state.recent_alerts()[0]["status"], "sent")
            state.close()


class ConfigRules(unittest.TestCase):
    def test_poll_floor_enforced(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "c.toml"
            p.write_text("[settings]\npoll_interval_seconds = 30\n")
            with self.assertRaises(ConfigError):
                load_config(p)

    def test_shipped_config_loads(self):
        cfg = load_config(ROOT / "config.toml")
        self.assertGreater(len(cfg.races), 10)
        self.assertTrue(all(cfg.poll_interval(r) >= 60 for r in cfg.races))
        self.assertEqual(cfg.race(SENATE_CONTROL).majority, 51)


if __name__ == "__main__":
    unittest.main()


class DigestAndBudget(unittest.TestCase):
    def _setup(self, tmp):
        from tracker.policy import Alert
        cfg, _ = gov_cfg(tmp)
        state = State(tmp / "s.db")

        def mk(i, kind, prio, crit=False):
            return Alert(f"a{i}", 1000 + i, kind, prio, f"Race {i}: {kind}",
                         f"Leader line {i}\nMargin line\ncivicAPI last_updated: X", [], "X", crit)
        return cfg, state, mk

    def test_routine_updates_bundled_into_one_push(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg, state, mk = self._setup(Path(tmp))
            alerts = [mk(1, d.NEW_BATCH, 2), mk(2, d.FIRST_RESULTS, 4, True), mk(3, d.MILESTONE, 3),
                      mk(4, d.CALL, 4)]
            for a in alerts:
                state.db.execute("INSERT INTO alerts (alert_id, race_id, kind, priority, title, body, tags,"
                                 " last_updated, critical, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                                 (a.alert_id, a.race_id, a.kind, a.priority, a.title, a.body, "[]",
                                  "X", int(a.critical), __import__("time").time()))
            state.db.commit()
            out = io.StringIO()
            sent = deliver_pending(cfg, state, ConsoleNotifier(out))
            self.assertEqual(sent, 2)  # the call alone + one digest
            text = out.getvalue()
            self.assertIn("Results update: 3 races (1 control-critical)", text)
            self.assertLess(text.index("Race 2"), text.index("Race 1: new_batch"))  # critical first
            statuses = {r["alert_id"]: r["status"] for r in state.recent_alerts()}
            self.assertEqual(statuses["a4"], "sent")
            self.assertEqual(statuses["a1"], "digested")

            # A new routine alert right away waits for the digest window.
            a = mk(5, d.NEW_BATCH, 2)
            state.commit_poll(parse_race(with_votes(load_raw(GOVERNOR), [1, 0, 0, 0], 1, T1), GOVERNOR), [a], False)
            self.assertEqual(deliver_pending(cfg, state, ConsoleNotifier(io.StringIO())), 0)
            state.close()

    def test_budget_reserves_room_for_urgent(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg, state, mk = self._setup(Path(tmp))
            cfg.delivery.daily_limit = 10
            cfg.delivery.reserve_for_urgent = 3
            for _ in range(8):
                state.record_send("single")
            snap = parse_race(with_votes(load_raw(GOVERNOR), [1, 0, 0, 0], 1, T1), GOVERNOR)
            state.commit_poll(snap, [mk(1, d.STATUS_FLAG, 3), mk(2, d.CALL_RETRACTED, 5)], False)
            out = io.StringIO()
            self.assertEqual(deliver_pending(cfg, state, ConsoleNotifier(out)), 1)
            self.assertIn("call_retracted", out.getvalue())
            self.assertNotIn("status_flag", out.getvalue())
            state.close()


class CallsOnlyMode(unittest.TestCase):
    """Decisions-only alerts that quote the chamber seat count."""

    def _cfg(self, tmp):
        cfg, _ = gov_cfg(tmp)
        house = RaceConfig(id=SENATE_CONTROL, label="Senate", group="control", majority=51, total_seats=100)
        race = RaceConfig(id=GOVERNOR, label="Test race", group="senate", critical=True, chamber=SENATE_CONTROL)
        cfg.races = [house, race]
        cfg.settings.alert_kinds = ["call", "call_changed", "call_retracted", "runoff", "chamber_majority"]
        return cfg, race

    def test_call_alert_includes_fresh_seat_count_and_routine_updates_are_silent(self):
        from .helpers import with_seats
        base, tally = load_raw(GOVERNOR), load_raw(SENATE_CONTROL)
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            cfg, rc = self._cfg(tmp)
            state = State(tmp / "s.db")
            client = FakeClient({
                GOVERNOR: [with_votes(base, [100, 90, 0, 0], 50, T1),
                           with_votes(base, [300, 200, 0, 0], 80, T2),   # batch + lead: silent
                           with_votes(base, [300, 200, 0, 0], 80, T3, winners=(0,))],
                SENATE_CONTROL: [with_seats(tally, [54, 44]), with_seats(tally, [55, 44], T3)],
            })
            process_race(cfg, client, state, cfg.race(SENATE_CONTROL))
            out = io.StringIO()
            for _ in range(3):
                process_race(cfg, client, state, rc)
                deliver_pending(cfg, state, ConsoleNotifier(out))
            text = out.getvalue()
            self.assertEqual(text.count("=" * 72), 1, text)  # exactly one push: the call
            self.assertIn(PROJECTION, text)
            self.assertIn("Senate now: Democrats 55, Republicans 44 (51 for majority, 1 uncalled)", text)
            state.close()

    def test_runoff_detected(self):
        base = load_raw(GOVERNOR)
        prev = parse_race(with_votes(base, [480, 450, 70, 0], 100, T1), GOVERNOR)
        raw = with_votes(base, [480, 450, 70, 0], 100, T2)
        raw["candidates"][0]["advanced"] = raw["candidates"][1]["advanced"] = True
        ev = d.detect(prev, parse_race(raw, GOVERNOR))
        self.assertEqual([e.kind for e in ev], [d.RUNOFF])
        self.assertEqual(ev[0].details["advanced"], ["Tina Kotek", "Christine Drazan"])

    def test_calls_config_loads(self):
        cfg = load_config(ROOT / "config.calls.toml")
        tallies = {r.id for r in cfg.races if r.group == "control"}
        self.assertEqual(tallies, {87524, 87528})
        self.assertTrue(all(r.chamber in tallies for r in cfg.races if r.group != "control"))
