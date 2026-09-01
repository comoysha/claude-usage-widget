import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import fetch_usage
import imggen
import render
import window_log
from PIL import Image


def make_panel(path):
    Image.new("RGBA", (20, 10), (0, 0, 0, 0)).save(path, dpi=(144, 144))
    return path


class ClaudePayloadTests(unittest.TestCase):
    def test_null_window_is_not_a_usable_cache_payload(self):
        self.assertFalse(fetch_usage._is_usable_usage({"five_hour": None}))

    def test_window_object_is_a_usable_cache_payload(self):
        self.assertTrue(
            fetch_usage._is_usable_usage(
                {"five_hour": {"utilization": 0, "resets_at": None}}
            )
        )

    def test_malformed_usage_is_not_a_usable_cache_payload(self):
        self.assertFalse(
            fetch_usage._is_usable_usage(
                {"five_hour": {"utilization": "not-a-number"}}
            )
        )

    @mock.patch.object(fetch_usage.time, "sleep")
    @mock.patch.object(fetch_usage.subprocess, "run")
    def test_curl_json_retries_transient_non_json_response(self, run, sleep):
        run.side_effect = [
            mock.Mock(stdout="<html>302 Found</html>", stderr="", returncode=0),
            mock.Mock(stdout='{"five_hour": {"utilization": 25}}', stderr="", returncode=0),
        ]

        result = fetch_usage.curl_json("https://example.test/usage", "secret")

        self.assertEqual(25, result["five_hour"]["utilization"])
        self.assertEqual(2, run.call_count)
        sleep.assert_called_once_with(fetch_usage.RETRY_DELAYS_S[0])

    @mock.patch.object(fetch_usage.time, "sleep")
    @mock.patch.object(fetch_usage.subprocess, "run")
    def test_curl_json_reports_failure_after_bounded_retries(self, run, sleep):
        run.return_value = mock.Mock(
            stdout="<html>302 Found</html>", stderr="", returncode=0
        )

        with self.assertRaisesRegex(fetch_usage.TransientFetchError, "连续 3 次非 JSON 响应"):
            fetch_usage.curl_json("https://example.test/usage", "secret")

        self.assertEqual(fetch_usage.REQUEST_ATTEMPTS, run.call_count)
        self.assertEqual(
            [mock.call(3), mock.call(10)], sleep.call_args_list
        )

    @mock.patch.object(fetch_usage.subprocess, "run")
    def test_curl_json_does_not_retry_explicit_auth_failure(self, run):
        run.return_value = mock.Mock(
            stdout=f'not authorized\n{fetch_usage.STATUS_MARKER}401',
            stderr="",
            returncode=0,
        )

        with self.assertRaisesRegex(fetch_usage.AuthenticationError, "HTTP 401"):
            fetch_usage.curl_json("https://example.test/usage", "secret")

        run.assert_called_once()

    def test_transient_failure_reuses_last_good_payload(self):
        cached = {"five_hour": {"utilization": 21}}
        with tempfile.TemporaryDirectory() as tmp:
            with (
                mock.patch.object(fetch_usage, "STAMP", Path(tmp) / ".last_attempt"),
                mock.patch.object(fetch_usage, "recently_tried", return_value=False),
                mock.patch.object(fetch_usage, "get_session_key", return_value="secret"),
                mock.patch.object(
                    fetch_usage, "get_org", side_effect=fetch_usage.TransientFetchError("302")
                ),
                mock.patch.object(fetch_usage, "_last_good", return_value=cached),
            ):
                self.assertEqual(cached, fetch_usage.fetch_usage())


