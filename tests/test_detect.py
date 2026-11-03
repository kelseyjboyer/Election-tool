import copy
import unittest

from tracker import detect as d
from tracker.model import parse_race

from .helpers import GOVERNOR, MEASURE, SENATE_CONTROL, load_raw, with_seats, with_votes

T1, T2, T3 = "2026-11-04T04:30:00.000Z", "2026-11-04T05:00:00.000Z", "2026-11-04T05:30:00.000Z"


def snap(raw, rid=GOVERNOR):
    return parse_race(raw, rid)


def kinds(events):
    return sorted(e.kind for e in events)


class RealFixtureShape(unittest.TestCase):
    """Guards against civicAPI renaming fields we rely on."""

    def test_v2_fields_present(self):
        raw = load_raw(GOVERNOR)
        for f in ("election_name", "percent_reporting", "last_updated", "is_disputed",
                  "candidates", "province", "test_data"):
            self.assertIn(f, raw)
        for f in ("candidate_id", "name", "party", "votes", "percent", "winner"):
            self.assertIn(f, raw["candidates"][0])

    def test_parse_real_fixture(self):
        s = snap(load_raw(GOVERNOR))
        self.assertEqual(s.name, "Oregon Governor")
        self.assertTrue(s.is_oregon)
        self.assertFalse(s.is_chamber)
        self.assertIsNone(s.is_delayed)  # not present in v2
        self.assertTrue(snap(load_raw(MEASURE), MEASURE).is_measure)
        self.assertTrue(snap(load_raw(SENATE_CONTROL), SENATE_CONTROL).is_chamber)

    def test_pre_election_fixture_has_no_anomaly(self):
        s = snap(load_raw(GOVERNOR))
        self.assertEqual(d.find_anomalies(s, s), [])


class Batches(unittest.TestCase):
    def setUp(self):
        self.base = load_raw(GOVERNOR)
        self.zero = snap(with_votes(self.base, [0, 0, 0, 0], 0))

    def test_first_observation_is_silent(self):
        self.assertEqual(d.detect(None, self.zero), [])

    def test_first_results(self):
        cur = snap(with_votes(self.base, [1000, 900, 50, 10], 5, T1))
        ev = d.detect(self.zero, cur)
        self.assertEqual(kinds(ev), [d.FIRST_RESULTS])
        self.assertEqual(ev[0].details["leader"]["name"], "Tina Kotek")

    def test_new_batch_reports_change_since_baseline(self):
        baseline = snap(with_votes(self.base, [1000, 900, 50, 10], 5, T1))
        prev = snap(with_votes(self.base, [1500, 1300, 60, 10], 8, T2))
        cur = snap(with_votes(self.base, [3000, 2500, 100, 20], 15, T3))
        ev = d.detect(prev, cur, baseline=baseline)
        self.assertEqual(kinds(ev), [d.NEW_BATCH])
        self.assertEqual(ev[0].details["votes_added"], 5620 - 1960)
        self.assertEqual(ev[0].details["reporting_added"], 10)

    def test_no_change_no_event(self):
        a = snap(with_votes(self.base, [1000, 900, 50, 10], 5, T1))
        self.assertEqual(d.detect(a, copy.deepcopy(a)), [])


class LeadChanges(unittest.TestCase):
    def test_lead_change(self):
        base = load_raw(GOVERNOR)
        prev = snap(with_votes(base, [1000, 900, 50, 10], 10, T1))
        cur = snap(with_votes(base, [1500, 1700, 60, 10], 20, T2))
        ev = d.detect(prev, cur)
        self.assertIn(d.LEAD_CHANGE, kinds(ev))
        lc = [e for e in ev if e.kind == d.LEAD_CHANGE][0]
        self.assertEqual(lc.details["new_leader"]["name"], "Christine Drazan")
        self.assertEqual(lc.details["old_leader"]["name"], "Tina Kotek")

    def test_measure_yes_no_flip(self):
        base = load_raw(MEASURE)
        prev = snap(with_votes(base, [600, 400], 10, T1), MEASURE)
        cur = snap(with_votes(base, [800, 900], 20, T2), MEASURE)
        lc = [e for e in d.detect(prev, cur) if e.kind == d.LEAD_CHANGE]
        self.assertEqual(lc[0].details["new_leader"]["name"], "No")

    def test_tie_is_not_a_lead_change(self):
        base = load_raw(MEASURE)
        prev = snap(with_votes(base, [600, 400], 10, T1), MEASURE)
        cur = snap(with_votes(base, [700, 700], 20, T2), MEASURE)
        self.assertNotIn(d.LEAD_CHANGE, kinds(d.detect(prev, cur)))


