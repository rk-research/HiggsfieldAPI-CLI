# Copyright (c) 2026 Richard Knuchel
# SPDX-License-Identifier: BSD-2-Clause
"""Offline tests for the MCP server and its Higgsfield SDK boundary.

All provider operations below use synthetic responses.  The preferred entry
point is ``python test_offline_suite.py``, which blocks socket and DNS access
before importing either test module.
"""

from __future__ import annotations

import asyncio
import argparse
import contextlib
import importlib.util
import io
import json
import os
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from test_offline_suite import network_blocked


SERVER_PATH = Path(__file__).with_name("HiggsfieldAPI-MCP.py")


def _load_server():
    spec = importlib.util.spec_from_file_location("higgsfield_api_mcp_test", SERVER_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("Unable to import MCP server")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class FakeHiggsfieldClient:
    """Synthetic SDK-compatible client with no network-capable methods."""

    def __init__(self):
        self.calls: list[tuple[str, str, dict | None]] = []
        self.next_submission = {"status": "queued", "request_id": "synthetic-request-1"}
        self.status_payloads: dict[str, dict] = {}
        self.status_sequences: dict[str, list[dict]] = {}
        self.preset_payloads = [{"items": [{"id": "preset-1", "name": "Summer"}], "total": 1}]
        self.downloaded: list[tuple[str, Path]] = []
        self.uploaded: list[Path] = []
        self.failure: Exception | None = None
        self.max_download_size = 2 * 1024 * 1024 * 1024
        self.max_input_file_size = 1024 * 1024
        self.download_timeout = 30
        self.credentials = None
        self._client = self

    def json_request(self, method, path_or_url, payload=None, **_kwargs):
        self.calls.append((method.upper(), path_or_url, payload))
        if self.failure is not None:
            raise self.failure
        if method.upper() == "POST" and path_or_url.startswith("/estimate"):
            return {"credits": 7, "usd": "0.70"}, None
        if method.upper() == "POST":
            return dict(self.next_submission), SimpleNamespace(status=202, headers={}, body=b"")
        if path_or_url.startswith("/requests/"):
            request_id = path_or_url.split("/")[2]
            sequence = self.status_sequences.get(request_id)
            if sequence:
                return dict(sequence.pop(0)), None
            return dict(self.status_payloads.get(request_id, {
                "request_id": request_id, "status": "in_progress",
            })), None
        if path_or_url.startswith("/marketing-studio/image/presets"):
            return self.preset_payloads.pop(0), None
        raise AssertionError(f"Unexpected fake-provider operation: {method} {path_or_url}")

    def request(self, method, path_or_url, **_kwargs):
        self.calls.append((method.upper(), path_or_url, None))
        if self.failure is not None:
            raise self.failure
        return SimpleNamespace(status=200)

    def upload_file(self, path):
        self.uploaded.append(Path(path))
        return "https://media.example/uploaded.jpg"

    def download_file(self, url, target):
        self.downloaded.append((url, Path(target)))

    def close(self):
        pass


def _tool_data(result):
    data = getattr(result, "structuredContent", None)
    if data is None:
        for block in getattr(result, "content", []):
            if getattr(block, "type", None) == "text":
                try:
                    return json.loads(block.text)
                except (ValueError, TypeError):
                    pass
        return None
    return data


class HiggsfieldMcpTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.mcp = _load_server()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.fake = FakeHiggsfieldClient()
        self.get_client_impl = self.mcp._get_client
        self.network_guard = network_blocked()
        self.network_guard.__enter__()
        self.addCleanup(self.network_guard.__exit__, None, None, None)
        def fake_dns(host, port=None, *args, **kwargs):
            if isinstance(host, str) and host.endswith(".example"):
                return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "",
                         ("93.184.216.34", port or 443))]
            raise AssertionError(f"Unexpected DNS lookup in offline tests: {host}")
        self.dns_patch = mock.patch.object(self.mcp.socket, "getaddrinfo", side_effect=fake_dns)
        self.dns_patch.start()
        self.addCleanup(self.dns_patch.stop)
        self.make_client_patch = mock.patch.object(
            self.mcp, "_get_client", side_effect=lambda _args, _roots, _output: self.fake
        )
        self.make_client_patch.start()
        self.cli_client_patch = mock.patch.object(self.mcp.cli, "make_client", return_value=self.fake)
        self.cli_client_patch.start()
        self.addCleanup(self.cli_client_patch.stop)
        # Parse-only startup never reads caller credentials; use synthetic,
        # non-secret values for code paths that inspect the environment.
        self.env_patch = mock.patch.dict(os.environ, {
            "HF_API_KEY_ID": "offline-test-id",
            "HF_API_KEY_SECRET": "offline-test-secret",
            "HF_API_BASE_URL": "https://offline.invalid",
        })
        self.env_patch.start()
        self.args = self.mcp.parse_args([
            "--output-dir", str(self.root / "out"), "--input-dir", str(self.root),
        ])
        self.args.allowed_host = ["mcp.test"]
        self.state = self.mcp.ServerState(self.args)

    def tearDown(self):
        self.env_patch.stop()
        self.make_client_patch.stop()
        self.temp.cleanup()

    def _arguments(self, model, workflow):
        schema, _ = self.mcp._workflow_schema(model, workflow)
        names = {parameter.name for parameter in schema}
        arguments = {}
        if "prompt" in names:
            arguments["prompt"] = "Synthetic test generation"
        if "image" in names:
            action = next(action for action in self.mcp.LEAF_PARSERS[(model, workflow)]._actions
                          if action.dest == "image")
            image_url = "https://assets.example/input.jpg"
            arguments["image"] = [image_url] if isinstance(action, argparse._AppendAction) else image_url
        if "video" in names:
            arguments["video"] = "https://assets.example/input.mp4"
        if "image_ref" in names:
            arguments["image_ref"] = ["https://assets.example/ref.jpg"]
        return arguments

    def _in_memory_session(self, server=None):
        from mcp.shared.memory import create_connected_server_and_client_session

        return create_connected_server_and_client_session(server or self.mcp.create_server(self.args))

    def test_default_transport_allows_docker_host_at_selected_port(self):
        self.args.allowed_host = []
        self.args.port = 9876
        security = self.mcp.create_server(self.args).settings.transport_security
        self.assertTrue(security.enable_dns_rebinding_protection)
        self.assertIn("host.docker.internal:9876", security.allowed_hosts)
        self.assertNotIn("host.docker.internal:8765", security.allowed_hosts)
        self.assertNotIn("*", security.allowed_hosts)

    def test_explicit_allowed_hosts_replace_defaults(self):
        security = self.mcp.create_server(self.args).settings.transport_security
        self.assertEqual(security.allowed_hosts, ["mcp.test"])

    async def test_mcp_initialization_and_full_tool_discovery(self):
        server = self.mcp.create_server(self.args)
        async with self._in_memory_session(server) as session:
            await session.initialize()
            tools = await session.list_tools()
        names = {tool.name for tool in tools.tools}
        expected = {
            f"higgsfield_{model.replace('-', '_').replace('.', '_')}_{workflow}"
            for model, workflow, _kind in self.mcp.WORKFLOWS
        }
        expected |= {
            f"higgsfield_estimate_{model.replace('-', '_').replace('.', '_')}_{workflow}"
            for model, workflow, _kind in self.mcp.WORKFLOWS
        }
        expected |= {
            "higgsfield_status", "higgsfield_result", "higgsfield_cancel",
            "higgsfield_wait", "higgsfield_presets", "higgsfield_credits",
            "higgsfield_upload",
        }
        self.assertEqual(names, expected)
        for tool in tools.tools:
            self.assertIsInstance(tool.inputSchema, dict)
            self.assertEqual(tool.inputSchema.get("type"), "object")

    async def test_each_supported_generation_workflow_invokes_one_async_submission(self):
        server = self.mcp.create_server(self.args)
        async with self._in_memory_session(server) as session:
            await session.initialize()
            for model, workflow, _kind in self.mcp.WORKFLOWS:
                tool_name = f"higgsfield_{model.replace('-', '_').replace('.', '_')}_{workflow}"
                with self.subTest(model=model, workflow=workflow):
                    result = await session.call_tool(tool_name, self._arguments(model, workflow))
                    payload = _tool_data(result)
                    self.assertTrue(payload["ok"], payload)
                    self.assertEqual(payload["request_id"], "synthetic-request-1")
                    self.assertTrue(payload["async"])
                    self.assertIsNone(payload["estimated_cost"])
                    self.assertIsNone(payload["charged_cost"])
                    self.assertEqual(payload["cost"]["confirmed"], False)
        submissions = [call for call in self.fake.calls if call[0] == "POST"]
        self.assertEqual(len(submissions), len(self.mcp.WORKFLOWS))
        self.assertFalse(any(path.startswith("/estimate") for _, path, _ in submissions))

    async def test_workflow_tools_expose_typed_model_specific_schemas(self):
        server = self.mcp.create_server(self.args)
        async with self._in_memory_session(server) as session:
            await session.initialize()
            tools = {tool.name: tool for tool in (await session.list_tools()).tools}
        marketing = tools["higgsfield_marketing_studio_generate"].inputSchema["properties"]
        self.assertIn("quality", marketing)
        self.assertIn("aspect_ratio", marketing)
        soul = tools["higgsfield_soul2_generate"].inputSchema["properties"]
        self.assertIn("batch_size", soul)
        seedance_edit = tools["higgsfield_seedance_2_5_edit"].inputSchema["properties"]
        self.assertIn("video", seedance_edit)
        self.assertIn("output_format", seedance_edit)
        with self.assertRaises((ValueError, TypeError)):
            self.mcp._validate_action_values("soul2", "generate", {"batch_size": 2})
        invalid_duration = self.state.submit("kling-3.0-standard", "text", {
            "prompt": "synthetic", "duration": 90,
        })
        self.assertFalse(invalid_duration["ok"])
        self.assertEqual(self.fake.calls, [])

    async def test_advanced_parameters_reject_secrets_paths_and_typed_field_overrides(self):
        invalid_values = (
            {"api_key": "synthetic"},
            {"prompt": "override"},
            {"media_path": str(self.root / "secret.jpg")},
            {"media_url": "http://private.example/file.jpg"},
        )
        for value in invalid_values:
            with self.subTest(value=value):
                result = self.state.submit("soul2", "generate", {"prompt": "safe"}, value)
                self.assertFalse(result["ok"])
        too_large = {"opaque": "x" * (self.mcp.MAX_TOOL_TEXT + 1)}
        oversized = self.state.submit("soul2", "generate", {"prompt": "safe"}, too_large)
        self.assertFalse(oversized["ok"])
        self.assertEqual(self.fake.calls, [])

    async def test_version_matches_cli_without_starting_server(self):
        self.assertEqual(self.mcp.MCP_VERSION, self.mcp.cli.CLI_VERSION)
        stdout = io.StringIO()
        with mock.patch.object(sys, "argv", [SERVER_PATH.name]), \
                mock.patch.object(sys, "version_info", (3, 11)), \
                mock.patch.object(self.mcp, "create_server") as create_server, \
                contextlib.redirect_stdout(stdout):
            with self.assertRaises(SystemExit) as raised:
                self.mcp.main(["--version"])
        self.assertEqual(raised.exception.code, 0)
        self.assertEqual(stdout.getvalue(), f"{SERVER_PATH.name} {self.mcp.MCP_VERSION}\n")
        create_server.assert_not_called()

    async def test_invalid_timeout_arguments_are_rejected_at_startup(self):
        for flag_value in (("--operation-timeout", "nan"), ("--poll-interval", "-1"),
                           ("--http-timeout", "inf")):
            with self.subTest(flag_value=flag_value), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    self.mcp.parse_args([*flag_value, "--output-dir", str(self.root / "bad")])

    async def test_sdk_transport_is_configured_for_no_retries_before_generation(self):
        from higgsfield_client.http.retry import NoRetry

        raw = self.mcp.cli.SDKClient(self.mcp.cli.Credentials("offline-id", "offline-secret"))
        with mock.patch.object(self.mcp.cli, "make_client", return_value=raw):
            guarded = self.get_client_impl(self.args, self.state.roots, self.state.output_dir)
        try:
            self.assertIsInstance(raw.sdk._transport._retry_strategy, NoRetry)
        finally:
            guarded.close()

    async def test_submit_never_estimates_and_returns_promptly_with_request_id(self):
        response = self.state.submit("grok-image-2", "generate", {"prompt": "synthetic"})
        self.assertTrue(response["ok"])
        self.assertEqual(response["request_id"], "synthetic-request-1")
        self.assertEqual([path for method, path, _ in self.fake.calls if method == "POST"],
                         [self.mcp.cli.endpoint_for("grok-image-2", "generate")])
        self.assertIsNone(response["cost"]["actual_cost"])

    async def test_terminal_status_extracts_authoritative_cost_and_result_is_retrievable(self):
        self.fake.status_payloads["synthetic-request-1"] = {
            "request_id": "synthetic-request-1", "status": "completed",
            "charged_cost": {"amount": "0.0375", "currency": "USD", "credits": 4},
            "images": [{"url": "https://cdn.example/final.jpg"}],
        }
        result = self.state.status("synthetic-request-1")
        self.assertEqual(result["charged_cost"]["amount"], "0.0375")
        self.assertEqual(result["cost"]["actual_cost"], "0.0375")
        self.assertEqual(result["cost"]["currency"], "USD")
        self.assertTrue(result["cost"]["confirmed"])
        self.assertEqual(result["outputs"][0]["url"], "https://cdn.example/final.jpg")
        self.assertEqual(self.state.status("synthetic-request-1")["charged_cost"]["amount"], "0.0375")

    async def test_unknown_or_untrusted_actual_cost_is_never_filled_from_estimate(self):
        for value in (None, "not-a-number", -2, float("nan")):
            with self.subTest(charged_cost=value):
                metadata = self.mcp._cost_metadata({
                    "request_id": "synthetic-request-1", "status": "completed",
                    "charged_cost": value,
                    "estimated_cost": {"usd": 9.99, "credits": 500},
                })
                self.assertIsNone(metadata["actual_cost"])
                self.assertFalse(metadata["confirmed"])
        metadata = self.mcp._cost_metadata({
            "request_id": "synthetic-request-1", "status": "completed",
            "charged_cost": {"amount": "12", "currency": "credits"},
        })
        self.assertIsNone(metadata["actual_cost"] if metadata["currency"] == "USD" else None)

    async def test_credit_only_authoritative_cost_preserves_credits_without_usd_conversion(self):
        metadata = self.mcp._cost_metadata({
            "request_id": "synthetic-request-1", "status": "completed",
            "charged_cost": {"credits": 8},
        })
        self.assertIsNone(metadata["actual_cost"])
        self.assertIsNone(metadata["currency"])
        self.assertEqual(metadata["credits"], "8")
        self.assertTrue(metadata["confirmed"])
        self.assertFalse(metadata["billable"])

    async def test_estimate_only_uses_mock_estimate_and_never_submits_generation(self):
        response = self.state.estimate("soul2", "generate", {"prompt": "synthetic"})
        self.assertTrue(response["ok"])
        self.assertEqual(response["status"], "estimate_only")
        self.assertEqual(response["estimated_cost"]["usd"], "0.70")
        self.assertIsNone(response["charged_cost"])
        self.assertFalse(response["cost"]["confirmed"])
        self.assertEqual(len(self.fake.calls), 1)
        self.assertTrue(self.fake.calls[0][1].startswith("/estimate"))

    async def test_estimate_unavailable_is_reported_without_fake_zero_cost(self):
        with mock.patch.object(self.mcp.cli, "estimate_cost", side_effect=self.mcp.cli.EstimateUnavailable(
                "synthetic unavailable")):
            response = self.state.estimate("soul2", "generate", {"prompt": "synthetic"})
        self.assertFalse(response["ok"])
        self.assertTrue(response["estimate_unavailable"])
        self.assertIsNone(response["estimated_cost"])
        self.assertIsNone(response["charged_cost"])

    async def test_generation_proceeds_when_optional_estimation_is_unavailable(self):
        response = self.state.submit("soul2", "generate", {"prompt": "synthetic"})
        self.assertTrue(response["ok"])
        self.assertEqual(response["status"], "queued")
        self.assertIsNone(response["estimated_cost"])
        self.assertIsNone(response["charged_cost"])

    async def test_local_image_upload_uses_only_allowlisted_file(self):
        image = self.root / "input.jpg"
        image.write_bytes(b"synthetic-image")
        response = self.state.submit("marketing-studio", "generate", {
            "prompt": "Synthetic product image", "image": [str(image)],
        })
        self.assertTrue(response["ok"])
        self.assertEqual(self.fake.uploaded, [image.resolve()])
        self.assertEqual(len([call for call in self.fake.calls if call[0] == "POST"]), 1)

    async def test_upload_tool_returns_server_local_metadata_and_rejects_escape(self):
        asset = self.root / "upload.png"
        asset.write_bytes(b"synthetic-image")
        server = self.mcp.create_server(self.args)
        async with self._in_memory_session(server) as session:
            await session.initialize()
            success = await session.call_tool("higgsfield_upload", {"path": str(asset)})
            data = _tool_data(success)
            self.assertEqual(data["url"], "https://media.example/uploaded.jpg")
            self.assertTrue(data["file_server_local"])
            invalid = await session.call_tool("higgsfield_upload", {"path": str(self.root.parent / "outside.jpg")})
            self.assertTrue(invalid.isError)

    async def test_mcp_wait_returns_successful_progress_and_can_resume(self):
        self.args.wait_call_timeout = 0.3
        self.fake.status_payloads["bounded-1"] = {"request_id": "bounded-1", "status": "in_progress"}
        async with self._in_memory_session() as session:
            await session.initialize()
            pending = await session.call_tool("higgsfield_wait", {"request_id": "bounded-1"})
            self.assertFalse(pending.isError)
            self.assertTrue(pending.structuredContent["ok"])
            self.assertFalse(pending.structuredContent["wait_complete"])
            self.assertEqual(pending.structuredContent["status"], "in_progress")
            self.fake.status_payloads["bounded-1"]["status"] = "completed"
            final = await session.call_tool("higgsfield_wait", {"request_id": "bounded-1", "download": True})
            self.assertFalse(final.isError)
            self.assertTrue(final.structuredContent["wait_complete"])
            self.assertTrue(final.structuredContent["download_pending"])
        self.assertFalse(any(call[0] == "POST" for call in self.fake.calls))

    async def test_mcp_wait_bounds_slow_status_lookup(self):
        self.args.wait_call_timeout = 0.01
        async def slow_lookup(*_args):
            await asyncio.sleep(10)
        with mock.patch.object(self.mcp.asyncio, "to_thread", side_effect=slow_lookup):
            async with self._in_memory_session() as session:
                await session.initialize()
                result = await asyncio.wait_for(session.call_tool("higgsfield_wait", {"request_id": "slow-1"}), 1)
        self.assertFalse(result.isError)
        self.assertFalse(result.structuredContent["wait_complete"])
        self.assertEqual(result.structuredContent["request_id"], "slow-1")

    async def test_status_wait_polls_until_terminal_and_network_timeout_never_resubmits(self):
        self.state.args.poll_interval = 0.001
        self.state.args.operation_timeout = 1
        self.fake.status_sequences["poll-1"] = [
            {"request_id": "poll-1", "status": "in_progress"},
            {"request_id": "poll-1", "status": "completed",
             "charged_cost": {"amount": "0.12", "currency": "USD"}},
        ]
        final = self.state.status("poll-1", wait=True)
        self.assertEqual(final["status"], "completed")
        status_calls = [call for call in self.fake.calls if call[1] == "/requests/poll-1/status"]
        self.assertGreaterEqual(len(status_calls), 2)

        self.fake.calls.clear()
        self.fake.failure = self.mcp.cli.ApiError("synthetic timeout", kind="network")
        failed = self.state.submit("grok-image-2", "generate", {"prompt": "synthetic"})
        self.assertFalse(failed["ok"])
        posts = [call for call in self.fake.calls if call[0] == "POST"]
        self.assertEqual(len(posts), 1)

    async def test_status_wait_cancel_failures_and_moderation_are_classified(self):
        self.fake.status_payloads["failed-1"] = {
            "request_id": "failed-1", "status": "failed", "error": "synthetic failure",
        }
        failed = self.state.status("failed-1")
        self.assertFalse(failed["ok"])
        self.assertEqual(failed["status"], "failed")
        self.fake.status_payloads["moderated-1"] = {
            "request_id": "moderated-1", "status": "nsfw",
        }
        moderated = self.state.status("moderated-1")
        self.assertFalse(moderated["ok"])
        self.assertEqual(moderated["status"], "nsfw")
        canceled = self.state.cancel("synthetic-request-1")
        self.assertTrue(canceled["ok"])
        self.assertEqual(canceled["status"], "canceled")
        self.assertEqual(self.fake.calls[-1][1], "/requests/synthetic-request-1/cancel")
        with self.assertRaises(ValueError):
            self.state.status("../../outside")

    async def test_presets_search_and_pagination_and_balance_discovery(self):
        self.fake.preset_payloads = [
            {"items": [{"id": "one", "name": "Summer"}], "total": 2, "cursor": "next"},
            {"items": [{"id": "two", "name": "Winter"}], "total": 2},
        ]
        server = self.mcp.create_server(self.args)
        async with self._in_memory_session(server) as session:
            await session.initialize()
            page = _tool_data(await session.call_tool("higgsfield_presets", {"all_pages": True}))
            credits = _tool_data(await session.call_tool("higgsfield_credits", {}))
        self.assertEqual(len(page["items"]), 2)
        self.assertFalse(credits["supported"])
        self.assertEqual(len([c for c in self.fake.calls if "/marketing-studio/image/presets" in c[1]]), 2)

    async def test_streamable_http_initialization_discovery_and_tool_call_in_memory(self):
        import httpx
        from mcp.client.session import ClientSession
        from mcp.client.streamable_http import streamable_http_client

        server = self.mcp.create_server(self.args)
        server.settings.streamable_http_path = "/mcp"
        app = self.mcp.BearerAuth(server.streamable_http_app(), "synthetic-token")
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://mcp.test",
            headers={"Authorization": "Bearer synthetic-token"},
        )
        try:
            async with server.session_manager.run():
                async with streamable_http_client("http://mcp.test/mcp", http_client=client) as (read, write, _):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        tools = await session.list_tools()
                        self.assertIn("higgsfield_soul2_generate", {tool.name for tool in tools.tools})
                        result = await session.call_tool("higgsfield_soul2_generate", {"prompt": "synthetic"})
                        self.assertTrue(_tool_data(result)["ok"])
                        self.assertEqual(_tool_data(result)["request_id"], "synthetic-request-1")
                rejected = await client.post(
                    "http://evil.test/mcp", json={"jsonrpc": "2.0", "id": 99,
                    "method": "initialize", "params": {}},
                )
                self.assertEqual(rejected.status_code, 421)
        finally:
            await client.aclose()

    async def test_http_reconnect_and_late_notifications_do_not_reuse_closed_session(self):
        import httpx

        self.args.wait_call_timeout = 0.3
        server = self.mcp.create_server(self.args)
        self.assertTrue(server.settings.stateless_http)
        self.assertTrue(server.settings.json_response)
        app = self.mcp.BearerAuth(
            self.mcp.RequestBodyLimit(server.streamable_http_app(), self.args.max_request_body_size),
            "synthetic-token")
        headers = {"Authorization": "Bearer synthetic-token",
                   "Accept": "application/json, text/event-stream",
                   "MCP-Protocol-Version": "2025-11-25"}
        # Separate clients model the gateway's per-call connection lifecycle.
        async def post(payload, extra_headers=None):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url="http://mcp.test", headers=headers) as client:
                return await client.post("/mcp", json=payload, headers=extra_headers)

        def tool_call(identifier, name, arguments):
            return {"jsonrpc": "2.0", "id": identifier, "method": "tools/call",
                    "params": {"name": name, "arguments": arguments}}

        with self.assertNoLogs("mcp.server.streamable_http", level="ERROR"):
            async with server.session_manager.run():
                initialized = await post({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                    "params": {"protocolVersion": "2025-11-25", "capabilities": {},
                               "clientInfo": {"name": "offline-gateway", "version": "1"}}})
                self.assertEqual(initialized.status_code, 200)
                self.assertNotIn("mcp-session-id", initialized.headers)
                submitted = await post(tool_call(2, "higgsfield_grok_image_2_generate", {"prompt": "synthetic"}))
                request_id = submitted.json()["result"]["structuredContent"]["request_id"]
                self.fake.status_payloads[request_id] = {"request_id": request_id, "status": "in_progress"}
                pending = await post(tool_call(3, "higgsfield_wait", {"request_id": request_id}))
                self.assertFalse(pending.json()["result"]["isError"])
                self.assertFalse(pending.json()["result"]["structuredContent"]["wait_complete"])
                # A gateway may retain an obsolete session header and send
                # cancellation/initialized notifications after a connection ends.
                for method, params in [("notifications/cancelled", {"requestId": 3}),
                                       ("notifications/initialized", {})]:
                    notification = await post({"jsonrpc": "2.0", "method": method, "params": params},
                                              {"Mcp-Session-Id": "obsolete-session"})
                    self.assertEqual(notification.status_code, 202)
                self.fake.status_payloads[request_id]["status"] = "completed"
                completed = await post(tool_call(4, "higgsfield_wait", {"request_id": request_id}),
                                       {"Mcp-Session-Id": "obsolete-session"})
                self.assertEqual(completed.status_code, 200)
                self.assertTrue(completed.json()["result"]["structuredContent"]["wait_complete"])
                self.assertEqual(completed.json()["result"]["structuredContent"]["request_id"], request_id)
        self.assertEqual(len([call for call in self.fake.calls if call[0] == "POST"]), 1)

    async def test_failure_moderation_and_unknown_tool_arguments_return_errors(self):
        server = self.mcp.create_server(self.args)
        self.fake.status_payloads["failed-1"] = {
            "request_id": "failed-1", "status": "failed", "error": "synthetic failure",
        }
        async with self._in_memory_session(server) as session:
            await session.initialize()
            failed = await session.call_tool("higgsfield_status", {"request_id": "failed-1"})
            self.assertTrue(failed.isError)
            invalid = await session.call_tool("higgsfield_soul2_generate", {
                "prompt": "synthetic", "unsupported_provider_request": {"url": "https://x"},
            })
            self.assertTrue(invalid.isError)

    async def test_local_files_are_allowlisted_and_symlink_escape_is_rejected(self):
        allowed = self.root / "allowed.jpg"
        allowed.write_bytes(b"image")
        self.assertEqual(self.mcp._resolve_allowed(allowed, (self.root,)), allowed.resolve())
        outside_dir = Path(tempfile.mkdtemp())
        try:
            outside = outside_dir / "outside.jpg"
            outside.write_bytes(b"outside")
            link = self.root / "link.jpg"
            try:
                link.symlink_to(outside)
            except (OSError, NotImplementedError):
                self.skipTest("Symlink creation is unavailable on this Windows account")
            with self.assertRaises(ValueError):
                self.mcp._resolve_allowed(link, (self.root,))
        finally:
            for path in outside_dir.iterdir():
                path.unlink()
            outside_dir.rmdir()

    async def test_prompt_file_must_stay_inside_input_roots_and_obey_size_cap(self):
        outside_dir = Path(tempfile.mkdtemp())
        try:
            external_prompt = outside_dir / "prompt.txt"
            external_prompt.write_text("synthetic prompt", encoding="utf-8")
            with self.assertRaises(ValueError):
                self.state.submit("soul2", "generate", {"prompt_file": str(external_prompt)})
            self.assertEqual(self.fake.calls, [])
        finally:
            external_prompt.unlink(missing_ok=True)
            outside_dir.rmdir()

        too_large = self.root / "too-large.txt"
        too_large.write_text("x" * (self.mcp.cli.DEFAULT_MAX_INPUT_FILE_SIZE + 1), encoding="utf-8")
        with self.assertRaises(ValueError):
            self.state.submit("soul2", "generate", {"prompt_file": str(too_large)})
        self.assertEqual(self.fake.calls, [])

    async def test_remote_media_url_policy_and_output_download_guard(self):
        for unsafe in ("http://example.com/a.jpg", "https://127.0.0.1/a.jpg",
                       "https://user:pass@example.com/a.jpg", "https://example.com:8443/a.jpg"):
            with self.subTest(url=unsafe), self.assertRaises(ValueError):
                self.mcp._safe_remote_url(unsafe)
        safe = "https://cdn.example/final.jpg"
        target = self.root / "out" / "final.jpg"
        client = self.mcp.GuardedClient(self.fake, (self.root,), (self.root / "out").resolve())
        class FakeResponse:
            headers = {}
            def __enter__(self):
                return self
            def __exit__(self, *_args):
                return None
            def read(self, _size=-1):
                if hasattr(self, "done"):
                    return b""
                self.done = True
                return b"synthetic-output"
        fake_opener = mock.Mock()
        fake_opener.open.return_value = FakeResponse()
        with mock.patch.object(self.mcp.urllib.request, "build_opener", return_value=fake_opener):
            client.download_file(safe, target)
        self.assertEqual(target.read_bytes(), b"synthetic-output")
        with self.assertRaises(ValueError):
            client.download_file(safe, self.root / "outside.jpg")

    def test_litellm_raw_key_matches_gateway_hash_and_existing_ledger(self):
        # A fixed synthetic key/digest guards LiteLLM's SHA-256 identifier
        # contract without calling a provider or requiring credentials.
        gateway_hash = "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
        raw = {"litellm_params": {"metadata": {"user_api_key": "abc"}}}
        hashed = {"litellm_params": {"metadata": {"user_api_key_hash": gateway_hash}}}
        self.assertEqual(self.mcp._key_attribution(raw), gateway_hash)
        self.assertEqual(self.mcp._key_attribution(hashed), gateway_hash)
        with mock.patch.dict(os.environ, {"HF_MCP_ACCOUNTING_DB": str(self.root / "ledger.sqlite3")}):
            self.mcp._ledger_record("higgsfield", "synthetic-request-key", gateway_hash)
            self.assertTrue(self.mcp._ledger_claim(
                "higgsfield", "synthetic-request-key", self.mcp._key_attribution(raw)))
            self.assertFalse(self.mcp._ledger_claim(
                "higgsfield", "synthetic-request-key", self.mcp._key_attribution(hashed)))

    async def test_litellm_cost_hook_books_originating_usd_charge_once_across_restarts(self):
        ledger = self.root / "ledger.sqlite3"
        with mock.patch.dict(os.environ, {"HF_MCP_ACCOUNTING_DB": str(ledger)}):
            response = SimpleNamespace(
                mcp_tool_call_response={"request_id": "synthetic-request-1", "status": "queued",
                    "litellm_costs": True, "cost": {"confirmed": False}},
                hidden_params=SimpleNamespace(response_cost=None),
            )
            origin = {"mcp_tool_call_metadata": {"name": "higgsfield_soul2_generate",
                        "mcp_server_name": "higgsfield"},
                      "litellm_params": {"metadata": {"user_api_key_hash": "key-origin"}}}
            await self.mcp._litellm_post_tool_hook(origin, response, None, None)
            completed = SimpleNamespace(
                mcp_tool_call_response={"request_id": "synthetic-request-1", "status": "completed",
                    "litellm_costs": True, "cost": {"actual_cost": "0.42", "currency": "USD",
                    "confirmed": True, "billable": True}},
                hidden_params=SimpleNamespace(response_cost=None),
            )
            same_origin = {"mcp_tool_call_metadata": {"name": "higgsfield_status",
                            "mcp_server_name": "higgsfield"},
                           "litellm_params": {"metadata": {"user_api_key_hash": "key-origin"}}}
            await self.mcp._litellm_post_tool_hook(same_origin, completed, None, None)
            self.assertEqual(completed.hidden_params.response_cost, 0.42)
            # Importing the callback source afresh simulates a LiteLLM process
            # restart: the SQLite ledger must still suppress later bookings.
            fresh_spec = importlib.util.spec_from_file_location("higgsfield_cost_tracker_reloaded", SERVER_PATH)
            fresh_module = importlib.util.module_from_spec(fresh_spec)
            fresh_spec.loader.exec_module(fresh_module)

            async def repeated_call():
                repeated = SimpleNamespace(
                    mcp_tool_call_response=completed.mcp_tool_call_response,
                    hidden_params=SimpleNamespace(response_cost=None),
                )
                await fresh_module._litellm_post_tool_hook(same_origin, repeated, None, None)
                return repeated.hidden_params.response_cost

            repeats = await asyncio.gather(*(repeated_call() for _ in range(8)))
            self.assertEqual(repeats.count(0), 8)
            other_origin = {"mcp_tool_call_metadata": {"name": "higgsfield_result",
                             "mcp_server_name": "higgsfield"},
                            "litellm_params": {"metadata": {"user_api_key_hash": "key-other"}}}
            other = SimpleNamespace(mcp_tool_call_response=completed.mcp_tool_call_response,
                                    hidden_params=SimpleNamespace(response_cost=None))
            await fresh_module._litellm_post_tool_hook(other_origin, other, None, None)
            self.assertIsNone(other.hidden_params.response_cost)

    async def test_litellm_hook_skips_unknown_and_estimated_costs(self):
        with mock.patch.dict(os.environ, {"HF_MCP_ACCOUNTING_DB": str(self.root / "ledger.sqlite3")}):
            unknown = SimpleNamespace(
                mcp_tool_call_response={"request_id": "synthetic-request-unknown", "status": "completed",
                    "litellm_costs": True, "estimated_cost": {"usd": 1.0}, "cost": {
                        "confirmed": False, "billable": False, "actual_cost": None}},
                hidden_params=SimpleNamespace(response_cost=None),
            )
            kwargs = {"mcp_tool_call_metadata": {"name": "higgsfield_status",
                       "mcp_server_name": "higgsfield"},
                      "litellm_params": {"metadata": {"user_api_key_hash": "key-origin"}}}
            await self.mcp._litellm_post_tool_hook(kwargs, unknown, None, None)
            self.assertIsNone(unknown.hidden_params.response_cost)
            self.assertIsNone(kwargs.get("response_cost"))

    async def test_litellm_dependency_is_lazy_and_required_only_when_enabled(self):
        args = self.mcp.parse_args(["--output-dir", str(self.root / "plain")])
        with mock.patch.dict(sys.modules, {"litellm": None}):
            self.mcp.create_server(args)
            optional = self.mcp.create_server(self.mcp.parse_args([
                "--litellm-costs", "--output-dir", str(self.root / "costs"),
            ]))
            self.assertIsNotNone(optional)
            with self.assertRaises(RuntimeError):
                self.mcp._higgsfield_cost_tracker_class()

    def test_litellm_can_load_callback_source_without_mcp_sdk_import(self):
        import types

        module_name = "higgsfield_mcp_callback_without_mcp"
        spec = importlib.util.spec_from_file_location(module_name, SERVER_PATH)
        module = importlib.util.module_from_spec(spec)
        custom_logger = types.ModuleType("litellm.integrations.custom_logger")
        custom_logger.CustomLogger = type("CustomLogger", (), {})
        integrations = types.ModuleType("litellm.integrations")
        package = types.ModuleType("litellm")
        package.__path__ = []
        integrations.__path__ = []
        with mock.patch.dict(sys.modules, {
            "mcp.server.fastmcp": None,
            "litellm": package,
            "litellm.integrations": integrations,
            "litellm.integrations.custom_logger": custom_logger,
        }):
            spec.loader.exec_module(module)
            tracker = getattr(module, "higgsfield_cost_tracker")
        self.assertEqual(type(tracker).__name__, "HiggsfieldCostTracker")
        self.assertTrue(hasattr(tracker, "async_post_mcp_tool_call_hook"))

    async def test_bearer_middleware_denies_missing_or_wrong_and_accepts_correct_token(self):
        calls = []

        async def downstream(_scope, _receive, send):
            calls.append("called")
            await send({"type": "http.response.start", "status": 204, "headers": []})
            await send({"type": "http.response.body", "body": b""})

        app = self.mcp.BearerAuth(downstream, "synthetic-token")
        async def exercise(headers):
            events = []
            async def send(event):
                events.append(event)
            await app({"type": "http", "headers": headers}, None, send)
            return events

        denied = await exercise([])
        self.assertEqual(denied[0]["status"], 401)
        self.assertEqual(calls, [])
        wrong_token = await exercise([(b"authorization", b"Bearer synthetic-tokem")])
        self.assertEqual(wrong_token[0]["status"], 401)
        self.assertIn((b"www-authenticate", b"Bearer"), wrong_token[0]["headers"])
        self.assertEqual(json.loads(wrong_token[1]["body"]), {"error": "unauthorized"})
        self.assertEqual(calls, [])
        accepted = await exercise([(b"authorization", b"Bearer synthetic-token")])
        self.assertEqual(accepted[0]["status"], 204)
        self.assertEqual(calls, ["called"])

    async def test_http_body_limit_rejects_oversized_stream_before_mcp_dispatch(self):
        calls = []

        async def downstream(_scope, _receive, _send):
            calls.append("called")

        messages = iter([
            {"type": "http.request", "body": b"x" * 8, "more_body": True},
            {"type": "http.request", "body": b"x" * 4, "more_body": False},
        ])
        events = []
        async def receive():
            return next(messages)
        async def send(event):
            events.append(event)
        app = self.mcp.RequestBodyLimit(downstream, 10)
        await app({"type": "http", "headers": []}, receive, send)
        self.assertEqual(events[0]["status"], 413)
        self.assertEqual(calls, [])

    async def test_error_messages_and_credentials_are_redacted(self):
        credentials = self.mcp.cli.Credentials("synthetic-id", "synthetic-secret")
        error = self.mcp.cli.error_from_http(
            500, b"Rejected synthetic-secret and synthetic-id", credentials,
        )
        serialized = json.dumps(self.mcp.cli.build_error_result(error))
        self.assertNotIn("synthetic-secret", serialized)
        self.assertNotIn("synthetic-id", serialized)

    async def test_cli_public_contract_stays_available(self):
        parser = self.mcp.cli.make_parser()
        self.assertEqual(parser.parse_args(["image", "grok-image-2", "--prompt", "x"]).command, "image")
        result = self.mcp.cli.build_result(
            model="grok-image-2", workflow="generate",
            payload={"request_id": "synthetic", "status": "queued"},
        )
        self.assertTrue(result["ok"])
        self.assertIsNone(result["charged_cost"])


class OfflineNetworkPolicyTests(unittest.TestCase):
    def test_runner_blocks_external_dns_and_socket_operations(self):
        from test_offline_suite import UnexpectedNetworkAccess

        with network_blocked():
            with self.assertRaises(UnexpectedNetworkAccess):
                socket.getaddrinfo("api.higgsfield.ai", 443)
            sock = socket.socket()
            try:
                with self.assertRaises(UnexpectedNetworkAccess):
                    sock.connect(("203.0.113.10", 443))
                with self.assertRaises(UnexpectedNetworkAccess):
                    sock.connect(("127.0.0.1", 8765))
                with self.assertRaises(UnexpectedNetworkAccess):
                    sock.connect_ex(("203.0.113.10", 443))
                with self.assertRaises(UnexpectedNetworkAccess):
                    sock.sendto(b"blocked", ("203.0.113.10", 443))
            finally:
                sock.close()
            with self.assertRaises(UnexpectedNetworkAccess):
                socket.create_connection(("api.higgsfield.ai", 443))


if __name__ == "__main__":
    unittest.main(verbosity=2)
