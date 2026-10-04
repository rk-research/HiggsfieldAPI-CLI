#!/usr/bin/env python3
# Copyright (c) 2026 Richard Knuchel
# SPDX-License-Identifier: BSD-2-Clause
"""Standalone MCP server for the repository's official Higgsfield SDK CLI.

Workflow arguments and validation are derived from the supported CLI parser and
its business logic. The MCP layer does not make estimate requests unless the
caller invokes a workflow-specific ``higgsfield_estimate_*`` tool explicitly.
"""

from __future__ import annotations

import argparse
import asyncio
from contextlib import closing
import importlib.util
import inspect
import ipaddress
import json
import logging
import math
import os
import re
import socket
import sqlite3
import sys
import time
import typing
import urllib.request
from decimal import Decimal, InvalidOperation
from pathlib import Path, PureWindowsPath
from typing import Any, Literal

from urllib.parse import urlparse

LOG = logging.getLogger("higgsfield_mcp")
# Update this value alongside CLI_VERSION for each repository release.
MCP_VERSION = "v0.2.0"
BASE_DIR = Path(__file__).resolve().parent
CLI_PATH = BASE_DIR / "HiggsfieldAPI-CLI-sdk.py"
REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,200}$")
MAX_TOOL_TEXT = 1024 * 1024
MAX_MEDIA_FILE = 2 * 1024 * 1024 * 1024
IMAGE_MODELS = {"marketing-studio", "grok-image-2", "soul2", "ideogram4"}
VIDEO_MODELS = {"seedance-2", "seedance-2.5", "cinema-studio-4.0", "kling-3.0-standard"}
WORKFLOWS: tuple[tuple[str, str, str], ...] = (
    ("marketing-studio", "generate", "image"), ("grok-image-2", "generate", "image"),
    ("soul2", "generate", "image"), ("ideogram4", "generate", "image"),
    ("seedance-2", "text", "video"), ("seedance-2", "image", "video"),
    ("seedance-2", "reference", "video"), ("seedance-2.5", "text", "video"),
    ("seedance-2.5", "image", "video"), ("seedance-2.5", "reference", "video"),
    ("seedance-2.5", "edit", "video"), ("seedance-2.5", "extend", "video"),
    ("cinema-studio-4.0", "text", "video"), ("cinema-studio-4.0", "reference", "video"),
    ("kling-3.0-standard", "text", "video"), ("kling-3.0-standard", "image", "video"),
)


