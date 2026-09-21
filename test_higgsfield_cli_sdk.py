# Copyright (c) 2026 Richard Knuchel
# SPDX-License-Identifier: BSD-2-Clause

import importlib.util
import contextlib
import io
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


SCRIPT = Path(__file__).with_name("HiggsfieldAPI-CLI-sdk.py")


def load_module():
    spec = importlib.util.spec_from_file_location("higgsfield_sdk_cli_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class Completed:
    pass


class FakeController:
    def __init__(self, request_id):
        self.request_id = request_id
        self.status_url = f"https://api.example/requests/{request_id}/status"
        self.cancel_url = f"https://api.example/requests/{request_id}/cancel"
        self.calls = 0

    def status(self):
        self.calls += 1
        return Completed()

    def get(self):
        return {"status": "completed", "request_id": self.request_id,
                "images": [{"url": "https://cdn.example/result.jpg"}]}

    def cancel(self):
        return None


class FakeSyncClient:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self._client = mock.Mock()

    def submit(self, application, arguments):
        self.application = application
        self.arguments = arguments
        return FakeController("request-1")

    def get_request_controller(self, request_id):
        return FakeController(request_id)

    def upload_file(self, path):
        return "https://cdn.example/uploaded.jpg"


class HiggsfieldSdkCliTests(unittest.TestCase):
    def setUp(self):
        self.fake_sdk = types.ModuleType("higgsfield_client")
        self.fake_sdk.SyncClient = FakeSyncClient
        self.modules = mock.patch.dict(sys.modules, {"higgsfield_client": self.fake_sdk})
        self.modules.start()
        self.cli = load_module()

    def tearDown(self):
        self.modules.stop()

    def client(self):
        return self.cli.SDKClient(self.cli.Credentials("id", "secret"))

    def test_sdk_is_used_for_submission_and_status(self):
        client = self.client()
        payload, _ = client.json_request("POST", "/xai/grok-imagine-image-2.0", {"prompt": "cat"})
        self.assertEqual(payload["request_id"], "request-1")
        final, _ = client.json_request("GET", payload["status_url"])
        self.assertEqual(final["status"], "completed")
        self.assertEqual(final["images"][0]["url"], "https://cdn.example/result.jpg")

    def test_sdk_upload_and_cancel_are_used(self):
        client = self.client()
        self.assertEqual(client.upload_file(Path("input.jpg")),
                         "https://cdn.example/uploaded.jpg")
        client.json_request("POST", "/xai/grok-imagine-image-2.0", {"prompt": "cat"})
        response = client.request("POST", "/requests/request-1/cancel")
        self.assertEqual(response.status, 200)

    def test_cli_uses_only_the_sdk_transport(self):
        self.assertFalse(hasattr(self.cli, "HttpClient"))

    def test_empty_environment_mapping_does_not_fall_back_to_process_environment(self):
        with self.assertRaisesRegex(self.cli.AppError, "Missing required Higgsfield credential"):
            self.cli.load_credentials({}, Path("definitely-missing.env"))

    def test_credentials_default_to_dotenv_next_to_the_script(self):
        with mock.patch.object(self.cli, "parse_env_file", return_value={
            "HF_API_KEY_ID": "id", "HF_API_KEY_SECRET": "secret",
        }) as parse_env:
            credentials = self.cli.load_credentials({})
        self.assertEqual(credentials, self.cli.Credentials("id", "secret"))
        parse_env.assert_called_once_with(self.cli.DEFAULT_DOTENV_PATH)

    def test_env_file_option_is_available_before_intermediate_and_leaf_commands(self):
        parser = self.cli.make_parser()
        for argv in (
            ["--env-file", "custom.env", "image", "grok-image-2", "--prompt", "cat"],
            ["image", "--env-file", "custom.env", "grok-image-2", "--prompt", "cat"],
            ["image", "grok-image-2", "--env-file", "custom.env", "--prompt", "cat"],
        ):
            with self.subTest(argv=argv):
                self.assertEqual(parser.parse_args(argv).env_file, "custom.env")

    def test_prompt_and_params_inputs_keep_external_paths_but_enforce_size_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            prompt_path = Path(directory) / "prompt.txt"
            prompt_path.write_text("external prompt", encoding="utf-8")
            prompt_args = SimpleNamespace(
                prompt=None, prompt_file=str(prompt_path), max_input_file_size=1024,
            )
            self.assertEqual(self.cli.read_prompt(prompt_args), "external prompt")

            params_path = Path(directory) / "params.json"
            params_path.write_text('{"seed": 42}', encoding="utf-8")
            self.assertEqual(self.cli.parse_json_params(str(params_path), 1024), {"seed": 42})

            prompt_path.write_text("x" * 11, encoding="utf-8")
            prompt_args.max_input_file_size = 10
            with self.assertRaisesRegex(self.cli.AppError, "size limit"):
                self.cli.read_prompt(prompt_args)

    def test_base_url_requires_trusted_https_and_warns_for_custom_targets(self):
        self.assertEqual(self.cli.resolve_base_url({}), self.cli.BASE_URL)
        self.assertEqual(
            self.cli.resolve_base_url({"HF_API_BASE_URL": "https://api.higgsfield.ai/"}),
            self.cli.BASE_URL,
        )
        warning = io.StringIO()
        with contextlib.redirect_stderr(warning):
            custom = self.cli.resolve_base_url({"HF_API_BASE_URL": "https://staging.example/v1"})
        self.assertEqual(custom, "https://staging.example/v1")
        self.assertIn("send your API key", warning.getvalue())

        invalid_values = [
            "http://staging.example",
            "staging.example",
            "https://staging.example?redirect=1",
            "https://id:secret@staging.example",
        ]
        for value in invalid_values:
            with self.subTest(value=value):
                with self.assertRaisesRegex(self.cli.AppError, "HF_API_BASE_URL"):
                    self.cli.resolve_base_url({"HF_API_BASE_URL": value})

    def test_sdk_client_also_rejects_insecure_direct_base_url(self):
        with self.assertRaisesRegex(self.cli.AppError, "https://"):
            self.cli.SDKClient(self.cli.Credentials("id", "secret"), base_url="http://localhost:8000")

    def test_http_timeout_options_are_forwarded_and_downloads_have_separate_timeout(self):
        parser = self.cli.make_parser()
        args = parser.parse_args([
            "image", "grok-image-2", "--prompt", "cat",
            "--http-timeout", "17", "--download-timeout", "241",
        ])
        self.cli.normalise_args(args)
        with mock.patch.object(self.cli, "load_credentials",
                               return_value=self.cli.Credentials("id", "secret")):
            client = self.cli.make_client(args)
        self.assertEqual(client.timeout, 17.0)
        self.assertEqual(client.download_timeout, 241.0)
        self.assertEqual(client.sdk.kwargs["timeout"], 17.0)

        response = mock.Mock(status=200, headers={}, read=mock.Mock(side_effect=[b"video", b""]))
        response.__enter__ = mock.Mock(return_value=response)
        response.__exit__ = mock.Mock(return_value=None)
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "video.mp4"
            with mock.patch.object(self.cli, "urlopen", return_value=response) as opened:
                client.download_file("https://cdn.example/video.mp4", target)
            self.assertEqual(target.read_bytes(), b"video")
        self.assertEqual(opened.call_args.kwargs["timeout"], 241.0)

    def test_download_size_limit_rejects_large_content_without_leaving_partial_file(self):
        client = self.client()
        client.max_download_size = 3
        response = mock.Mock(status=200, headers={}, read=mock.Mock(side_effect=[b"video", b""]))
        response.__enter__ = mock.Mock(return_value=response)
        response.__exit__ = mock.Mock(return_value=None)
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "video.mp4"
            with mock.patch.object(self.cli, "urlopen", return_value=response):
                with self.assertRaisesRegex(self.cli.AppError, "size limit"):
                    client.download_file("https://cdn.example/video.mp4", target)
            self.assertFalse(target.exists())

    def test_missing_private_transport_has_actionable_configuration_error(self):
        client = self.client()
        client.sdk._client = None
        with self.assertRaisesRegex(self.cli.AppError, "requirements-sdk.txt") as raised:
            client.json_request("GET", "/marketing-studio/image/presets")
        self.assertEqual(raised.exception.kind, "configuration")

    def test_missing_private_estimate_transport_remains_estimate_unavailable(self):
        client = self.client()
        client.sdk._client = None
        with self.assertRaises(self.cli.EstimateUnavailable):
            client.json_request("POST", "/estimate/xai/grok-imagine-image-2.0", {})

    def test_unsupported_sdk_series_fails_with_install_guidance(self):
        with mock.patch.object(self.cli, "package_version", return_value="0.3.0"):
            with self.assertRaisesRegex(self.cli.AppError, "higgsfield-client>=0.2,<0.3"):
                self.client()

    def test_optional_empty_prompt_is_treated_as_absent(self):
        args = SimpleNamespace(prompt="   ", prompt_file=None)
        self.assertIsNone(self.cli.read_prompt(args, required=False))
        with self.assertRaises(self.cli.AppError):
            self.cli.read_prompt(args, required=True)

    def test_repeated_preset_cursor_stops_pagination(self):
        args = self.cli.make_parser().parse_args(["presets", "--all"])
        self.cli.normalise_args(args)

        class RepeatingPresetClient:
            def __init__(self):
                self.calls = 0

            def json_request(self, method, path, **kwargs):
                self.calls += 1
                return {"items": [{"id": str(self.calls)}], "total": 2, "cursor": "same"}, None

        client = RepeatingPresetClient()
        with mock.patch.object(self.cli, "make_client", return_value=client):
            with self.assertRaisesRegex(self.cli.ApiError, "repeated"):
                self.cli.run_presets(args)
        self.assertEqual(client.calls, 2)

    def test_preset_all_reports_the_aggregated_item_count(self):
        args = self.cli.make_parser().parse_args(["presets", "--all"])
        self.cli.normalise_args(args)

        class PagingPresetClient:
            def __init__(self):
                self.calls = 0

            def json_request(self, method, path, **kwargs):
                self.calls += 1
                cursor = "next" if self.calls == 1 else None
                return {"items": [{"id": str(self.calls)}], "total": 99, "cursor": cursor}, None

        client = PagingPresetClient()
        with mock.patch.object(self.cli, "make_client", return_value=client):
            result = self.cli.run_presets(args)
        self.assertEqual(result["total"], 2)
        self.assertEqual(len(result["items"]), 2)

    def test_forbidden_errors_are_only_credit_errors_when_body_mentions_credits(self):
        credits = self.cli.error_from_http(403, b'{"detail":"balance is exhausted"}')
        self.assertEqual((credits.kind, credits.exit_code), ("credits", self.cli.EXIT_CREDITS))
        access = self.cli.error_from_http(403, b'{"detail":"region is not allowed"}')
        self.assertEqual((access.kind, access.exit_code), ("api", self.cli.EXIT_API))

    def test_estimate_failures_keep_distinct_exit_codes(self):
        class EstimateClient:
            def __init__(self, status):
                self.status = status

            def json_request(self, *args, **kwargs):
                raise self.cli.error_from_http(self.status, b'{"detail":"failure"}')

        unavailable_client = EstimateClient(404)
        unavailable_client.cli = self.cli
        with self.assertRaises(self.cli.EstimateUnavailable) as unavailable:
            self.cli.estimate_cost(unavailable_client, "/model", {})
        self.assertEqual(unavailable.exception.exit_code, self.cli.EXIT_ESTIMATE_UNAVAILABLE)

        failing_client = EstimateClient(500)
        failing_client.cli = self.cli
        with self.assertRaises(self.cli.ApiError) as failing:
            self.cli.estimate_cost(failing_client, "/model", {})
        self.assertEqual(failing.exception.exit_code, self.cli.EXIT_API)

    def test_terminal_status_fetch_avoids_controller_repoll(self):
        client = self.client()
        controller = mock.Mock()
        controller.request_id = "request-1"
        controller.response_url = "https://api.example/requests/request-1/status"
        controller.status.return_value = Completed()
        controller._transport.request.return_value.json.return_value = {
            "status": "completed", "request_id": "request-1",
        }
        payload = client._status_payload(controller)
        self.assertEqual(payload["status"], "completed")
        controller._transport.request.assert_called_once_with("GET", controller.response_url)
        controller.get.assert_not_called()

    def test_human_output_includes_request_id(self):
        output = io.StringIO()
        with mock.patch("sys.stdout", output):
            self.cli.emit_success({"status": "queued", "request_id": "request-1"}, False)
        self.assertIn("Request ID: request-1", output.getvalue())

    def test_image_without_model_is_validation_error_and_intermediate_json_is_accepted(self):
        with mock.patch("sys.stderr", io.StringIO()):
            self.assertEqual(self.cli.main(["image"]), self.cli.EXIT_VALIDATION)
        output = io.StringIO()
        with mock.patch("sys.stdout", output), mock.patch("sys.stderr", io.StringIO()):
            self.assertEqual(self.cli.main(["image", "--json"]), self.cli.EXIT_VALIDATION)
        self.assertEqual(json.loads(output.getvalue())["error"]["type"], "validation")

    def test_poll_interrupt_preserves_request_id(self):
        client = mock.Mock()
        with mock.patch.object(self.cli.time, "sleep", side_effect=KeyboardInterrupt):
            with self.assertRaises(self.cli.AppError) as raised:
                self.cli.poll_request(
                    client, {"request_id": "request-1", "status": "queued"},
                    timeout=60, interval=1, json_mode=True,
                )
        self.assertEqual((raised.exception.kind, raised.exception.request_id),
                         ("interrupted", "request-1"))

    def test_main_serializes_unhandled_interrupts_in_json_mode(self):
        output = io.StringIO()
        with mock.patch.object(self.cli, "run_generation", side_effect=KeyboardInterrupt):
            with mock.patch("sys.stdout", output), mock.patch("sys.stderr", io.StringIO()):
                self.assertEqual(
                    self.cli.main(["image", "grok-image-2", "--json"]),
                    self.cli.EXIT_INTERRUPTED,
                )
        self.assertEqual(json.loads(output.getvalue())["error"]["type"], "interrupted")

    def test_debug_mode_includes_exception_class_without_raw_details(self):
        output = io.StringIO()
        with mock.patch.object(self.cli, "run_credits", side_effect=RuntimeError("secret-value")):
            with mock.patch("sys.stdout", output), mock.patch("sys.stderr", io.StringIO()):
                self.assertEqual(self.cli.main(["credits", "--json", "--debug"]), self.cli.EXIT_API)
        rendered = output.getvalue()
        self.assertIn("RuntimeError", rendered)
        self.assertNotIn("secret-value", rendered)

    def test_existing_cli_parser_is_available_without_live_api(self):
        parser = self.cli.make_parser()
        args = parser.parse_args(["image", "grok-image-2", "--prompt", "cat"])
        self.assertEqual(args.image_model, "grok-image-2")

    def test_version_option_reports_cli_version(self):
        parser = self.cli.make_parser()
        output = io.StringIO()
        with mock.patch("sys.stdout", output):
            with self.assertRaises(SystemExit) as raised:
                parser.parse_args(["--version"])
        self.assertEqual(raised.exception.code, 0)
        self.assertEqual(output.getvalue(), f"{parser.prog} {self.cli.CLI_VERSION}\n")

    def test_new_models_are_registered_with_documented_endpoints(self):
        parser = self.cli.make_parser()
        cases = [
            (["image", "soul2", "--prompt", "portrait"], "soul2", "generate",
             "/higgsfield-ai/soul/v2/standard"),
            (["image", "ideogram4", "--prompt", "a cat"], "ideogram4", "generate",
             "/ideogram/v4.0"),
            (["video", "cinema-studio-4.0", "text", "--prompt", "a film"],
             "cinema-studio-4.0", "text", "/higgsfield/cinema-studio/4.0"),
            (["video", "kling-3.0-standard", "image", "--image", "https://example.com/start.jpg"],
             "kling-3.0-standard", "image", "/kling-video/v3.0/std/image-to-video"),
        ]
        for argv, model, workflow, endpoint in cases:
            args = parser.parse_args(argv)
            self.assertEqual((args.image_model if args.command == "image" else args.video_model), model)
            self.assertEqual(self.cli.endpoint_for(model, workflow), endpoint)

    def test_image_model_payloads_use_their_documented_fields(self):
        parser = self.cli.make_parser()
        soul_args = parser.parse_args([
            "image", "soul2", "--prompt", "editorial portrait", "--seed", "42",
            "--batch-size", "4", "--resolution", "1080p", "--aspect-ratio", "16:9",
            "--no-enhance-prompt",
        ])
        soul = self.cli.image_params(soul_args, "soul2", mock.Mock())
        self.assertEqual(soul, {
            "prompt": "editorial portrait", "seed": 42, "batch_size": 4,
            "resolution": "1080p", "aspect_ratio": "16:9", "enhance_prompt": False,
        })

        ideogram_args = parser.parse_args([
            "image", "ideogram4", "--prompt", "a cat", "--image", "https://example.com/cat.jpg",
            "--image-weight", "80", "--rendering-speed", "QUALITY", "--aspect-ratio", "3:2",
        ])
        ideogram = self.cli.image_params(ideogram_args, "ideogram4", mock.Mock())
        self.assertEqual(ideogram, {
            "prompt": "a cat", "image_url": "https://example.com/cat.jpg", "image_weight": 80,
            "aspect_ratio": "3:2", "rendering_speed": "QUALITY",
        })

    def test_video_duration_help_and_payload_ranges_are_model_specific(self):
        parser = self.cli.make_parser()
        with contextlib.redirect_stdout(io.StringIO()) as output:
            with self.assertRaises(SystemExit):
                parser.parse_args(["video", "kling-3.0-standard", "text", "-h"])
        self.assertIn("3-15 s", output.getvalue())

        kling_args = parser.parse_args([
            "video", "kling-3.0-standard", "text", "--prompt", "a tracking shot",
            "--duration", "15", "--no-generate-audio", "--cfg-scale", "0.8", "--multi-shots",
            "--aspect-ratio", "9:16",
        ])
        kling = self.cli.video_params(kling_args, "kling3", "text", mock.Mock())
        self.assertEqual(kling, {
            "prompt": "a tracking shot", "duration": 15, "sound": "off", "cfg_scale": 0.8,
            "multi_shots": True, "aspect_ratio": "9:16",
        })

        cinema_args = parser.parse_args([
            "video", "cinema-studio-4.0", "reference", "--prompt", "a film",
            "--duration", "30", "--image-ref", "https://example.com/reference.jpg",
        ])
        cinema = self.cli.video_params(cinema_args, "cinema4", "reference", mock.Mock())
        self.assertEqual(cinema, {
            "prompt": "a film", "duration": 30, "resolution": "720p", "aspect_ratio": "16:9",
            "generate_audio": True, "image_urls": ["https://example.com/reference.jpg"],
        })

        with self.assertRaises(self.cli.AppError):
            invalid = parser.parse_args([
                "video", "kling-3.0-standard", "text", "--prompt", "a shot", "--duration", "16",
            ])
            self.cli.video_params(invalid, "kling3", "text", mock.Mock())

    def test_error_taxonomy_separates_concurrency_and_estimate_unavailable(self):
        concurrency = self.cli.error_from_http(
            400, b'{"detail":"model concurrency limit reached"}'
        )
        self.assertEqual(concurrency.kind, "concurrency")
        self.assertEqual(concurrency.exit_code, self.cli.EXIT_CONCURRENCY)

        estimate = self.cli.EstimateUnavailable("Estimate endpoint unavailable")
        self.assertEqual(estimate.kind, "estimate_unavailable")
        self.assertEqual(estimate.exit_code, self.cli.EXIT_ESTIMATE_UNAVAILABLE)

    def test_sdk_error_recovers_http_response_from_exception_cause(self):
        class SdkError(Exception):
            pass

        class HttpStatusError(Exception):
            pass

        cause = HttpStatusError("403 Forbidden")
        cause.response = SimpleNamespace(
            status_code=403,
            content=b'{"detail":"Insufficient credits: balance 0"}',
            headers={"X-Correlation-ID": "corr-123"},
        )
        sdk_failure = SdkError("Insufficient credits: balance 0")
        sdk_failure.__cause__ = cause

        converted = self.cli.sdk_error(sdk_failure, self.cli.Credentials("id", "secret"))
        self.assertEqual(converted.kind, "credits")
        self.assertEqual(converted.exit_code, self.cli.EXIT_CREDITS)
        self.assertIn("balance 0", converted.message)
        self.assertEqual(converted.correlation_id, "corr-123")

    def test_sdk_error_maps_typed_and_httpx_failures(self):
        typed_cases = [
            (type("InsufficientCreditsError", (Exception,), {}), "credits", self.cli.EXIT_CREDITS),
            (type("AgentAccessDeniedError", (Exception,), {}), "authentication", self.cli.EXIT_AUTHENTICATION),
            (type("SessionBusyError", (Exception,), {}), "concurrency", self.cli.EXIT_CONCURRENCY),
        ]
        for error_type, kind, exit_code in typed_cases:
            converted = self.cli.sdk_error(error_type("failure"), self.cli.Credentials("id", "secret"))
            self.assertEqual((converted.kind, converted.exit_code), (kind, exit_code))

        connect_error_type = type("ConnectError", (Exception,), {"__module__": "httpx"})
        converted = self.cli.sdk_error(connect_error_type("connection refused"),
                                        self.cli.Credentials("id", "secret"))
        self.assertEqual((converted.kind, converted.exit_code), ("network", self.cli.EXIT_NETWORK))


if __name__ == "__main__":
    unittest.main()