class RenderCompatibilityTests(unittest.TestCase):
    def test_claude_utilization_is_already_percentage_points(self):
        for percentage in (0, 0.5, 1.0, 2, 100):
            with self.subTest(percentage=percentage):
                payload = {
                    "five_hour": {
                        "utilization": percentage,
                        "resets_at": "2026-09-02T02:49:59+00:00",
                    }
                }

                used, _ = render.find_window(
                    payload, ("five_hour", "fiveHour")
                )
                logged, _ = window_log._extract(
                    payload, ("five_hour", "fiveHour")
                )

                self.assertEqual(float(percentage), used)
                self.assertEqual(float(percentage), logged)

    def test_claude_failure_keeps_codex_panel(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            claude = root / "last_fetch.json"
            codex = root / "codex_last_fetch.json"
            claude.write_text(
                json.dumps(
                    {"five_hour": None, "member_dashboard_available": False}
                )
            )
            codex.write_text(
                json.dumps(
                    {
                        "rate_limits": {
                            "secondary": {
                                "used_percent": 12,
                                "window_minutes": 10080,
                                "resets_at": 4102444800,
                            }
                        }
                    }
                )
            )

            output = io.StringIO()
            with (
                mock.patch.object(render, "RAW", claude),
                mock.patch.object(render, "CACHE", claude),
                mock.patch.object(render, "CODEX_RAW", codex),
                mock.patch.object(render, "b64", return_value="image-data"),
                mock.patch.object(
                    render.imggen, "combined_menubar_icon", return_value=root / "menubar.png"
                ) as menubar,
                mock.patch.object(
                    render.imggen,
                    "panel",
                    side_effect=lambda *args, **kwargs: make_panel(root / "codex-panel.png"),
                ) as panel,
                redirect_stdout(output),
            ):
                render.main()

            text = output.getvalue()
            self.assertIn("Claude Code · 重新登录", text)
            self.assertNotIn("待确认字段", text)
            self.assertNotIn("顶层键", text)
            self.assertIn("立即刷新", text)
            self.assertIn("width=10.0 height=5.0", text)
            panel.assert_called_once()
            self.assertEqual(panel.call_args.args[2], "Codex")
            self.assertFalse(menubar.call_args.kwargs["show_claude"])

    def test_malformed_claude_usage_keeps_codex_panel(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            claude = root / "last_fetch.json"
            codex = root / "codex_last_fetch.json"
            claude.write_text(
                json.dumps({"five_hour": {"utilization": "not-a-number"}})
            )
            codex.write_text(
                json.dumps(
                    {
                        "rate_limits": {
                            "primary": {
                                "used_percent": 12,
                                "window_minutes": 10080,
                                "resets_at": 4102444800,
                            }
                        }
                    }
                )
            )

            output = io.StringIO()
            with (
                mock.patch.object(render, "RAW", claude),
                mock.patch.object(render, "CACHE", claude),
                mock.patch.object(render, "CODEX_RAW", codex),
                mock.patch.object(render, "b64", return_value="image-data"),
                mock.patch.object(
                    render.imggen, "combined_menubar_icon", return_value=root / "menubar.png"
                ),
                mock.patch.object(
                    render.imggen,
                    "panel",
                    side_effect=lambda *args, **kwargs: make_panel(root / "codex-panel.png"),
                ) as panel,
                redirect_stdout(output),
            ):
                render.main()

            self.assertIn("Claude Code · 重新登录", output.getvalue())
            self.assertIn("width=10.0 height=5.0", output.getvalue())
            self.assertEqual(panel.call_args.args[2], "Codex")

    def test_panel_image_line_uses_retina_point_size(self):
        with tempfile.TemporaryDirectory() as tmp:
            panel = make_panel(Path(tmp) / "panel.png")
            with mock.patch.object(render, "b64", return_value="image-data"):
                line = render.panel_image_line(panel)
            self.assertEqual("| image=image-data width=10.0 height=5.0", line)

    def test_menubar_icon_hides_claude_columns_and_separators(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(imggen, "HERE", Path(tmp)):
                hidden = imggen.combined_menubar_icon(
                    (None, None), (None, None), (16, 27), show_claude=False
                )
                with Image.open(hidden) as image:
                    self.assertEqual((74, 44), image.size)

                full = imggen.combined_menubar_icon(
                    (10, 20), (30, 40), (16, 27), show_claude=True
                )
                with Image.open(full) as image:
                    self.assertEqual((222, 44), image.size)


if __name__ == "__main__":
    unittest.main()