def _load_cli() -> Any:
    if not CLI_PATH.is_file():
        raise RuntimeError(f"Required CLI implementation is missing: {CLI_PATH.name}")
    spec = importlib.util.spec_from_file_location("higgsfield_cli_sdk", CLI_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("Could not load the Higgsfield CLI module")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


cli = _load_cli()


def _safe_remote_url(value: str, label: str = "media URL", *, resolve_dns: bool = False) -> str:
    try:
        parsed = urlparse(value)
        host = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise ValueError(f"Invalid {label}") from exc
    if (parsed.scheme != "https" or not host or parsed.username or parsed.password or
            parsed.fragment or port not in (None, 443)):
        raise ValueError(f"{label} must be an HTTPS URL without credentials or a nonstandard port")
    lowered = host.rstrip(".").lower()
    if lowered in {"localhost", "localhost.localdomain"} or lowered.endswith((".localhost", ".local", ".internal")):
        raise ValueError(f"Private or loopback {label} is not allowed")
    try:
        address = ipaddress.ip_address(lowered.strip("[]"))
    except ValueError:
        address = None
    if address is not None and (not address.is_global or address.is_multicast or address.is_unspecified):
        raise ValueError(f"Private or loopback {label} is not allowed")
    if address is None and resolve_dns:
        try:
            infos = socket.getaddrinfo(host, port or 443, type=socket.SOCK_STREAM)
        except OSError as exc:
            raise ValueError("Could not resolve output host") from exc
        if not infos or any(not ipaddress.ip_address(info[4][0].split("%", 1)[0]).is_global
                            for info in infos):
            raise ValueError("Output host resolves to a private or non-public address")
    return value


def _resolve_allowed(value: str | Path, roots: tuple[Path, ...], *, must_exist: bool = True) -> Path:
    candidate = Path(value).expanduser()
    try:
        resolved = candidate.resolve(strict=must_exist)
    except (OSError, RuntimeError) as exc:
        raise ValueError("Input path cannot be resolved") from exc
    if not any(resolved == root or root in resolved.parents for root in roots):
        raise ValueError("Local paths must be inside a configured input directory")
    if must_exist and (not resolved.is_file() or resolved.stat().st_size > MAX_MEDIA_FILE):
        raise ValueError("Input file is missing or exceeds the configured size limit")
    return resolved


class GuardedClient:
    """SDK adapter that applies the MCP filesystem and remote URL policy."""
    def __init__(self, client: Any, roots: tuple[Path, ...], output_dir: Path):
        self._client, self.roots, self.output_dir = client, roots, output_dir

    def upload_file(self, path: str | Path) -> str:
        safe = _resolve_allowed(path, self.roots)
        if safe.stat().st_size > self._client.max_input_file_size:
            raise ValueError("Input upload exceeds the configured size limit")
        if cli.infer_media_type(safe) not in cli.SUPPORTED_MEDIA_TYPES:
            raise ValueError("Unsupported local media type")
        return self._client.upload_file(safe)

    def close(self) -> None:
        # Avoid touching SDK lazy properties: close only already-created clients.
        sdk = getattr(self._client, "sdk", None)
        if sdk is None:
            return
        for name in ("_client", "_upload_client"):
            transport = vars(sdk).get(name)
            close = getattr(transport, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    LOG.debug("SDK client cleanup failed (%s)", name)

    def download_file(self, url: str, target: Path) -> None:
        _safe_remote_url(url, "output URL", resolve_dns=True)
        resolved_dir = target.parent.resolve()
        if resolved_dir != self.output_dir and self.output_dir not in resolved_dir.parents:
            raise ValueError("Output path is outside the configured output directory")
        # The CLI downloader follows redirects by default. Reject redirects so
        # an otherwise public URL cannot redirect the server into a private host.
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req: Any, fp: Any, code: int, msg: str,
                                 headers: Any, newurl: str) -> None:
                raise urllib.error.HTTPError(req.full_url, code, "redirects are disabled", headers, fp)
        opener = urllib.request.build_opener(NoRedirect)
        request = urllib.request.Request(url, method="GET")
        temporary: Path | None = None
        try:
            with opener.open(request, timeout=self._client.download_timeout) as response:
                raw_length = response.headers.get("Content-Length")
                expected = int(raw_length) if raw_length and raw_length.isdigit() else None
                if expected is not None and expected > self._client.max_download_size:
                    raise ValueError("Output exceeds the configured download size limit")
                with __import__("tempfile").NamedTemporaryFile(
                        mode="wb", dir=target.parent, prefix=f".{target.name}.",
                        suffix=".part", delete=False) as stream:
                    temporary = Path(stream.name)
                    total = 0
                    while True:
                        chunk = response.read(1024 * 1024)
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > self._client.max_download_size:
                            raise ValueError("Output exceeds the configured download size limit")
                        stream.write(chunk)
                if expected is not None and expected != total:
                    raise ValueError("Downloaded output was truncated")
            os.replace(temporary, target)
            temporary = None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def __getattr__(self, key: str) -> Any:
        return getattr(self._client, key)


def _get_client(args: argparse.Namespace, roots: tuple[Path, ...], output_dir: Path) -> GuardedClient:
    raw = cli.make_client(args)
    # Never let SDK retries repeat a POST that may already have spent credits.
    try:
        import higgsfield_client
        from higgsfield_client.http.retry import NoRetry
        transport = getattr(raw.sdk, "_transport", None)
        if transport is None or not hasattr(transport, "_retry_strategy"):
            raise RuntimeError("Installed Higgsfield SDK does not expose the safe no-retry transport")
        transport._retry_strategy = NoRetry()
    except (ImportError, AttributeError) as exc:
        raise RuntimeError("Installed Higgsfield SDK lacks the required no-retry support") from exc
    raw.max_download_size = min(raw.max_download_size, MAX_MEDIA_FILE) if raw.max_download_size else MAX_MEDIA_FILE
    raw.max_input_file_size = min(args.max_input_file_size, MAX_MEDIA_FILE)
    return GuardedClient(raw, roots, output_dir)


def _validate_media_inputs(args: argparse.Namespace, roots: tuple[Path, ...]) -> None:
    path_names = ("image", "end_image", "video", "image_ref", "video_ref", "audio_ref", "prompt_file")
    for name in path_names:
        value = getattr(args, name, None)
        values = value if isinstance(value, list) else ([value] if value else [])
        for item in values:
            if not isinstance(item, str):
                continue
            if name == "prompt_file":
                if item == "-":
                    raise ValueError("prompt_file cannot read MCP server stdin")
                safe_path = _resolve_allowed(item, roots)
                if safe_path.stat().st_size > args.max_input_file_size:
                    raise ValueError("Prompt file exceeds the configured size limit")
                continue
            if item.startswith("https://"):
                _safe_remote_url(item)
            elif item.startswith(("http://", "ftp://")):
                raise ValueError("Media URLs must use public HTTPS")
            else:
                _resolve_allowed(item, roots)


def _parser_leaf_lookup() -> dict[tuple[str, str], argparse.ArgumentParser]:
    parser = cli.make_parser()
    command_parsers = {a.dest: a.choices for a in parser._actions if isinstance(a, argparse._SubParsersAction)}
    result: dict[tuple[str, str], argparse.ArgumentParser] = {}
    for model, workflow, kind in WORKFLOWS:
        if kind == "image":
            sub = command_parsers["command"]["image"]
            m = next(a for a in sub._actions if isinstance(a, argparse._SubParsersAction))
            result[(model, workflow)] = m.choices[model]
        else:
            sub = command_parsers["command"]["video"]
            m = next(a for a in sub._actions if isinstance(a, argparse._SubParsersAction))
            model_parser = m.choices[model]
            wf = next(a for a in model_parser._actions if isinstance(a, argparse._SubParsersAction))
            result[(model, workflow)] = wf.choices[workflow]
    return result


LEAF_PARSERS = _parser_leaf_lookup()
COMMON_DESTS = {
    "env_file", "json", "no_wait", "timeout", "http_timeout", "download_timeout",
    "max_download_size", "max_input_file_size", "poll_interval", "output_dir", "no_download",
    "overwrite", "estimate_only", "params_json", "debug",
}


def _action_type(action: argparse.Action) -> Any:
    if isinstance(action, (argparse._StoreTrueAction, argparse._StoreFalseAction, argparse.BooleanOptionalAction)):
        return bool
    if isinstance(action, argparse._AppendAction):
        return list[str]
    if action.choices:
        return Literal.__getitem__(tuple(action.choices))
    return action.type or str


def _workflow_schema(model: str, workflow: str) -> tuple[list[inspect.Parameter], dict[str, Any]]:
    from pydantic import Field
    parser = LEAF_PARSERS[(model, workflow)]
    parameters: list[inspect.Parameter] = []
    defaults: dict[str, Any] = {}
    for action in parser._actions:
        if action.dest in {"help", "command", "image_model", "video_model", "workflow"} or action.dest in COMMON_DESTS:
            continue
        annotation = _action_type(action)
        default = action.default
        is_required = bool(action.required)
        if is_required:
            default = inspect.Parameter.empty
        else:
            if default is None:
                annotation = typing.Optional[annotation]
            # argparse.SUPPRESS is not a tool argument default.
            if default is argparse.SUPPRESS:
                default = None
            defaults[action.dest] = default
        if action.help:
            annotation = typing.Annotated[annotation, Field(description=action.help)]
        parameters.append(inspect.Parameter(action.dest, inspect.Parameter.KEYWORD_ONLY,
                                            default=default, annotation=annotation))
    return parameters, defaults


def _validate_action_values(model: str, workflow: str, values: dict[str, Any]) -> None:
    parser = LEAF_PARSERS[(model, workflow)]
    for action in parser._actions:
        if action.dest not in values or values[action.dest] is None:
            continue
        value = values[action.dest]
        for item in value if isinstance(value, list) else [value]:
            if isinstance(item, str) and len(item.encode("utf-8")) > MAX_TOOL_TEXT:
                raise ValueError(f"{action.dest} exceeds the 1 MiB input limit")
        if action.choices:
            for item in value if isinstance(value, list) else [value]:
                if item not in action.choices:
                    raise ValueError(f"{action.dest} must be one of: {', '.join(map(str, action.choices))}")
        if action.type and not isinstance(value, list) and type(value) not in (action.type,):
            try:
                values[action.dest] = action.type(value)
            except (ValueError, TypeError) as exc:
                raise ValueError(f"Invalid value for {action.dest}") from exc
        if isinstance(action, argparse._AppendAction) and not isinstance(value, list):
            raise ValueError(f"{action.dest} must be a list")


class ServerState:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.output_dir = Path(args.output_dir).expanduser().resolve()
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.roots = tuple(Path(p).expanduser().resolve() for p in args.input_dir)
        self.litellm_enabled = args.litellm_costs
        self.lite_hook_enabled = False

    def _mcp_result(self, result: dict[str, Any]) -> dict[str, Any]:
        result.setdefault("estimated_cost", None)
        result.setdefault("charged_cost", None)
        result["cost"] = _cost_metadata(result)
        result["litellm_costs"] = bool(self.litellm_enabled)
        return result

    def submit(self, model: str, workflow: str, raw_values: dict[str, Any],
               advanced_params: dict[str, Any] | None = None) -> dict[str, Any]:
        _validate_action_values(model, workflow, raw_values)
        values, _ = _workflow_schema(model, workflow)
        defaults = {p.name: p.default for p in values if p.default is not inspect.Parameter.empty}
        supplied = {**defaults, **raw_values}
        ns = argparse.Namespace(**supplied, env_file=self.args.env_file, json=True, no_wait=True,
            timeout=self.args.operation_timeout, http_timeout=self.args.http_timeout,
            download_timeout=self.args.download_timeout, max_download_size=self.args.max_download_size,
            max_input_file_size=self.args.max_input_file_size, poll_interval=self.args.poll_interval,
            output_dir=str(self.output_dir), no_download=True, overwrite=False, estimate_only=False,
            params_json=None, debug=False)
        _validate_media_inputs(ns, self.roots)
        try:
            client = _get_client(ns, self.roots, self.output_dir)
            if model in IMAGE_MODELS:
                params = cli.image_params(ns, model, client)
                endpoint = cli.endpoint_for(model, "generate")
            else:
                version = {"seedance-2": "2.0", "seedance-2.5": "2.5",
                           "cinema-studio-4.0": "cinema4", "kling-3.0-standard": "kling3"}[model]
                params = cli.video_params(ns, version, workflow, client)
                endpoint = cli.endpoint_for(model, workflow)
            params.update(_validated_advanced_params(advanced_params, params))
            payload, _ = client.json_request("POST", endpoint, params)
            if not isinstance(payload, dict) or not payload.get("request_id"):
                raise ValueError("Higgsfield returned an invalid submission response")
            _safe_request_id(str(payload["request_id"]))
            result = {"ok": True, "model": model, "workflow": workflow,
                      "status": payload.get("status", "queued"), "request_id": payload["request_id"],
                      "estimated_cost": None, "charged_cost": None,
                      "remaining_credits": None, "async": True}
            for key in ("status_url", "cancel_url"):
                if payload.get(key):
                    result[key] = payload[key]
            return self._mcp_result(result)
        except cli.AppError as exc:
            return cli.build_error_result(exc)
        except ValueError as exc:
            return {"ok": False, "error": {"type": "validation", "message": str(exc)}}
        finally:
            if "client" in locals():
                client.close()

    def status(self, request_id: str, wait: bool = False, download: bool = False) -> dict[str, Any]:
        _safe_request_id(request_id)
        ns = self._base_args()
        try:
            client = _get_client(ns, self.roots, self.output_dir)
            current = cli.fetch_status(client, request_id)
            if wait and current.get("status") not in cli.TERMINAL_STATUSES:
                current = cli.poll_request(client, current, timeout=self.args.operation_timeout,
                    interval=self.args.poll_interval, json_mode=True, watch=True)
            status = current.get("status")
            result: dict[str, Any] = {"ok": True, **cli.request_metadata(current)}
            result.pop("error", None)
            result["estimated_cost"] = None
            result["charged_cost"] = current.get("charged_cost") if status in cli.TERMINAL_STATUSES else None
            outputs = cli.output_items(current)
            if download and status == "completed" and outputs:
                outputs = cli.download_outputs(client, outputs, "request", "status", request_id,
                    str(self.output_dir), False)
                for output in outputs:
                    output["file_server_local"] = True
            result["outputs"] = outputs
            if download:
                result["server_local_paths"] = [item["file"] for item in outputs if item.get("file")]
            if status in {"failed", "nsfw", "canceled"}:
                error = cli.classify_terminal(current)
                if error:
                    result["ok"] = False
                    safe_message = cli.redact(error.message, getattr(client, "credentials", None))
                    result["error"] = {"type": error.kind, "message": safe_message}
            result = self._mcp_result(result)
            return result
        except cli.AppError as exc:
            return cli.build_error_result(exc)
        finally:
            if "client" in locals():
                client.close()

    def _base_args(self) -> argparse.Namespace:
        return argparse.Namespace(env_file=self.args.env_file, http_timeout=self.args.http_timeout,
            download_timeout=self.args.download_timeout, max_download_size=self.args.max_download_size,
            max_input_file_size=self.args.max_input_file_size, timeout=self.args.operation_timeout,
            poll_interval=self.args.poll_interval, json=True, no_download=True, overwrite=False,
            output_dir=str(self.output_dir), watch=False)

    def cancel(self, request_id: str) -> dict[str, Any]:
        _safe_request_id(request_id)
        try:
            client = _get_client(self._base_args(), self.roots, self.output_dir)
            client.request("POST", f"/requests/{request_id}/cancel")
            return self._mcp_result({"ok": True, "request_id": request_id,
                                     "status": "canceled", "remaining_credits": None})
        except cli.AppError as exc:
            return cli.build_error_result(exc)
        finally:
            if "client" in locals():
                client.close()

    def estimate(self, model: str, workflow: str, raw_values: dict[str, Any],
                 advanced_params: dict[str, Any] | None = None) -> dict[str, Any]:
        _validate_action_values(model, workflow, raw_values)
        schema, _ = _workflow_schema(model, workflow)
        defaults = {p.name: p.default for p in schema if p.default is not inspect.Parameter.empty}
        ns = argparse.Namespace(**{**defaults, **raw_values}, env_file=self.args.env_file, json=True, no_wait=True,
            timeout=self.args.operation_timeout, http_timeout=self.args.http_timeout,
            download_timeout=self.args.download_timeout, max_download_size=self.args.max_download_size,
            max_input_file_size=self.args.max_input_file_size, poll_interval=self.args.poll_interval,
            output_dir=str(self.output_dir), no_download=True, overwrite=False, estimate_only=True,
            params_json=None, debug=False)
        _validate_media_inputs(ns, self.roots)
        try:
            client = _get_client(ns, self.roots, self.output_dir)
            if model in IMAGE_MODELS:
                params = cli.image_params(ns, model, client)
                endpoint = cli.endpoint_for(model, "generate")
            else:
                version = {"seedance-2": "2.0", "seedance-2.5": "2.5",
                           "cinema-studio-4.0": "cinema4", "kling-3.0-standard": "kling3"}[model]
                params = cli.video_params(ns, version, workflow, client)
                endpoint = cli.endpoint_for(model, workflow)
            params.update(_validated_advanced_params(advanced_params, params))
            estimate = cli.estimate_cost(client, endpoint, params)
            return {"ok": True, "model": model, "workflow": workflow,
                    "status": "estimate_only", "estimate_unavailable": estimate is None,
                    "estimated_cost": estimate, "charged_cost": None,
                    "cost": {"actual_cost": None, "currency": None, "source": None,
                             "confirmed": False, "billable": False, "accounted": None}}
        except cli.EstimateUnavailable as exc:
            return {"ok": False, "estimate_unavailable": True,
                    "error": {"type": exc.kind, "message": exc.message}, "estimated_cost": None,
                    "charged_cost": None}
        except cli.AppError as exc:
            return cli.build_error_result(exc)
        finally:
            if "client" in locals():
                client.close()

def _safe_request_id(value: str) -> str:
    if not isinstance(value, str) or not REQUEST_ID_RE.fullmatch(value):
        raise ValueError("request_id has invalid characters or length")
    return value


def _validated_advanced_params(value: dict[str, Any] | None,
                               typed_params: dict[str, Any]) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("advanced_params must be an object")
    try:
        rendered = json.dumps(value, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError("advanced_params must contain JSON-safe values") from exc
    if len(rendered.encode("utf-8")) > MAX_TOOL_TEXT:
        raise ValueError("advanced_params exceeds the 1 MiB limit")
    typed_field_names = {
        action.dest for parser in LEAF_PARSERS.values() for action in parser._actions
        if action.dest not in {"help", "command", "image_model", "video_model", "workflow"}
    }
    typed_field_names.update({"image_url", "image_urls", "last_image_url", "end_image_url",
                              "video_url", "video_urls", "audio_urls", "sound"})
    def inspect_node(node: Any, depth: int = 0) -> None:
        if depth > 12:
            raise ValueError("advanced_params nesting exceeds 12 levels")
        if isinstance(node, dict):
            for key, child in node.items():
                if (not isinstance(key, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", key)
                        or re.search(r"credential|api.?key|secret|token|authorization|password", key, re.I)):
                    raise ValueError("advanced_params contains an invalid or sensitive field name")
                inspect_node(child, depth + 1)
        elif isinstance(node, list):
            for child in node:
                inspect_node(child, depth + 1)
        elif isinstance(node, str):
            if node.startswith("https://"):
                _safe_remote_url(node)
            elif re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", node):
                raise ValueError("advanced_params URLs must use public HTTPS")
            elif Path(node).is_absolute() or PureWindowsPath(node).is_absolute():
                raise ValueError("advanced_params cannot contain local filesystem paths")
            elif len(node) > MAX_TOOL_TEXT:
                raise ValueError("advanced_params text value is too large")
        elif node is not None and not isinstance(node, (bool, int, float)):
            raise ValueError("advanced_params contains an unsupported value")
    inspect_node(value)
    duplicated = set(value).intersection(typed_field_names)
    if duplicated:
        raise ValueError("Use validated typed arguments for model fields: " + ", ".join(sorted(duplicated)))
    return value


def _authoritative_charge(payload: dict[str, Any]) -> Any:
    raw = payload.get("charged_cost")
    if raw is None:
        return None
    if isinstance(raw, (int, float, str)) and not isinstance(raw, bool):
        try:
            amount = Decimal(str(raw))
        except InvalidOperation:
            return None
        if not amount.is_finite() or amount < 0:
            return None
        return {"amount": str(amount), "currency": None, "source": "higgsfield.charged_cost"}
    if isinstance(raw, dict):
        credits = raw.get("credits")
        amount = raw.get("usd", raw.get("amount", raw.get("value")))
        currency = "USD" if raw.get("usd") is not None else raw.get("currency")
        if amount is None and credits is not None:
            try:
                credit_amount = Decimal(str(credits))
            except (InvalidOperation, TypeError):
                return None
            if credit_amount.is_finite() and credit_amount >= 0:
                return {"amount": None, "currency": None, "credits": str(credit_amount),
                        "source": "higgsfield.charged_cost"}
        try:
            decimal_amount = Decimal(str(amount))
        except (InvalidOperation, TypeError):
            decimal_amount = None
        if decimal_amount is None or not decimal_amount.is_finite() or decimal_amount < 0:
            return None
        return {"amount": str(decimal_amount), "currency": currency,
                "credits": str(credits) if credits is not None else None,
                "source": "higgsfield.charged_cost"}
    return None


def _cost_metadata(result: dict[str, Any]) -> dict[str, Any]:
    rid = result.get("request_id")
    status = result.get("status")
    raw = result.get("charged_cost")
    parsed = _authoritative_charge({"charged_cost": raw}) if status in {"completed", "failed", "nsfw", "canceled"} else None
    amount = parsed.get("amount") if parsed else None
    currency = parsed.get("currency") if parsed else None
    billable = bool(parsed and currency == "USD")
    return {"request_id": rid, "status": status, "actual_cost": amount,
            "currency": currency, "credits": parsed.get("credits") if parsed else None,
            "source": parsed.get("source") if parsed else None,
            "confirmed": bool(parsed), "billable": billable, "accounted": None}


def _install_litellm_hook(state: ServerState) -> None:
    """Enable LiteLLM-compatible metadata without requiring LiteLLM in server."""
    state.lite_hook_enabled = True


def _mcp_response_data(response_obj: Any) -> dict[str, Any] | None:
    """Extract one JSON tool result from LiteLLM's MCP response blocks."""
    value = getattr(response_obj, "mcp_tool_call_response", None)
    if value is None and isinstance(response_obj, dict):
        value = response_obj.get("mcp_tool_call_response")
    if isinstance(value, dict):
        return value
    if isinstance(value, list):
        for item in value:
            block_text = item.get("text") if isinstance(item, dict) else getattr(item, "text", None)
            if isinstance(block_text, str):
                try:
                    parsed = json.loads(block_text)
                except json.JSONDecodeError:
                    continue
                if isinstance(parsed, dict):
                    return parsed
    return None


def _get_nested(source: Any, *path: str) -> Any:
    value = source
    for part in path:
        if isinstance(value, dict):
            value = value.get(part)
        else:
            value = getattr(value, part, None)
        if value is None:
            return None
    return value


def _key_attribution(kwargs: dict[str, Any]) -> str | None:
    metadata = _get_nested(kwargs, "litellm_params", "metadata") or {}
    # Prefer the stable hashed identifier; raw API keys must never be persisted.
    hashed = metadata.get("user_api_key_hash") if isinstance(metadata, dict) else None
    if hashed:
        return str(hashed)
    key = metadata.get("user_api_key") if isinstance(metadata, dict) else None
    if key:
        import hashlib
        return hashlib.sha256(str(key).encode()).hexdigest()
    for alt in (kwargs.get("litellm_metadata"), kwargs.get("metadata")):
        if isinstance(alt, dict):
            hashed = alt.get("user_api_key_hash")
            if hashed:
                return str(hashed)
    return None


def _tool_identity(kwargs: dict[str, Any]) -> tuple[str | None, str | None]:
    metadata = kwargs.get("mcp_tool_call_metadata") or {}
    if not isinstance(metadata, dict):
        metadata = {}
    server = metadata.get("mcp_server_name") or kwargs.get("mcp_server_name")
    tool = metadata.get("name") or metadata.get("tool_name") or kwargs.get("mcp_tool_name")
    return (str(server) if server else None, str(tool) if tool else None)


def _tool_arguments(kwargs: dict[str, Any]) -> dict[str, Any]:
    for key in ("mcp_tool_call_arguments", "tool_arguments", "arguments"):
        value = kwargs.get(key)
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except json.JSONDecodeError:
                value = None
        if isinstance(value, dict):
            return value
    return {}


def _accounting_db_path() -> Path:
    raw = os.environ.get("HF_MCP_ACCOUNTING_DB")
    path = Path(raw).expanduser() if raw else BASE_DIR / "outputs" / "mcp" / "higgsfield-mcp-accounting.sqlite3"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path.resolve()


def _ledger_record(server: str, request_id: str, key_hash: str) -> None:
    path = _accounting_db_path()
    with closing(sqlite3.connect(path, timeout=15)) as db:
        with db:
            db.execute("CREATE TABLE IF NOT EXISTS operations (server TEXT NOT NULL, request_id TEXT NOT NULL, origin_key TEXT NOT NULL, accounted INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(server, request_id))")
            db.execute("INSERT OR IGNORE INTO operations(server, request_id, origin_key) VALUES (?, ?, ?)",
                       (server, request_id, key_hash))


def _ledger_claim(server: str, request_id: str, key_hash: str) -> bool:
    path = _accounting_db_path()
    with closing(sqlite3.connect(path, timeout=15, isolation_level="IMMEDIATE")) as db:
        with db:
            db.execute("CREATE TABLE IF NOT EXISTS operations (server TEXT NOT NULL, request_id TEXT NOT NULL, origin_key TEXT NOT NULL, accounted INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(server, request_id))")
            cur = db.execute("UPDATE operations SET accounted=1 WHERE server=? AND request_id=? AND origin_key=? AND accounted=0",
                             (server, request_id, key_hash))
            return cur.rowcount == 1


def _ledger_matches(server: str, request_id: str, key_hash: str) -> bool:
    path = _accounting_db_path()
    with closing(sqlite3.connect(path, timeout=15)) as db:
        db.execute("CREATE TABLE IF NOT EXISTS operations (server TEXT NOT NULL, request_id TEXT NOT NULL, origin_key TEXT NOT NULL, accounted INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(server, request_id))")
        row = db.execute("SELECT 1 FROM operations WHERE server=? AND request_id=? AND origin_key=? AND accounted=1",
                         (server, request_id, key_hash)).fetchone()
        return row is not None


def _litellm_post_tool_hook_sync(kwargs: dict[str, Any], response_obj: Any) -> Any:
    """LiteLLM ``async_post_mcp_tool_call_hook`` implementation.

    Persist originating key attribution on submission, then claim a completed
    confirmed USD charge once. SQLite gives concurrent hook processes an
    at-most-once claim; a process crash after claim and before LiteLLM commits
    can lose a charge, so this is not exactly-once accounting.
    """
    try:
        from decimal import Decimal
        server, tool_name = _tool_identity(kwargs)
        if server != os.environ.get("HF_MCP_LITELLM_SERVER", "higgsfield"):
            return response_obj
        data = _mcp_response_data(response_obj) or {}
        if data.get("litellm_costs") is not True:
            return response_obj
        hidden = getattr(response_obj, "hidden_params", None)
        if hidden is not None:
            if isinstance(hidden, dict):
                hidden["response_cost"] = None
            else:
                hidden.response_cost = None
        key_hash = _key_attribution(kwargs)
        arguments = _tool_arguments(kwargs)
        request_id = data.get("request_id") or arguments.get("request_id")
        if not request_id or not REQUEST_ID_RE.fullmatch(str(request_id)):
            return response_obj
        request_id = str(request_id)
        generation_tools = {
            f"higgsfield_{re.sub('[^a-z0-9]+', '_', model + '_' + workflow).strip('_')}"
            for model, workflow, _ in WORKFLOWS
        }
        if tool_name in generation_tools and key_hash:
            _ledger_record(server, request_id, key_hash)
        cost = data.get("cost") if isinstance(data.get("cost"), dict) else {}
        if (key_hash and data.get("status") == "completed" and cost.get("confirmed") is True
                and cost.get("currency") == "USD" and cost.get("billable") is True):
            amount = Decimal(str(cost.get("actual_cost")))
            numeric_amount = float(amount) if amount.is_finite() else float("inf")
            if amount.is_finite() and amount >= 0 and math.isfinite(numeric_amount):
                if _ledger_claim(server, request_id, key_hash):
                    if hidden is not None:
                        if isinstance(hidden, dict):
                            hidden["response_cost"] = numeric_amount
                        else:
                            hidden.response_cost = numeric_amount
                    # LiteLLM consumes hidden response_cost from response_obj.
                elif _ledger_matches(server, request_id, key_hash):
                    # Zero is used only after a previous successful claim, to
                    # suppress a second fallback charge for the same request.
                    if hidden is not None:
                        if isinstance(hidden, dict):
                            hidden["response_cost"] = 0
                        else:
                            hidden.response_cost = 0
    except Exception as exc:
        # Accounting failures must not prevent MCP tools from returning results.
        LOG.error("LiteLLM cost hook failed (%s)", type(exc).__name__)
    return response_obj


async def _litellm_post_tool_hook(kwargs: dict[str, Any], response_obj: Any,
                                  start_time: Any, end_time: Any) -> Any:
    return await asyncio.to_thread(_litellm_post_tool_hook_sync, kwargs, response_obj)


def _higgsfield_cost_tracker_class() -> Any:
    """Return a LiteLLM CustomLogger instance, importing LiteLLM lazily."""
    try:
        from litellm.integrations.custom_logger import CustomLogger
    except ImportError as exc:
        raise RuntimeError("The Higgsfield LiteLLM tracker requires LiteLLM") from exc

    class HiggsfieldCostTracker(CustomLogger):
        async def async_post_mcp_tool_call_hook(self, kwargs: dict[str, Any], response_obj: Any,
                                                start_time: Any, end_time: Any) -> Any:
            return await _litellm_post_tool_hook(kwargs, response_obj, start_time, end_time)

    return HiggsfieldCostTracker()


def create_higgsfield_cost_tracker() -> Any:
    """Create the optional LiteLLM callback object from this source file."""
    return _higgsfield_cost_tracker_class()


def __getattr__(name: str) -> Any:
    # LiteLLM imports custom callbacks by module attribute. The optional package
    # remains unloaded for regular MCP server use.
    if name in {"higgsfield_cost_tracker", "HiggsfieldCostTracker"}:
        return _higgsfield_cost_tracker_class()
    raise AttributeError(name)


def create_server(args: argparse.Namespace) -> Any:
    from mcp.server.fastmcp import FastMCP
    from mcp.server.transport_security import TransportSecuritySettings
    from mcp.types import CallToolResult, TextContent
    state = ServerState(args)
    if args.litellm_costs:
        _install_litellm_hook(state)
    transport_security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=args.allowed_host or [f"{args.host}:{args.port}",
            f"localhost:{args.port}", f"127.0.0.1:{args.port}", f"[::1]:{args.port}",
            f"host.docker.internal:{args.port}"],
        allowed_origins=args.allowed_origin,
    )
    # Generation state belongs to the provider request_id, not an HTTP
    # session. Gateways reconnect between tool calls and may send late
    # notifications after closing a session. Avoid reusing closed transports
    # and the SDK's notification-after-202 double-response error path.
    mcp = FastMCP("higgsfield-api", instructions="Higgsfield image and video generation. Paid generation tools submit asynchronously; check request status to retrieve outputs and confirmed charges.",
                  transport_security=transport_security, stateless_http=True, json_response=True)

    async def run_tool(func: Any, *call_args: Any) -> Any:
        try:
            result = await asyncio.to_thread(func, *call_args)
        except cli.AppError as exc:
            result = cli.build_error_result(exc)
        except ValueError as exc:
            result = {"ok": False, "error": {"type": "validation", "message": str(exc)}}
        except Exception as exc:
            LOG.error("MCP tool failed (%s)", type(exc).__name__)
            result = {"ok": False, "error": {"type": "internal", "message": "Higgsfield MCP operation failed."}}
        if isinstance(result, dict) and result.get("ok") is False:
            return CallToolResult(content=[TextContent(type="text", text=json.dumps(result, ensure_ascii=False))],
                                  structuredContent=result, isError=True)
        return result

    def register_generation(model: str, workflow: str, estimate: bool = False) -> None:
        params, _ = _workflow_schema(model, workflow)
        # FastMCP reads inspect.signature when deriving each tool's JSON schema.
        async def handler(**kwargs: Any) -> dict[str, Any]:
            call = state.estimate if estimate else state.submit
            advanced = kwargs.pop("advanced_params", None)
            return await run_tool(call, model, workflow, kwargs, advanced)
        prefix = "estimate" if estimate else "generate"
        handler.__name__ = f"{prefix}_{re.sub('[^a-z0-9]+', '_', model + '_' + workflow).strip('_')}"
        handler.__doc__ = (f"Estimate {model} {workflow} costs without submitting generation." if estimate
                           else f"Submit {model} {workflow} generation asynchronously. Returns a Higgsfield request_id.")
        handler.__signature__ = inspect.Signature(parameters=params, return_annotation=dict[str, Any])
        adv_type = typing.Optional[dict[str, Any]]
        adv = inspect.Parameter("advanced_params", inspect.Parameter.KEYWORD_ONLY,
                                default=None, annotation=adv_type)
        handler.__signature__ = inspect.Signature(parameters=[*params, adv], return_annotation=dict[str, Any])
        handler.__annotations__ = {p.name: p.annotation for p in [*params, adv]} | {"return": dict[str, Any]}
        suffix = handler.__name__[len(prefix) + 1:]
        tool_name = f"higgsfield_estimate_{suffix}" if estimate else f"higgsfield_{suffix}"
        mcp.tool(name=tool_name)(handler)

    for model, workflow, _kind in WORKFLOWS:
        register_generation(model, workflow)

    async def get_status(request_id: str, wait: bool = False, download: bool = False) -> dict[str, Any]:
        """Retrieve status. Waiting returns a successful progress result within wait-call-timeout; repeat with the same request_id while wait_complete is false. Never resubmit."""
        if not wait:
            return await run_tool(state.status, request_id, False, download)
        _safe_request_id(request_id)
        deadline = time.monotonic() + min(args.operation_timeout, args.wait_call_timeout)
        delay = max(0.1, args.poll_interval)
        current = {"ok": True, "request_id": request_id, "status": None, "outputs": []}

        def progress_result() -> dict[str, Any]:
            return {**current, "wait_complete": False,
                    "next_action": "Call higgsfield_wait again with the same request_id. Do not resubmit generation."}

        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return progress_result()
            try:
                current = await asyncio.wait_for(
                    run_tool(state.status, request_id, False, False), timeout=remaining)
            except asyncio.TimeoutError:
                return progress_result()
            if not isinstance(current, dict) or current.get("ok") is False:
                return current
            if current.get("status") in cli.TERMINAL_STATUSES:
                if download and current.get("status") == "completed":
                    # Downloads have their own potentially long timeout; keep
                    # gateway-facing waits bounded and retrieve files separately.
                    return {**current, "wait_complete": True, "download_pending": True,
                            "next_action": "Call higgsfield_result with this request_id to download outputs."}
                return {**current, "wait_complete": True}
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return progress_result()
            await asyncio.sleep(min(delay, remaining))
            delay = min(delay * 1.5, 10.0)
    mcp.tool(name="higgsfield_status")(get_status)

    async def wait_generation(request_id: str, download: bool = False) -> dict[str, Any]:
        """Wait briefly for generation. If wait_complete is false, call again with the same request_id; never resubmit. Downloads requested here are deferred to higgsfield_result."""
        return await get_status(request_id, True, download)
    mcp.tool(name="higgsfield_wait")(wait_generation)

    async def cancel_generation(request_id: str) -> dict[str, Any]:
        """Cancel a queued Higgsfield generation request."""
        return await run_tool(state.cancel, request_id)
    mcp.tool(name="higgsfield_cancel")(cancel_generation)

    async def list_presets(search: str | None = None, size: int = 50, cursor: str | None = None,
                           all_pages: bool = False) -> dict[str, Any]:
        """List Marketing Studio presets with optional local search and pagination."""
        def run() -> dict[str, Any]:
            ns = state._base_args()
            if size < 1 or size > 1000:
                return {"ok": False, "error": {"type": "validation", "message": "size must be 1..1000"}}
            client = _get_client(ns, state.roots, state.output_dir)
            try:
                ns.search, ns.size, ns.cursor, ns.all = search, size, cursor, all_pages
                return cli.run_presets(ns, client=client)
            except cli.AppError as exc:
                return cli.build_error_result(exc)
            finally:
                client.close()
        return await run_tool(run)
    mcp.tool(name="higgsfield_presets")(list_presets)

    async def credits() -> dict[str, Any]:
        """Report whether the public Higgsfield API supports account balance lookup."""
        return cli.run_credits(argparse.Namespace())
    mcp.tool(name="higgsfield_credits")(credits)

    async def retrieve_result(request_id: str, download: bool = False) -> dict[str, Any]:
        """Retrieve a terminal generation result and optionally download media."""
        return await run_tool(state.status, request_id, False, download)
    mcp.tool(name="higgsfield_result")(retrieve_result)

    async def upload_media(path: str) -> dict[str, Any]:
        """Upload one allowlisted local media file and return its provider URL."""
        def run() -> dict[str, Any]:
            safe = _resolve_allowed(path, state.roots)
            if safe.stat().st_size > args.max_input_file_size:
                return {"ok": False, "error": {"type": "validation", "message": "Upload exceeds configured input size limit"}}
            if cli.infer_media_type(safe) not in cli.SUPPORTED_MEDIA_TYPES:
                return {"ok": False, "error": {"type": "validation", "message": "Unsupported local media type"}}
            client = _get_client(state._base_args(), state.roots, state.output_dir)
            try:
                url = client.upload_file(safe)
                return {"ok": True, "url": url, "file_server_local": True}
            except cli.AppError as exc:
                return cli.build_error_result(exc)
            finally:
                client.close()
        return await run_tool(run)
    mcp.tool(name="higgsfield_upload")(upload_media)

    for model, workflow, _kind in WORKFLOWS:
        register_generation(model, workflow, estimate=True)
    tool_manager = getattr(mcp, "_tool_manager", None)
    registered_tools = getattr(tool_manager, "_tools", {}) if tool_manager is not None else {}
    for tool in registered_tools.values():
        metadata = getattr(tool, "fn_metadata", None)
        arg_model = getattr(metadata, "arg_model", None)
        if arg_model is None:
            continue
        arg_model.model_config["extra"] = "forbid"
        arg_model.model_rebuild(force=True)
        try:
            tool.parameters = arg_model.model_json_schema()
        except (AttributeError, TypeError, ValueError):
            LOG.debug("Could not refresh strict MCP schema for tool %s", getattr(tool, "name", "unknown"))
    return mcp


class BearerAuth:
    """Minimal ASGI bearer middleware for Streamable HTTP deployments."""
    def __init__(self, app: Any, token: str):
        self.app, self.expected = app, token.encode("utf-8")

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers", []))
        value = headers.get(b"authorization", b"")
        expected = b"Bearer " + self.expected
        if not __import__("hmac").compare_digest(value, expected):
            body = b'{"error":"unauthorized"}'
            await send({"type": "http.response.start", "status": 401,
                        "headers": [(b"content-type", b"application/json"),
                                    (b"www-authenticate", b"Bearer"),
                                    (b"content-length", str(len(body)).encode())]})
            await send({"type": "http.response.body", "body": body})
            return
        await self.app(scope, receive, send)


class RequestBodyLimit:
    """Bound streamed HTTP request bodies before FastMCP parses JSON."""
    def __init__(self, app: Any, max_bytes: int):
        self.app, self.max_bytes = app, max_bytes

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        chunks: list[dict[str, Any]] = []
        size = 0
        while True:
            message = await receive()
            if message["type"] != "http.request":
                if message["type"] == "http.disconnect":
                    return
                continue
            body = message.get("body", b"")
            size += len(body)
            if size > self.max_bytes:
                content = b'{"error":"request body too large"}'
                await send({"type": "http.response.start", "status": 413,
                            "headers": [(b"content-type", b"application/json"),
                                        (b"content-length", str(len(content)).encode())]})
                await send({"type": "http.response.body", "body": content})
                return
            chunks.append(message)
            if not message.get("more_body", False):
                break
        cursor = 0
        async def replay() -> dict[str, Any]:
            nonlocal cursor
            if cursor < len(chunks):
                item = chunks[cursor]
                cursor += 1
                return item
            return await receive()
        await self.app(scope, replay, send)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", action="version", version="%(prog)s " + MCP_VERSION)
    parser.add_argument("--transport", choices=("http", "stdio"), default="http")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--endpoint", default="/mcp")
    parser.add_argument("--operation-timeout", type=float, default=1800.0)
    parser.add_argument("--wait-call-timeout", type=float, default=40.0,
                        help="Maximum seconds per MCP wait call; unfinished jobs return successful progress. Set below the gateway timeout.")
    parser.add_argument("--http-timeout", type=float, default=cli.DEFAULT_HTTP_TIMEOUT)
    parser.add_argument("--download-timeout", type=float, default=cli.DEFAULT_DOWNLOAD_TIMEOUT)
    parser.add_argument("--max-download-size", type=int, default=cli.DEFAULT_MAX_DOWNLOAD_SIZE)
    parser.add_argument("--max-input-file-size", type=int, default=cli.DEFAULT_MAX_INPUT_FILE_SIZE)
    parser.add_argument("--max-request-body-size", type=int, default=2 * 1024 * 1024)
    parser.add_argument("--poll-interval", type=float, default=2.0)
    parser.add_argument("--input-dir", action="append", default=[])
    parser.add_argument("--output-dir", default=str(BASE_DIR / "outputs" / "mcp"))
    parser.add_argument("--env-file", help="Trusted credentials file; defaults to the CLI's adjacent .env")
    parser.add_argument("--auth-token-env", default="HIGGSFIELD_MCP_BEARER_TOKEN")
    parser.add_argument("--allowed-host", action="append", default=[])
    parser.add_argument("--allowed-origin", action="append", default=[])
    parser.add_argument("--litellm-costs", action="store_true")
    args = parser.parse_args(argv)
    if args.port < 1 or args.port > 65535 or not args.endpoint.startswith("/"):
        parser.error("port must be 1..65535 and endpoint must start with '/'")
    for name in ("operation_timeout", "wait_call_timeout", "http_timeout", "download_timeout", "poll_interval"):
        val = getattr(args, name)
        if not isinstance(val, (int, float)) or not 0 < val < float("inf"):
            parser.error(f"{name.replace('_', '-')} must be finite and greater than zero")
    for name in ("max_download_size", "max_input_file_size", "max_request_body_size"):
        val = getattr(args, name)
        if val < 1 or val > MAX_MEDIA_FILE:
            parser.error(f"{name.replace('_', '-')} must be between 1 and {MAX_MEDIA_FILE}")
    args.input_dir = [str(Path(item).expanduser().resolve()) for item in args.input_dir]
    if args.host not in {"127.0.0.1", "::1", "localhost"} and not os.environ.get(args.auth_token_env):
        parser.error(f"Set {args.auth_token_env} before binding beyond loopback")
    return args


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    args = parse_args(argv)
    if sys.version_info < (3, 12):
        print("HiggsfieldAPI-MCP requires Python 3.12 or newer.", file=sys.stderr)
        return 2
    try:
        mcp = create_server(args)
        if args.transport == "stdio":
            mcp.run(transport="stdio")
        else:
            token = os.environ.get(args.auth_token_env)
            mcp.settings.host = args.host
            mcp.settings.port = args.port
            mcp.settings.streamable_http_path = args.endpoint
            app = mcp.streamable_http_app()
            app = RequestBodyLimit(app, args.max_request_body_size)
            if token:
                app = BearerAuth(app, token)
            import uvicorn
            uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        LOG.error("MCP server startup or runtime error (%s)", type(exc).__name__)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
