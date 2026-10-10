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
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patches = [
            mock.patch.object(fetch_usage, "DEBUG_LOG", Path(tmp.name) / "debug.log"),
            mock.patch.object(fetch_usage, "_system_proxy", return_value=None),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    @mock.patch.object(fetch_usage.subprocess, "run")
    def test_region_403_is_not_auth_failure(self, run):
        run.return_value = mock.Mock(
            stdout='{"type":"error","error":{"type":"permission_error",'
                   '"details":{"error_code":"region_unavailable"}}}\n'
                   f"{fetch_usage.STATUS_MARKER}403",
            stderr="", returncode=0,
        )

        with self.assertRaises(fetch_usage.RegionUnavailableError):
            fetch_usage.curl_json("https://example.test/usage", "secret")

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

    @mock.patch.object(fetch_usage.time, "sleep")
    @mock.patch.object(fetch_usage.subprocess, "run")
    def test_curl_json_retries_cloudflare_challenge_403(self, run, sleep):
        run.side_effect = [
            mock.Mock(
                stdout=f"<!DOCTYPE html><title>Just a moment...</title>\n"
                       f"{fetch_usage.STATUS_MARKER}403",
                stderr="", returncode=0,
            ),
            mock.Mock(
                stdout=f'{{"five_hour": {{"utilization": 69}}}}\n{fetch_usage.STATUS_MARKER}200',
                stderr="", returncode=0,
            ),
        ]

        result = fetch_usage.curl_json("https://example.test/usage", "secret")

        self.assertEqual(69, result["five_hour"]["utilization"])
        self.assertEqual(2, run.call_count)

    @mock.patch.object(fetch_usage.subprocess, "run")
    def test_curl_json_json_403_is_auth_failure(self, run):
        run.return_value = mock.Mock(
            stdout=f'{{"type":"error","error":{{"type":"permission_error"}}}}\n'
                   f"{fetch_usage.STATUS_MARKER}403",
            stderr="", returncode=0,
        )

        with self.assertRaisesRegex(fetch_usage.AuthenticationError, "HTTP 403"):
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

    def test_claude_image_rows_are_separated_for_swiftbar(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            claude = root / "last_fetch.json"
            codex = root / "codex_last_fetch.json"
            claude.write_text(
                json.dumps(
                    {
                        "five_hour": {"utilization": 10},
                        "seven_day": {"utilization": 20},
                    }
                )
            )
            codex.write_text(
                json.dumps(
                    {
                        "rate_limits": {
                            "secondary": {
                                "used_percent": 30,
                                "window_minutes": 10080,
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
                mock.patch.object(render, "b64", return_value="menubar-data"),
                mock.patch.object(
                    render.imggen,
                    "combined_menubar_icon",
                    return_value=root / "menubar.png",
                ),
                mock.patch.object(
                    render.imggen,
                    "panel",
                    side_effect=lambda *args, **kwargs: root / args[3],
                ),
                mock.patch.object(
                    render,
                    "panel_image_line",
                    side_effect=lambda path: "PANEL:" + path.name,
                ),
                redirect_stdout(output),
            ):
                render.main()

            self.assertIn(
                "PANEL:claude_five_panel.png\n---\nPANEL:claude_seven_panel.png",
                output.getvalue(),
            )

    def test_codex_five_hour_window_gets_its_own_column_and_panel(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            claude = root / "last_fetch.json"
            codex = root / "codex_last_fetch.json"
            claude.write_text(json.dumps({"five_hour": None}))
            codex.write_text(
                json.dumps(
                    {
                        "rate_limits": {
                            "primary": {
                                "used_percent": 100,
                                "window_minutes": 300,
                                "resets_at": 4102444800,
                            },
                            "secondary": {
                                "used_percent": 37,
                                "window_minutes": 10080,
                                "resets_at": 4102444800,
                            },
                        }
                    }
                )
            )

            output = io.StringIO()
            with (
                mock.patch.object(render, "RAW", claude),
                mock.patch.object(render, "CACHE", claude),
                mock.patch.object(render, "CODEX_RAW", codex),
                mock.patch.object(render, "b64", return_value="menubar-data"),
                mock.patch.object(
                    render.imggen,
                    "combined_menubar_icon",
                    return_value=root / "menubar.png",
                ) as menubar,
                mock.patch.object(
                    render.imggen,
                    "panel",
                    side_effect=lambda *args, **kwargs: root / args[3],
                ),
                mock.patch.object(
                    render,
                    "panel_image_line",
                    side_effect=lambda path: "PANEL:" + path.name,
                ),
                redirect_stdout(output),
            ):
                render.main()

            self.assertIn(
                "PANEL:codex_five_panel.png\n---\nPANEL:codex_panel.png",
                output.getvalue(),
            )
            self.assertEqual(100.0, menubar.call_args.kwargs["codex_five"][0])
            self.assertEqual(37.0, menubar.call_args.args[2][0])

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
            self.assertIn("Claude Code · 无数据", text)
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

            self.assertIn("Claude Code · 无数据", output.getvalue())
            self.assertIn("width=10.0 height=5.0", output.getvalue())
            self.assertEqual(panel.call_args.args[2], "Codex")

    def test_panel_image_line_uses_retina_point_size(self):
        with tempfile.TemporaryDirectory() as tmp:
            panel = make_panel(Path(tmp) / "panel.png")
            with mock.patch.object(render, "b64", return_value="image-data"):
                line = render.panel_image_line(panel)
            self.assertEqual("| image=image-data width=10.0 height=5.0", line)

    def test_claude_windows_render_as_two_short_retina_images(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            win = {
                "used": 25,
                "time": 50,
                "rem": "2h30m",
                "ucol": imggen.OK,
                "verdict": "↓ 落后 · 有余量",
                "vcol": imggen.OK,
            }
            with mock.patch.object(imggen, "HERE", root):
                five = imggen.panel(
                    win, None, "Claude Code", "five.png", single_window="five"
                )
                seven = imggen.panel(
                    None, win, None, "seven.png", single_window="seven"
                )
                codex = imggen.panel(
                    None, win, "Codex", "codex.png", only_weekly=True
                )

            with (
                Image.open(five) as five_image,
                Image.open(seven) as seven_image,
                Image.open(codex) as codex_image,
            ):
                self.assertLessEqual(five_image.height, codex_image.height)
                self.assertLess(seven_image.height, codex_image.height)
                self.assertEqual(five_image.info.get("dpi"), codex_image.info.get("dpi"))
                self.assertEqual(seven_image.info.get("dpi"), codex_image.info.get("dpi"))

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

                with_codex_five = imggen.combined_menubar_icon(
                    (10, 20), (30, 40), (16, 27), show_claude=True, codex_five=(100, 50)
                )
                with Image.open(with_codex_five) as image:
                    self.assertEqual((296, 44), image.size)


if __name__ == "__main__":
    unittest.main()
