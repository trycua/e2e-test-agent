import asyncio
import contextlib
import importlib.util
import json
import io
import tempfile
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest import mock
import urllib.error


REPO_ROOT = Path(__file__).resolve().parent.parent
AGENT_DIR = REPO_ROOT / "agent"
AGENT_FILE = AGENT_DIR / "e2e_test_agent.py"
HELPER_FILE = AGENT_FILE
EDIT_VIDEO_FILE = AGENT_DIR / "edit_video.py"


def load_agent_module():
    spec = importlib.util.spec_from_file_location("e2e_test_agent", AGENT_FILE)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def load_edit_video_module():
    spec = importlib.util.spec_from_file_location("edit_video", EDIT_VIDEO_FILE)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def load_helper_module():
    spec = importlib.util.spec_from_file_location("e2e_test_agent", HELPER_FILE)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class E2ETestAgentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.agent = load_agent_module()

    def test_custom_text_is_appended_to_system_prompt(self):
        with tempfile.TemporaryDirectory() as directory:
            context = Path(directory) / 'custom-system.md'
            context.write_text('Three nspawn nodes and local Keycloak.')
            with mock.patch.dict('os.environ', {'E2E_SYSTEM_PROMPT': str(context)}):
                prompt = self.agent.agent_system_prompt()
        self.assertEqual(prompt['preset'], 'claude_code')
        self.assertIn('Three nspawn nodes and local Keycloak.', prompt['append'])

    def test_default_system_prompt_is_unchanged(self):
        with mock.patch.dict('os.environ', {}, clear=True):
            self.assertEqual(self.agent.agent_system_prompt(),
                             {'type': 'preset', 'preset': 'claude_code'})

    def test_system_prompt_rejects_oversized_file(self):
        with tempfile.TemporaryDirectory() as directory:
            context = Path(directory) / 'large.md'
            context.write_bytes(b'x' * (128 * 1024 + 1))
            with mock.patch.dict('os.environ', {'E2E_SYSTEM_PROMPT': str(context)}):
                with self.assertRaises(ValueError):
                    self.agent.agent_system_prompt()

    def test_renders_all_prompt_tokens(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            prompt = Path(directory) / "prompt.md"
            prompt.write_text("repo={{REPOSITORY}} status={{REPO_STATUS}}")
            rendered = self.agent.render_prompt(
                prompt,
                {"REPOSITORY": "example/project", "REPO_STATUS": "READY"},
            )
        self.assertEqual(rendered, "repo=example/project status=READY")

    def test_rejects_unknown_prompt_tokens(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            prompt = Path(directory) / "prompt.md"
            prompt.write_text("{{UNKNOWN}}")
            with self.assertRaisesRegex(ValueError, "unknown token UNKNOWN"):
                self.agent.render_prompt(prompt, {})

    def test_planning_hook_blocks_paths_outside_working_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            hook = self.agent.planning_permission_hook(root)
            denied = asyncio.run(
                hook(
                    {
                        "tool_name": "Read",
                        "tool_input": {"file_path": "/etc/passwd"},
                    },
                    "tool-id",
                    None,
                )
            )
        output = denied["hookSpecificOutput"]
        self.assertEqual(output["permissionDecision"], "deny")

    def test_planning_hook_allows_plan_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            hook = self.agent.planning_permission_hook(root)
            allowed = asyncio.run(
                hook(
                    {
                        "tool_name": "Write",
                        "tool_input": {"file_path": str(root / "e2e-plan.md")},
                    },
                    "tool-id",
                    None,
                )
            )
        self.assertEqual(allowed, {})

    def test_video_command_hook_rejects_arbitrary_shell(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertTrue(
                self.agent.video_command_is_allowed(
                    root, "ffprobe e2e-video.mp4"
                )
            )
            self.assertTrue(
                self.agent.video_command_is_allowed(
                    root, "python3 edit_video.py --help"
                )
            )
            self.assertFalse(self.agent.video_command_is_allowed(root, "env"))
            self.assertFalse(
                self.agent.video_command_is_allowed(root, "cat /etc/passwd")
            )
            self.assertFalse(
                self.agent.video_command_is_allowed(
                    root, "ffprobe e2e-video.mp4; env"
                )
            )

    def test_validates_agent_result_schema(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = Path(directory) / "e2e-result.json"
            result.write_text('{"status":"pass","summary":"Journey passed"}')
            self.assertTrue(self.agent.valid_result(result))
            result.write_text('{"status":"unknown","summary":"No verdict"}')
            self.assertFalse(self.agent.valid_result(result))


class CuaSandboxHelperTests(unittest.TestCase):
    def setUp(self) -> None:
        self.helper = load_helper_module()

    def test_guest_server_uses_base_sandbox_service_by_default(self) -> None:
        args = SimpleNamespace(pool="pool", sandbox="sandbox", server_service="")
        self.assertEqual(
            self.helper.guest_server_path(args, "start_recording"),
            "/api/svc/pool/sandbox/start_recording",
        )

    def test_guest_server_supports_an_explicit_service_suffix(self) -> None:
        args = SimpleNamespace(pool="pool", sandbox="sandbox", server_service="server")
        self.assertEqual(
            self.helper.guest_server_path(args, "start_recording"),
            "/api/svc/pool/sandbox-server/start_recording",
        )

    def test_shell_uses_computer_server_service(self) -> None:
        args = SimpleNamespace(pool="pool", sandbox="sandbox")
        self.assertEqual(
            self.helper.computer_server_path(args),
            "/api/svc/pool/sandbox-server/cmd",
        )

    def test_guest_execute_parses_computer_server_sse(self) -> None:
        args = SimpleNamespace(pool="pool", sandbox="sandbox")
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = (
            b'data: {"success":true,"stdout":"ok\\n","stderr":"","return_code":0}\n'
        )
        with mock.patch.object(self.helper, "api_request", return_value=response) as request:
            text, is_error = self.helper.guest_execute(args, "pwd", 30)

        self.assertFalse(is_error)
        self.assertEqual(text, "ok\n\n[exit_code=0]")
        path = request.call_args.args[1]
        body = request.call_args.args[2]
        self.assertEqual(path, "/api/svc/pool/sandbox-server/cmd")
        self.assertEqual(
            json.loads(body),
            {"command": "run_command", "params": {"command": "pwd", "timeout": 30}},
        )

    def test_upload_streams_file_through_computer_server(self) -> None:
        args = SimpleNamespace(
            pool="pool",
            sandbox="sandbox",
            source="/tmp/source.bin",
            destination="/tmp/destination.bin",
        )
        with (
            mock.patch.object(self.helper.os.path, "getsize", return_value=3),
            mock.patch("builtins.open", mock.mock_open(read_data=b"abc")),
            mock.patch.object(
                self.helper,
                "computer_server_command",
                return_value={"success": True},
            ) as command,
        ):
            result = self.helper.cmd_upload(args)

        self.assertEqual(result, 0)
        command.assert_called_once_with(
            args,
            "write_bytes",
            {"path": "/tmp/destination.bin", "content_b64": "YWJj", "append": False},
            300,
        )

    def test_screenshot_bytes_decodes_computer_server_image(self) -> None:
        self.assertEqual(
            self.helper.screenshot_bytes({"success": True, "image_data": "iVBORw=="}),
            b"\x89PNG",
        )
        self.assertEqual(
            self.helper.screenshot_bytes(
                {"success": True, "images": [{"data_base64": "iVBORw=="}]}
            ),
            b"\x89PNG",
        )

    def test_shell_mcp_lists_and_executes_the_guest_shell_tool(self) -> None:
        args = SimpleNamespace(pool="pool", sandbox="sandbox", server_service="")
        status, listing = self.helper.shell_mcp_response(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, args
        )
        self.assertEqual(status, 200)
        self.assertEqual(listing["result"]["tools"][0]["name"], "shell_execute")

        with mock.patch.object(
            self.helper, "guest_execute", return_value=("ok\n[exit_code=0]", False)
        ) as execute:
            status, result = self.helper.shell_mcp_response(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {
                        "name": "shell_execute",
                        "arguments": {"command": "pwd", "timeout": 30},
                    },
                },
                args,
            )

        self.assertEqual(status, 200)
        self.assertFalse(result["result"]["isError"])
        self.assertIn("ok", result["result"]["content"][0]["text"])
        execute.assert_called_once_with(args, "export CUA_E2E_HEADED=1\npwd", 30)

    def test_exec_prefers_the_guest_server_shell(self) -> None:
        args = SimpleNamespace(
            pool="pool",
            sandbox="sandbox",
            service="mcp",
            wait_ready=0,
            timeout=30,
            command="pwd",
        )
        with (
            mock.patch.object(
                self.helper, "guest_execute", return_value=("/tmp\n[exit_code=0]", False)
            ) as execute,
            contextlib.redirect_stdout(io.StringIO()) as stdout,
        ):
            result = self.helper.cmd_exec(args)

        self.assertEqual(result, 0)
        self.assertIn("/tmp", stdout.getvalue())
        execute.assert_called_once_with(args, "pwd", 30)

    def test_recording_fallback_installs_ffmpeg_through_cua_driver(self) -> None:
        args = SimpleNamespace(pool="pool", sandbox="sandbox", service="mcp")
        session = mock.MagicMock()
        session.tools = ["install_ffmpeg"]
        session.call.return_value = ("installed", False)
        with mock.patch.object(self.helper, "McpSession", return_value=session):
            self.helper.install_guest_ffmpeg(args)

        session.open.assert_called_once_with(wait_ready=30)
        session.call.assert_called_once_with("install_ffmpeg", {"confirm": True}, timeout=300)

    def test_recording_fallback_is_best_effort_without_computer_server(self) -> None:
        args = SimpleNamespace(
            pool="pool", sandbox="sandbox", service="mcp", server_service=""
        )
        with (
            mock.patch.object(
                self.helper,
                "api_request",
                side_effect=urllib.error.URLError("recording endpoint unavailable"),
            ),
            mock.patch.object(self.helper, "install_guest_ffmpeg"),
            mock.patch.object(
                self.helper,
                "guest_execute",
                side_effect=RuntimeError("computer-server unavailable"),
            ),
            contextlib.redirect_stdout(io.StringIO()) as stdout,
        ):
            result = self.helper.cmd_start_recording(args)

        self.assertEqual(result, 0)
        self.assertEqual(stdout.getvalue().strip(), "none")


if __name__ == "__main__":
    unittest.main()


class EditVideoHelperTests(unittest.TestCase):
    def setUp(self) -> None:
        self.editor = load_edit_video_module()

    @staticmethod
    def meta(duration: float, has_audio: bool = False) -> dict:
        return {
            "duration": duration,
            "width": 1280,
            "height": 720,
            "fps_num": 25,
            "fps_den": 1,
            "has_audio": has_audio,
        }

    def test_zero_fps_streams_fall_back_to_a_sane_profile_rate(self) -> None:
        probe = {
            "format": {"duration": "10.0"},
            "streams": [
                {
                    "codec_type": "video",
                    "width": 640,
                    "height": 480,
                    "r_frame_rate": "0/0",
                }
            ],
        }
        proc = SimpleNamespace(returncode=0, stdout=json.dumps(probe), stderr="")
        with mock.patch.object(self.editor, "run", return_value=proc):
            meta = self.editor.ffprobe_meta("in.mp4")

        self.assertEqual((meta["fps_num"], meta["fps_den"]), (25, 1))
        self.assertEqual((meta["width"], meta["height"]), (640, 480))

    def test_parses_freeze_intervals_and_closes_a_freeze_open_at_eof(self) -> None:
        stderr = "\n".join(
            [
                "[freezedetect @ 0x1] lavfi.freezedetect.freeze_start: 4.0",
                "[freezedetect @ 0x1] lavfi.freezedetect.freeze_duration: 6.0",
                "[freezedetect @ 0x1] lavfi.freezedetect.freeze_end: 10.0",
                "[freezedetect @ 0x1] lavfi.freezedetect.freeze_start: 50.0",
            ]
        )
        self.assertEqual(
            self.editor.parse_freeze_intervals(stderr, 60.0),
            [(4.0, 10.0), (50.0, 60.0)],
        )

    def test_contiguous_freezes_keep_their_boundaries_out_of_the_cuts(self) -> None:
        # Regression for run 33498484891: the e2e recorder captures at 0.5 fps,
        # so each screen change is a single frame and freezedetect reports
        # back-to-back freezes with zero gap around it. Merging those touching
        # intervals collapsed a real 20-minute journey into one "frozen"
        # interval and the edit cut the entire recording to nothing.
        starts_ends = [
            (0, 346), (346, 348), (348, 796), (796, 840), (840, 906),
            (906, 908), (908, 948), (948, 990), (990, 992), (992, 1198),
        ]
        stderr = "\n".join(
            line
            for start, end in starts_ends
            for line in (
                f"[freezedetect @ 0x1] lavfi.freezedetect.freeze_start: {start}",
                f"[freezedetect @ 0x1] lavfi.freezedetect.freeze_end: {end}",
            )
        )
        frozen = self.editor.parse_freeze_intervals(stderr, 1200.0)
        self.assertEqual(len(frozen), 10)

        # One frame at 0.5 fps is 2s of pad; every boundary (a screen change)
        # must survive inside a kept segment, and the edit must keep far more
        # than the boundary frames of an all-frozen recording would leave.
        cuts = self.editor.compute_cuts(frozen, 1200.0, pad=2.0, min_freeze=5.0)
        segments = self.editor.keep_segments(cuts, 1200.0)
        total_kept = sum(end - start for start, end in segments)
        self.assertGreaterEqual(len(segments), 7)
        self.assertGreater(total_kept, 20.0)
        self.assertLess(total_kept, 120.0)
        for boundary in (346.0, 796.0, 840.0, 906.0, 948.0, 990.0):
            self.assertTrue(
                any(start <= boundary <= end for start, end in segments),
                f"screen change at {boundary}s was cut",
            )

    def test_merge_intervals_only_joins_touching_ranges_when_asked(self) -> None:
        touching = [(0.0, 5.0), (5.0, 9.0), (9.5, 12.0)]
        self.assertEqual(
            self.editor.merge_intervals(touching),
            [(0.0, 9.0), (9.5, 12.0)],
        )
        self.assertEqual(
            self.editor.merge_intervals(touching, merge_touching=False),
            touching,
        )
        self.assertEqual(
            self.editor.merge_intervals([(0.0, 6.0), (5.0, 9.0)], merge_touching=False),
            [(0.0, 9.0)],
        )

    def test_parses_silence_intervals_and_clamps_to_the_video(self) -> None:
        stderr = "\n".join(
            [
                "[silencedetect @ 0x1] silence_start: -0.01",
                "[silencedetect @ 0x1] silence_end: 5.5 | silence_duration: 5.51",
                "[silencedetect @ 0x1] silence_start: 30",
            ]
        )
        self.assertEqual(
            self.editor.parse_silence_intervals(stderr, 40.0),
            [(0.0, 5.5), (30.0, 40.0)],
        )

    def test_dead_air_is_the_intersection_of_frozen_and_silent(self) -> None:
        frozen = [(0.0, 10.0), (20.0, 30.0)]
        silent = [(5.0, 25.0), (28.0, 40.0)]
        self.assertEqual(
            self.editor.intersect_intervals(frozen, silent),
            [(5.0, 10.0), (20.0, 25.0), (28.0, 30.0)],
        )

    def test_cuts_pad_context_and_ignore_short_freezes(self) -> None:
        dead = [(10.0, 40.0), (50.0, 52.0), (90.0, 100.0)]
        cuts = self.editor.compute_cuts(dead, 100.0, pad=1.0, min_freeze=5.0)
        self.assertEqual(cuts, [(11.0, 39.0), (91.0, 99.0)])
        self.assertEqual(
            self.editor.keep_segments(cuts, 100.0),
            [(0.0, 11.0), (39.0, 91.0), (99.0, 100.0)],
        )

    def test_editing_requires_meaningful_savings(self) -> None:
        self.assertTrue(self.editor.worth_editing(60.0, 300.0, 5.0, 0.08))
        self.assertFalse(self.editor.worth_editing(4.0, 300.0, 5.0, 0.08))
        self.assertFalse(self.editor.worth_editing(6.0, 300.0, 5.0, 0.08))

    def test_clock_renders_mlt_timestamps(self) -> None:
        self.assertEqual(self.editor.clock(0), "00:00:00.000")
        self.assertEqual(self.editor.clock(3661.5), "01:01:01.500")

    def test_mlt_project_is_a_shotcut_timeline_of_kept_segments(self) -> None:
        xml = self.editor.mlt_project_xml(
            "e2e-video.mp4", [(0.0, 11.0), (39.0, 91.0)], self.meta(100.0)
        )
        self.assertIn('title="Shotcut e2e dead-air edit"', xml)
        self.assertIn('<property name="shotcut">1</property>', xml)
        self.assertIn('<property name="resource">e2e-video.mp4</property>', xml)
        self.assertIn(
            '<entry producer="chain0" in="00:00:00.000" out="00:00:11.000"/>', xml
        )
        self.assertIn(
            '<entry producer="chain0" in="00:00:39.000" out="00:01:31.000"/>', xml
        )
        self.assertIn('width="1280" height="720"', xml)
        self.assertIn('frame_rate_num="25" frame_rate_den="1"', xml)

    def test_ffmpeg_fallback_selects_the_same_segments(self) -> None:
        self.assertEqual(
            self.editor.select_expr([(0.0, 11.0), (39.0, 91.0)]),
            "between(t,0.000,11.000)+between(t,39.000,91.000)",
        )

    def test_validate_mode_checks_an_edited_video_against_the_original(self) -> None:
        with (
            mock.patch.object(
                self.editor, "ffprobe_meta", return_value=self.meta(100.0)
            ),
            mock.patch.object(self.editor, "output_ok", return_value=True) as valid,
        ):
            self.assertEqual(
                self.editor.main(
                    ["--input", "in.mp4", "--validate-edited", "out.mp4"]
                ),
                0,
            )
        valid.assert_called_once_with("out.mp4", 100.0)

        with (
            mock.patch.object(
                self.editor, "ffprobe_meta", return_value=self.meta(100.0)
            ),
            mock.patch.object(self.editor, "output_ok", return_value=False),
        ):
            self.assertEqual(
                self.editor.main(
                    ["--input", "in.mp4", "--validate-edited", "out.mp4"]
                ),
                1,
            )

    def test_main_skips_the_edit_when_dead_air_is_minor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = f"{tmp}/edited.mp4"
            project = f"{tmp}/edit.mlt"
            summary = f"{tmp}/summary.txt"
            with (
                mock.patch.object(
                    self.editor, "ffprobe_meta", return_value=self.meta(300.0)
                ),
                mock.patch.object(
                    self.editor, "detect_freezes", return_value=[(10.0, 16.0)]
                ),
                mock.patch.object(self.editor, "render_with_melt") as melt,
                mock.patch.object(self.editor, "render_with_ffmpeg") as ffmpeg,
            ):
                result = self.editor.main(
                    [
                        "--input", "e2e-video.mp4",
                        "--output", output,
                        "--project", project,
                        "--summary", summary,
                    ]
                )

            self.assertEqual(result, 0)
            melt.assert_not_called()
            ffmpeg.assert_not_called()
            self.assertFalse(Path(output).exists())
            self.assertFalse(Path(project).exists())
            self.assertIn("Dead-air edit skipped", Path(summary).read_text())

    def test_main_writes_the_shotcut_project_and_renders_the_cut(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = f"{tmp}/edited.mp4"
            project = f"{tmp}/edit.mlt"
            summary = f"{tmp}/summary.txt"
            with (
                mock.patch.object(
                    self.editor, "ffprobe_meta", return_value=self.meta(100.0)
                ),
                mock.patch.object(
                    self.editor,
                    "detect_freezes",
                    return_value=[(10.0, 40.0), (60.0, 90.0)],
                ),
                mock.patch.object(
                    self.editor, "render_with_melt", return_value=True
                ) as melt,
                mock.patch.object(self.editor, "render_with_ffmpeg") as ffmpeg,
            ):
                result = self.editor.main(
                    [
                        "--input", "e2e-video.mp4",
                        "--output", output,
                        "--project", project,
                        "--summary", summary,
                    ]
                )

            self.assertEqual(result, 0)
            melt.assert_called_once()
            ffmpeg.assert_not_called()
            project_xml = Path(project).read_text()
            self.assertIn("<mlt ", project_xml)
            self.assertIn('<property name="shotcut">1</property>', project_xml)
            self.assertIn("Dead air trimmed", Path(summary).read_text())

    def test_main_intersects_freezes_with_silence_when_audio_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = f"{tmp}/edited.mp4"
            project = f"{tmp}/edit.mlt"
            with (
                mock.patch.object(
                    self.editor,
                    "ffprobe_meta",
                    return_value=self.meta(100.0, has_audio=True),
                ),
                mock.patch.object(
                    self.editor, "detect_freezes", return_value=[(10.0, 60.0)]
                ),
                mock.patch.object(
                    self.editor, "detect_silences", return_value=[(30.0, 100.0)]
                ) as silences,
                mock.patch.object(
                    self.editor, "render_with_melt", return_value=True
                ) as melt,
            ):
                result = self.editor.main(
                    [
                        "--input", "e2e-video.mp4",
                        "--output", output,
                        "--project", project,
                    ]
                )

            self.assertEqual(result, 0)
            silences.assert_called_once()
            melt.assert_called_once()
            # Dead air is frozen AND silent: (30,60) -> cut (31,59) after padding.
            project_xml = Path(project).read_text()
            self.assertIn('in="00:00:00.000" out="00:00:31.000"', project_xml)
            self.assertIn('in="00:00:59.000" out="00:01:40.000"', project_xml)

    def test_main_degrades_to_freeze_only_when_silencedetect_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = f"{tmp}/edited.mp4"
            project = f"{tmp}/edit.mlt"
            with (
                mock.patch.object(
                    self.editor,
                    "ffprobe_meta",
                    return_value=self.meta(100.0, has_audio=True),
                ),
                mock.patch.object(
                    self.editor, "detect_freezes", return_value=[(10.0, 40.0)]
                ),
                mock.patch.object(
                    self.editor,
                    "detect_silences",
                    side_effect=RuntimeError("silencedetect failed"),
                ),
                mock.patch.object(
                    self.editor, "render_with_melt", return_value=True
                ) as melt,
            ):
                result = self.editor.main(
                    [
                        "--input", "e2e-video.mp4",
                        "--output", output,
                        "--project", project,
                    ]
                )

            self.assertEqual(result, 0)
            melt.assert_called_once()
            # The freeze-only cut (11,39) survives the audio-pass failure.
            project_xml = Path(project).read_text()
            self.assertIn('in="00:00:00.000" out="00:00:11.000"', project_xml)
            self.assertIn('in="00:00:39.000" out="00:01:40.000"', project_xml)

    def test_main_falls_back_to_ffmpeg_when_melt_cannot_render(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = f"{tmp}/edited.mp4"
            project = f"{tmp}/edit.mlt"
            with (
                mock.patch.object(
                    self.editor, "ffprobe_meta", return_value=self.meta(100.0)
                ),
                mock.patch.object(
                    self.editor, "detect_freezes", return_value=[(10.0, 40.0)]
                ),
                mock.patch.object(
                    self.editor, "render_with_melt", return_value=False
                ),
                mock.patch.object(
                    self.editor, "render_with_ffmpeg", return_value=True
                ) as ffmpeg,
            ):
                result = self.editor.main(
                    [
                        "--input", "e2e-video.mp4",
                        "--output", output,
                        "--project", project,
                    ]
                )

            self.assertEqual(result, 0)
            ffmpeg.assert_called_once()
            segments = ffmpeg.call_args.args[1]
            self.assertEqual(segments, [(0.0, 11.0), (39.0, 100.0)])


if __name__ == "__main__":
    unittest.main()
