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
        for name, path in (("RAW", self.raw), ("STAMP", root / "stamp")):
            p = mock.patch.object(k, name, path)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(k, "_minted", None)
        p.start()
        self.addCleanup(p.stop)
        self.creds = (None, "R")
        p = mock.patch.object(k, "_app_credentials", lambda: self.creds)
        p.start()
        self.addCleanup(p.stop)
        self.minted = []
        self.mint_result = jwt(10_000)

        def mint(refresh):
            self.minted.append(refresh)
            if isinstance(self.mint_result, Exception):
                raise self.mint_result
            return self.mint_result
        p = mock.patch.object(k, "_mint_access_token", mint)
        p.start()
        self.addCleanup(p.stop)

    def test_fresh_app_token_used_as_is(self):
        self.creds = (jwt(10_000), "R")
        self.assertEqual(k._access_token(now=9_000), jwt(10_000))
        self.assertEqual(self.minted, [])

    def test_expired_app_token_mints_with_app_refresh_token(self):
        self.creds = (jwt(1_000), "R")
        self.assertEqual(k._access_token(now=1_100), jwt(10_000))
        self.assertEqual(self.minted, ["R"])

    def test_minted_token_reused_within_process(self):
        self.creds = (jwt(1_000), "R")
        k._access_token(now=1_100)
        k._access_token(now=1_200)
        self.assertEqual(len(self.minted), 1)

    def test_rejected_refresh_is_auth_error(self):
        self.creds = (jwt(1_000), "R")
        self.mint_result = k.AuthenticationError("Kimi 认证失效：续期被拒 (HTTP 401)")
        with self.assertRaises(k.AuthenticationError):
            k._access_token(now=1_100)

    def test_no_refresh_token_is_auth_error(self):
        self.creds = (jwt(1_000), None)
        with self.assertRaises(k.AuthenticationError):
            k._access_token(now=1_100)

    def test_fetch_keeps_last_good_when_mint_fails_transiently(self):
        good = {"five_hour": {"utilization": 10.0, "resets_at": None}}
        self.raw.write_text(json.dumps(good))
        self.creds = (jwt(1), "R")
        self.mint_result = k.TokenStaleError("Kimi 续期失败 (HTTP 0: timeout)")
        with mock.patch.object(k, "_curl", side_effect=AssertionError("no call")):
            self.assertEqual(k.fetch_usage(force=True), good)
        self.assertFalse(k.STAMP.exists())

    def test_fetch_without_last_good_reports_transient(self):
        self.creds = (jwt(1), "R")
        self.mint_result = k.TokenStaleError("Kimi 续期失败 (HTTP 0: timeout)")
        with self.assertRaises(k.TokenStaleError) as cm:
            k.fetch_usage(force=True)
        self.assertNotIn("认证失效", str(cm.exception))


class MintTests(unittest.TestCase):
    def mint(self, status, body):
        with mock.patch.object(k, "_post", return_value=(status, body, "", 0)) as post:
            return k._mint_access_token("R"), post

    def test_ok_returns_access_token_only(self):
        tok, post = self.mint(200, json.dumps({"accessToken": "A", "refreshToken": "R2"}))
        self.assertEqual(tok, "A")
        self.assertEqual(post.call_args[0][1], {"refreshToken": "R"})

    def test_rejected(self):
        for status in (400, 401, 403):
            with self.assertRaises(k.AuthenticationError):
                self.mint(status, "{}")

    def test_network_failure_is_transient(self):
        with self.assertRaises(k.TokenStaleError):
            self.mint(0, "")
        with self.assertRaises(k.TokenStaleError):
            self.mint(502, "bad gateway")


class QuotaTests(unittest.TestCase):
    def test_protobuf_omitted_fields(self):
        self.assertEqual(k._quota_used_pct({"limit": "100", "used": "100"}), 100.0)
        self.assertEqual(k._quota_used_pct({"limit": "100", "remaining": "100"}), 0.0)
        self.assertEqual(k._quota_used_pct({"limit": "100", "used": "8", "remaining": "92"}), 8.0)
        self.assertEqual(k._quota_used_pct({"limit": "100", "remaining": "90"}), 10.0)

    def test_exhausted_five_hour_window_normalizes(self):
        data = {"usages": [{"scope": "FEATURE_CODING", "limits": [{
            "window": {"duration": 300, "timeUnit": "TIME_UNIT_MINUTE"},
            "detail": {"limit": "100", "used": "100", "resetTime": "x"}}]}],
            "totalQuota": {"limit": "100", "used": "8", "remaining": "92"}}
        out = k.normalize(data, {})
        self.assertEqual(out["five_hour"]["utilization"], 100.0)
        self.assertEqual(out["cycle"]["utilization"], 8.0)


class RollOverTests(unittest.TestCase):
    def test_past_five_hour_window_rolls_to_zero(self):
        data = {"five_hour": {"utilization": 100.0, "resets_at": "2026-10-26T09:01:01.813253Z"},
                "cycle": {"utilization": 8.0, "resets_at": "2026-11-18T07:01:01Z",
                          "starts_at": "2026-10-19T07:01:01Z"}}
        now = k._parse_iso("2026-10-26T09:07:00Z")
        out = k._roll_over(data, now)
        self.assertEqual(out["five_hour"]["utilization"], 0.0)
        self.assertEqual(k._parse_iso(out["five_hour"]["resets_at"]),
                         k._parse_iso("2026-10-26T14:01:01.813253Z"))
        self.assertEqual(out["cycle"]["utilization"], 8.0)

    def test_future_window_untouched(self):
        data = {"five_hour": {"utilization": 40.0, "resets_at": "2026-10-26T09:01:01Z"}}
        out = k._roll_over(data, k._parse_iso("2026-10-26T08:00:00Z"))
        self.assertEqual(out["five_hour"]["utilization"], 40.0)

    def test_multiple_windows_skipped(self):
        data = {"five_hour": {"utilization": 100.0, "resets_at": "2026-10-26T09:00:00Z"}}
        out = k._roll_over(data, k._parse_iso("2026-10-26T20:00:00Z"))
        self.assertEqual(out["five_hour"]["resets_at"], "2026-10-27T00:00:00Z")


if __name__ == "__main__":
    unittest.main()
