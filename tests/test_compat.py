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


class RenderCompatibilityTests(unittest.TestCase):
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
