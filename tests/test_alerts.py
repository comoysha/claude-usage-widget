import tempfile
import unittest
from pathlib import Path

import feishu_notify
import usage_alerts


def item(used, reset=1_000_000, key="Claude|5 小时"):
    return {"key": key, "name": "Claude", "window": "5 小时", "used": used, "reset": reset}


class FeishuSendTests(unittest.TestCase):
    def send(self, resp, url="https://x"):
        return feishu_notify.post_feishu_message({}, webhook_url=url, poster=lambda u, p: resp)

    def test_code_zero(self):
        self.assertTrue(self.send({"code": 0}))
        self.assertTrue(self.send({"StatusCode": 0}))

    def test_failures(self):
        self.assertFalse(self.send({"code": 19024}))
        self.assertFalse(self.send("oops"))

    def test_exception_and_no_webhook(self):
        def boom(u, p):
            raise RuntimeError(u)
        self.assertFalse(feishu_notify.post_feishu_message({}, webhook_url="https://x", poster=boom))
        called = []
        old = feishu_notify.ENV_FILE
        feishu_notify.ENV_FILE = Path("/nonexistent/.env")
        try:
            import os
            os.environ.pop(feishu_notify.WEBHOOK_ENV, None)
            self.assertFalse(feishu_notify.post_feishu_message({}, poster=lambda u, p: called.append(1)))
        finally:
            feishu_notify.ENV_FILE = old
        self.assertEqual(called, [])


class AlertTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name) / "s.json"
        self.sent = []

    def run_check(self, items, ok=True):
        def poster(u, p):
            self.sent.append(p)
            return {"code": 0 if ok else 11232}
        import os
        os.environ[feishu_notify.WEBHOOK_ENV] = "https://x"
        self.addCleanup(os.environ.pop, feishu_notify.WEBHOOK_ENV, None)
        return usage_alerts.check(items, poster=poster, state_path=self.state)

    def test_below_threshold_silent(self):
        self.assertFalse(self.run_check([item(99.0)]))
        self.assertEqual(self.sent, [])

    def test_fires_once_per_cycle(self):
        self.assertTrue(self.run_check([item(100.0)]))
        self.assertFalse(self.run_check([item(100.0, reset=1_000_000.4)]))
        self.assertEqual(len(self.sent), 1)

    def test_recovery_sent_once_then_rearms(self):
        self.run_check([item(100.0)])
        self.assertTrue(self.run_check([item(3.0)]))
        self.assertEqual(len(self.sent), 2)
        self.assertEqual(self.sent[1]["card"]["header"]["template"], "green")
        self.assertFalse(self.run_check([item(4.0)]))
        self.assertEqual(len(self.sent), 2)

    def test_failed_recovery_retries(self):
        self.run_check([item(100.0)])
        self.assertFalse(self.run_check([item(3.0)], ok=False))
        self.assertTrue(self.run_check([item(3.0)]))

    def test_rearms_after_reset(self):
        self.run_check([item(100.0)])
        self.run_check([item(3.0)])
        self.assertTrue(self.run_check([item(100.0, reset=1_020_000)]))
        self.assertEqual(len(self.sent), 3)

    def test_failed_send_does_not_advance_state(self):
        self.assertFalse(self.run_check([item(100.0)], ok=False))
        self.assertTrue(self.run_check([item(100.0)]))

    def test_multiple_windows_one_card(self):
        self.run_check([item(100.0), item(100.0, key="Kimi|30", reset=2_000_000)])
        self.assertEqual(len(self.sent), 1)


if __name__ == "__main__":
    unittest.main()