class Calls(unittest.TestCase):
    def setUp(self):
        self.base = load_raw(GOVERNOR)
        self.uncalled = snap(with_votes(self.base, [50000, 40000, 2000, 100], 60, T1))

    def test_call(self):
        cur = snap(with_votes(self.base, [50000, 40000, 2000, 100], 60, T2, winners=(0,)))
        ev = d.detect(self.uncalled, cur)
        self.assertEqual(kinds(ev), [d.CALL])
        self.assertEqual(ev[0].details["winners"], ["Tina Kotek"])

    def test_retraction(self):
        called = snap(with_votes(self.base, [50000, 40000, 2000, 100], 60, T1, winners=(0,)))
        cur = snap(with_votes(self.base, [52000, 45000, 2000, 100], 65, T2))
        ev = d.detect(called, cur)
        self.assertIn(d.CALL_RETRACTED, kinds(ev))
        self.assertNotIn(d.CALL, kinds(ev))

    def test_call_moved_to_other_candidate(self):
        called = snap(with_votes(self.base, [50000, 40000, 2000, 100], 60, T1, winners=(0,)))
        cur = snap(with_votes(self.base, [50000, 60000, 2000, 100], 80, T2, winners=(1,)))
        ev = d.detect(called, cur)
        self.assertIn(d.CALL_CHANGED, kinds(ev))
        self.assertIn(d.LEAD_CHANGE, kinds(ev))

    def test_existing_call_surfaced_on_first_observation(self):
        cur = snap(with_votes(self.base, [50000, 40000, 2000, 100], 60, T2, winners=(0,)))
        self.assertEqual(kinds(d.detect(None, cur)), [d.CALL])


class Anomalies(unittest.TestCase):
    def setUp(self):
        self.base = load_raw(GOVERNOR)
        self.prev = snap(with_votes(self.base, [5000, 4000, 200, 10], 30, T1))

    def test_votes_went_down_replaces_normal_update(self):
        cur = snap(with_votes(self.base, [4800, 4500, 200, 10], 40, T2))
        ev = d.detect(self.prev, cur)
        self.assertEqual(kinds(ev), [d.ANOMALY])
        self.assertTrue(any("went down" in i for i in ev[0].details["issues"]))

    def test_percents_do_not_add_up(self):
        raw = with_votes(self.base, [6000, 4500, 200, 10], 40, T2)
        raw["candidates"][0]["percent"] = 80.0
        issues = d.find_anomalies(self.prev, snap(raw))
        self.assertTrue(any("add up" in i for i in issues))

    def test_missing_candidate(self):
        raw = with_votes(self.base, [6000, 4500, 200, 10], 40, T2)
        raw["candidates"] = raw["candidates"][:2]
        for c in raw["candidates"]:
            c["percent"] = round(100 * c["votes"] / 10500, 2)
        issues = d.find_anomalies(self.prev, snap(raw))
        self.assertTrue(any("missing" in i for i in issues))

    def test_reporting_went_down(self):
        cur = snap(with_votes(self.base, [6000, 4500, 200, 10], 20, T2))
        self.assertTrue(any("percent_reporting" in i for i in d.find_anomalies(self.prev, cur)))

    def test_call_still_reported_during_anomaly(self):
        cur = snap(with_votes(self.base, [4000, 3000, 200, 10], 40, T2, winners=(0,)))
        ev = d.detect(self.prev, cur)
        self.assertEqual(kinds(ev), [d.ANOMALY, d.CALL])
        self.assertTrue([e for e in ev if e.kind == d.CALL][0].details["during_anomaly"])


class FlagsAndMilestones(unittest.TestCase):
    def test_disputed_flag(self):
        base = load_raw(GOVERNOR)
        prev = snap(with_votes(base, [5000, 4000, 200, 10], 30, T1))
        raw = with_votes(base, [5000, 4000, 200, 10], 30, T2)
        raw["is_disputed"] = True
        ev = d.detect(prev, snap(raw))
        self.assertEqual(kinds(ev), [d.STATUS_FLAG])
        self.assertEqual(ev[0].details["flag"], "is_disputed")

    def test_milestones_crossed_in_one_jump(self):
        base = load_raw(GOVERNOR)
        prev = snap(with_votes(base, [5000, 4000, 200, 10], 20, T1))
        cur = snap(with_votes(base, [9000, 8000, 300, 10], 55, T2))
        ms = [e for e in d.detect(prev, cur) if e.kind == d.MILESTONE]
        self.assertEqual(ms[0].details["milestones"], [25, 50])
        self.assertEqual(ms[0].key, "50")


class Chamber(unittest.TestCase):
    def test_seats_and_majority(self):
        base = load_raw(SENATE_CONTROL)
        prev = snap(with_seats(base, [49, 47]), SENATE_CONTROL)
        cur = snap(with_seats(base, [51, 47], T2), SENATE_CONTROL)
        ev = d.detect(prev, cur, majority=51)
        self.assertEqual(kinds(ev), [d.CHAMBER_MAJORITY, d.CHAMBER_SEATS])
        maj = [e for e in ev if e.kind == d.CHAMBER_MAJORITY][0]
        self.assertEqual(maj.details["party"], "Democrats")

    def test_real_chamber_fixture_has_holdover_seats(self):
        s = snap(load_raw(SENATE_CONTROL), SENATE_CONTROL)
        seats = {c.name: c.seats for c in s.candidates}
        self.assertGreater(seats["Democrats"] + seats["Republicans"], 0)


if __name__ == "__main__":
    unittest.main()
