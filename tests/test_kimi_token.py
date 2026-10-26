import base64
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import fetch_kimi_usage as k


def jwt(exp):
    body = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).decode().rstrip("=")
    return "h.%s.s" % body


class TokenFreshnessTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.raw = root / "kimi_last_fetch.json"
        for name, path in (("TOKEN_STATE", root / "state.json"),
                           ("RAW", self.raw), ("STAMP", root / "stamp")):
            p = mock.patch.object(k, name, path)
            p.start()
            self.addCleanup(p.stop)
        self.token = None
        p = mock.patch.object(k, "_access_token", lambda: self.token)
        p.start()
        self.addCleanup(p.stop)
        self.running = True
        p = mock.patch.object(k, "_app_running", lambda: self.running)
        p.start()
        self.addCleanup(p.stop)

    def check(self, now):
        return k._check_token_fresh(now=now)

    def test_valid_token_passes(self):
        self.token = jwt(10_000)
        self.check(9_000)

    def test_expired_token_waits_not_auth_error(self):
        self.token = jwt(1_000)
        with self.assertRaises(k.TokenStaleError):
            self.check(1_100)
        # Still within the app's normal rotation gap a few ticks later.
        with self.assertRaises(k.TokenStaleError):
            self.check(1_100 + 300)

    def test_app_missing_rotations_while_awake_is_auth_error(self):
        self.token = jwt(1_000)
        t = 1_100
        with self.assertRaises(k.TokenStaleError):
            self.check(t)
        while t < 1_100 + k.STALE_ALARM_S - 300:
            t += 300
            with self.assertRaises(k.TokenStaleError):
                self.check(t)
        with self.assertRaises(k.AuthenticationError):
            self.check(t + 300)

    def test_sleep_gap_does_not_count(self):
        self.token = jwt(1_000)
        with self.assertRaises(k.TokenStaleError):
            self.check(1_100)
        # Mac slept 8 hours; first tick after wake must not alarm.
        with self.assertRaises(k.TokenStaleError):
            self.check(1_100 + 8 * 3600)

    def test_app_closed_never_alarms(self):
        self.running = False
        self.token = jwt(1_000)
        t = 1_100
        for _ in range(20):
            with self.assertRaises(k.TokenStaleError):
                self.check(t)
            t += 300

    def test_new_token_resets_watch(self):
        self.token = jwt(1_000)
        with self.assertRaises(k.TokenStaleError):
            self.check(1_100)
        self.token = jwt(2_000)
        self.check(1_200)
        self.assertFalse(k.TOKEN_STATE.exists())

    def test_fetch_keeps_last_good_when_token_stale(self):
        good = {"five_hour": {"utilization": 10.0, "resets_at": None}}
        self.raw.write_text(json.dumps(good))
        self.token = jwt(1)  # long expired
        with mock.patch.object(k, "_curl", side_effect=AssertionError("no call")):
            self.assertEqual(k.fetch_usage(force=True), good)
        self.assertFalse(k.STAMP.exists())

    def test_fetch_without_last_good_reports_waiting(self):
        self.token = jwt(1)
        with self.assertRaises(k.TokenStaleError) as cm:
            k.fetch_usage(force=True)
        self.assertNotIn("认证失效", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
