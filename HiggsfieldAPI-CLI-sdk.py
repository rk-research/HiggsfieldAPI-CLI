#!/usr/bin/env python3
# Copyright (c) 2026 Richard Knuchel
# SPDX-License-Identifier: BSD-2-Clause

"""Higgsfield image and video CLI backed by the official Python SDK."""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import random
import re
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version as package_version
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen


BASE_URL = "https://api.higgsfield.ai"
# Update this value for each release. The leading ``v`` is part of the displayed
# CLI version and matches the project's release tag convention.
CLI_VERSION = "v0.1.0"
SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_DOTENV_PATH = SCRIPT_DIR / ".env"
DEFAULT_HTTP_TIMEOUT = 90.0
DEFAULT_DOWNLOAD_TIMEOUT = 300.0
DEFAULT_MAX_DOWNLOAD_SIZE = 2 * 1024 * 1024 * 1024
DEFAULT_MAX_INPUT_FILE_SIZE = 1024 * 1024
DOWNLOAD_CHUNK_SIZE = 1024 * 1024
MAX_PRESET_PAGES = 100
SUPPORTED_SDK_REQUIREMENT = "higgsfield-client>=0.2,<0.3"
TERMINAL_STATUSES = {"completed", "failed", "nsfw", "canceled"}
SUPPORTED_STATUSES = {"queued", "in_progress", *TERMINAL_STATUSES}
SUPPORTED_MEDIA_TYPES = {
    "image/jpeg", "image/jpg", "image/png", "image/webp", "image/gif",
    "audio/wav", "audio/x-wav", "video/mp4",
}

EXIT_SUCCESS = 0
EXIT_VALIDATION = 2
EXIT_AUTHENTICATION = 3
EXIT_CREDITS = 4
EXIT_API = 5
EXIT_GENERATION = 6
EXIT_MODERATION = 7
EXIT_TIMEOUT = 8
EXIT_NETWORK = 9
EXIT_CANCELED = 10
EXIT_CONCURRENCY = 11
EXIT_ESTIMATE_UNAVAILABLE = 12
EXIT_INTERRUPTED = 130


class AppError(Exception):
    """An expected user-facing failure with a stable category and exit code."""

    def __init__(self, message: str, kind: str = "api", exit_code: int = EXIT_API,
                 *, request_id: Optional[str] = None, correlation_id: Optional[str] = None):
        super().__init__(message)
        self.message = message
        self.kind = kind
        self.exit_code = exit_code
        self.request_id = request_id
        self.correlation_id = correlation_id


class ApiError(AppError):
    def __init__(self, message: str, status: Optional[int] = None, **kwargs: Any):
        self.http_status = status
        super().__init__(message, **kwargs)


class EstimateUnavailable(ApiError):
    """The optional cost-estimate operation is not available for this request."""

    def __init__(self, message: str, status: Optional[int] = None, **kwargs: Any):
        super().__init__(message, status=status, kind="estimate_unavailable",
                         exit_code=EXIT_ESTIMATE_UNAVAILABLE, **kwargs)


@dataclass(frozen=True)
class Credentials:
    key_id: str
    secret: str

    @property
    def authorization(self) -> str:
        return f"Key {self.key_id}:{self.secret}"


def redact(value: Any, credentials: Optional[Credentials] = None) -> str:
    text = str(value)
    if credentials:
        for secret in (credentials.key_id, credentials.secret, credentials.authorization):
            if secret:
                text = text.replace(secret, "[redacted]")
    return text


def header_value(headers: Optional[Mapping[str, str]], name: str) -> Optional[str]:
    """Read an HTTP header without depending on the server's capitalization."""
    if not headers:
        return None
    wanted = name.lower()
    for key, value in headers.items():
        if str(key).lower() == wanted:
            return str(value)
    return None


def parse_env_file(path: Path) -> Dict[str, str]:
    """Parse the deliberately small .env subset supported by this CLI.

    Supported syntax is blank lines, full-line ``#`` comments, and
    ``NAME=value`` assignments. A value may be wrapped in matching single or
    double quotes. ``export`` prefixes are not recognized and inline comments
    are not stripped; any malformed line raises a validation error.
    """
    if not path.exists():
        return {}
    values: Dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise AppError(f"Cannot read {path}: {exc}", kind="validation", exit_code=EXIT_VALIDATION)
    for number, raw in enumerate(lines, 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise AppError(f"Invalid .env line {number}: expected NAME=value.",
                           kind="validation", exit_code=EXIT_VALIDATION)
        name, value = line.split("=", 1)
        name = name.strip()
        value = value.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            raise AppError(f"Invalid .env variable name on line {number}.",
                           kind="validation", exit_code=EXIT_VALIDATION)
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[name] = value
    return values


def load_credentials(env: Optional[Mapping[str, str]] = None,
                     dotenv_path: Optional[Path] = None) -> Credentials:
    environment = dict(os.environ if env is None else env)
    dotenv_values = parse_env_file(dotenv_path or DEFAULT_DOTENV_PATH)
    key_id = environment.get("HF_API_KEY_ID") or dotenv_values.get("HF_API_KEY_ID")
    secret = environment.get("HF_API_KEY_SECRET") or dotenv_values.get("HF_API_KEY_SECRET")
    missing = []
    if not key_id:
        missing.append("HF_API_KEY_ID")
    if not secret:
        missing.append("HF_API_KEY_SECRET")
    if missing:
        raise AppError(
            "Missing required Higgsfield credential(s): " + ", ".join(missing) + ". "
            "Set them in the environment or in a local .env file.",
            kind="authentication", exit_code=EXIT_AUTHENTICATION,
        )
    return Credentials(key_id, secret)


def validate_base_url(value: str) -> str:
    """Validate a base URL before the SDK can attach credentials to it."""
    normalized = value.strip().rstrip("/")
    try:
        parsed = urlparse(normalized)
        hostname = parsed.hostname
        parsed.port  # Force validation of malformed ports.
    except ValueError as exc:
        raise AppError(
            "HF_API_BASE_URL must be a valid https:// URL with a host.",
            kind="configuration", exit_code=EXIT_VALIDATION,
        ) from exc
    if (
        parsed.scheme.lower() != "https"
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise AppError(
            "HF_API_BASE_URL must be an https:// URL with a host and no credentials, query, or fragment.",
            kind="configuration", exit_code=EXIT_VALIDATION,
        )
    return normalized


def resolve_base_url(env: Optional[Mapping[str, str]] = None) -> str:
    """Resolve the trusted process-only base URL and warn for custom targets."""
    environment = os.environ if env is None else env
    raw_value = environment.get("HF_API_BASE_URL")
    if not raw_value or not raw_value.strip():
        return BASE_URL
    base_url = validate_base_url(raw_value)
    if base_url != BASE_URL:
        print(
            "Warning: HF_API_BASE_URL overrides the default API host; "
            "the SDK will send your API key there. Use only a trusted HTTPS endpoint.",
            file=sys.stderr,
        )
    return base_url


def error_from_http(status: int, body: bytes, credentials: Optional[Credentials] = None,
                    headers: Optional[Mapping[str, str]] = None) -> ApiError:
    detail: Any = None
    try:
        payload = json.loads(body.decode("utf-8")) if body else None
        if isinstance(payload, dict):
            detail = payload.get("detail") or payload.get("error") or payload.get("message")
        elif payload:
            detail = payload
    except (UnicodeDecodeError, json.JSONDecodeError):
        detail = None
    detail_text = redact(detail or f"HTTP {status}", credentials)
    lowered = detail_text.lower()
    if status == 401:
        kind, code, message = "authentication", EXIT_AUTHENTICATION, "Invalid or missing Higgsfield credentials."
    elif status == 403 and any(token in lowered for token in ("credit", "balance", "quota")):
        kind, code = "credits", EXIT_CREDITS
        message = (
            "Available Higgsfield API credit is insufficient. "
            f"Higgsfield detail: {detail_text}"
        )
    elif status == 403:
        kind, code, message = "api", EXIT_API, f"Higgsfield denied access ({status}): {detail_text}"
    elif status == 400 and ("concurrent" in lowered or "concurrency" in lowered):
        kind, code, message = "concurrency", EXIT_CONCURRENCY, "The account or model concurrency limit has been reached."
    elif status in (422,):
        kind, code, message = "validation", EXIT_VALIDATION, f"Higgsfield rejected the request: {detail_text}"
    elif status in (500, 502, 503, 504):
        kind, code, message = "api", EXIT_API, f"Higgsfield is temporarily unavailable: {detail_text}"
    else:
        kind, code, message = "api", EXIT_API, f"Higgsfield API error ({status}): {detail_text}"
    return ApiError(message, status=status, kind=kind, exit_code=code,
                    correlation_id=header_value(headers, "X-Correlation-ID"))


def infer_media_type(path: Path) -> str:
    guessed, _ = mimetypes.guess_type(path.name)
    if guessed == "audio/wav":
        return guessed
    if guessed == "audio/x-wav":
        return guessed
    if guessed in SUPPORTED_MEDIA_TYPES:
        return guessed
    suffix_map = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".jpe": "image/jpeg",
                  ".png": "image/png", ".webp": "image/webp", ".gif": "image/gif",
                  ".mp4": "video/mp4", ".wav": "audio/wav"}
    return suffix_map.get(path.suffix.lower(), "")


def resolve_media(value: str, client: SDKClient) -> str:
    if value.startswith("https://"):
        return value
    if value.startswith("http://"):
        raise AppError("Media URLs must use https://.", kind="validation", exit_code=EXIT_VALIDATION)
    path = Path(value)
    if not path.is_file():
        raise AppError(f"Local media file does not exist: {value}", kind="validation", exit_code=EXIT_VALIDATION)
    content_type = infer_media_type(path)
    if content_type not in SUPPORTED_MEDIA_TYPES:
        raise AppError(f"Unsupported local media type for {value}.", kind="validation", exit_code=EXIT_VALIDATION)
    return client.upload_file(path)


def resolve_media_list(values: Optional[Sequence[str]], client: SDKClient) -> Optional[List[str]]:
    if not values:
        return None
    return [resolve_media(value, client) for value in values]


def read_text_input(path: Path, label: str, max_size: int) -> str:
    if max_size < 0:
        raise AppError(f"{label} size limit must be zero or greater.",
                       kind="validation", exit_code=EXIT_VALIDATION)
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise AppError(f"Cannot read {label}: {exc}", kind="validation", exit_code=EXIT_VALIDATION)
    if max_size and len(data) > max_size:
        raise AppError(
            f"{label} exceeds the {max_size}-byte size limit; use --max-input-file-size 0 to disable it.",
            kind="validation", exit_code=EXIT_VALIDATION,
        )
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise AppError(f"Cannot read {label}: expected UTF-8 text.",
                       kind="validation", exit_code=EXIT_VALIDATION) from exc


def read_prompt(args: argparse.Namespace, *, required: bool = True) -> Optional[str]:
    prompt = getattr(args, "prompt", None)
    prompt_file = getattr(args, "prompt_file", None)
    if prompt is not None and prompt_file is not None:
        raise AppError("Use only one of --prompt and --prompt-file.", kind="validation", exit_code=EXIT_VALIDATION)
    if prompt_file is not None:
        if prompt_file == "-":
            prompt = sys.stdin.read()
        else:
            prompt = read_text_input(
                Path(prompt_file), "prompt file",
                getattr(args, "max_input_file_size", DEFAULT_MAX_INPUT_FILE_SIZE),
            )
    elif prompt == "-":
        prompt = sys.stdin.read()
    if prompt is not None:
        prompt = prompt.strip()
    if not prompt:
        if required:
            raise AppError("A non-empty prompt is required.", kind="validation", exit_code=EXIT_VALIDATION)
        return None
    return prompt


def parse_json_params(path_value: Optional[str],
                      max_size: int = DEFAULT_MAX_INPUT_FILE_SIZE) -> Dict[str, Any]:
    if not path_value:
        return {}
    try:
        text = read_text_input(Path(path_value), "--params-json file", max_size)
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise AppError(f"Cannot read --params-json: {exc}", kind="validation", exit_code=EXIT_VALIDATION)
    if not isinstance(data, dict):
        raise AppError("--params-json must contain a JSON object.", kind="validation", exit_code=EXIT_VALIDATION)
    return dict(data)


def apply_params_json(params: Dict[str, Any], args: argparse.Namespace) -> Dict[str, Any]:
    imported = parse_json_params(
        getattr(args, "params_json", None),
        getattr(args, "max_input_file_size", DEFAULT_MAX_INPUT_FILE_SIZE),
    )
    imported.update(params)
    return imported


def validate_enum(value: Any, name: str, allowed: Iterable[Any]) -> None:
    choices = set(allowed)
    if value not in choices:
        rendered = ", ".join(map(str, sorted(choices)))
        raise AppError(f"{name} must be one of: {rendered}.", kind="validation", exit_code=EXIT_VALIDATION)


def validate_range(value: Any, name: str, minimum: int, maximum: int) -> None:
    if value < minimum or value > maximum:
        raise AppError(f"{name} must be between {minimum} and {maximum}.", kind="validation", exit_code=EXIT_VALIDATION)


def validate_urls(values: Optional[Sequence[str]], name: str, minimum: int, maximum: int) -> None:
    count = len(values or [])
    if count and (count < minimum or count > maximum):
        raise AppError(f"{name} accepts {minimum}–{maximum} values.", kind="validation", exit_code=EXIT_VALIDATION)


def add_prompt_options(parser: argparse.ArgumentParser, *, required: bool = False) -> None:
    parser.add_argument("--prompt", help="Text prompt, or '-' to read from stdin.", required=False)
    parser.add_argument("--prompt-file", help="UTF-8 prompt file; use '-' for stdin.")


def add_common_options(parser: argparse.ArgumentParser, *, defaults: bool = True) -> None:
    missing = argparse.SUPPRESS if not defaults else None
    parser.add_argument("--env-file", default=(None if defaults else missing),
                        help="Trusted credentials file path (default: .env next to this script).")
    parser.add_argument("--json", action="store_true", default=(False if defaults else missing), help="Emit only machine-readable JSON.")
    parser.add_argument("--no-wait", action="store_true", default=(False if defaults else missing), help="Submit and return request metadata without polling.")
    parser.add_argument("--timeout", type=float, default=(1800.0 if defaults else missing), help="Application polling timeout in seconds (default: 1800).")
    parser.add_argument("--http-timeout", type=float, default=(DEFAULT_HTTP_TIMEOUT if defaults else missing),
                        help="HTTP API timeout in seconds (default: 90).")
    parser.add_argument("--download-timeout", type=float, default=(DEFAULT_DOWNLOAD_TIMEOUT if defaults else missing),
                        help="Output-download HTTP timeout in seconds (default: 300).")
    parser.add_argument("--max-download-size", type=int,
                        default=(DEFAULT_MAX_DOWNLOAD_SIZE if defaults else missing),
                        help="Maximum output size in bytes (default: 2 GiB; use 0 for unlimited).")
    parser.add_argument("--max-input-file-size", type=int,
                        default=(DEFAULT_MAX_INPUT_FILE_SIZE if defaults else missing),
                        help="Maximum prompt/params file size in bytes (default: 1 MiB; use 0 for unlimited).")
    parser.add_argument("--poll-interval", type=float, default=(2.0 if defaults else missing), help="Initial polling interval in seconds (default: 2).")
    parser.add_argument("--output-dir", default=("." if defaults else missing), help="Directory for downloaded output (default: current directory).")
    parser.add_argument("--no-download", action="store_true", default=(False if defaults else missing), help="Keep output URLs but do not download generated media.")
    parser.add_argument("--overwrite", action="store_true", default=(False if defaults else missing), help="Allow replacing an existing output file.")
    parser.add_argument("--estimate-only", action="store_true", default=(False if defaults else missing), help="Estimate cost without submitting generation.")
    parser.add_argument("--params-json", help="Merge advanced request fields from a JSON object file.")
    parser.add_argument("--debug", action="store_true", default=(False if defaults else missing),
                        help="Include the unexpected exception class in diagnostics.")


def add_video_common(parser: argparse.ArgumentParser, version: str, workflow: str) -> None:
    add_prompt_options(parser)
    if version == "2.0":
        resolutions = ["480p", "720p", "1080p", "4k"]
    elif version in {"2.5", "cinema4"}:
        resolutions = ["480p", "720p"]
    else:
        resolutions = None
    if resolutions:
        parser.add_argument("--resolution", default=None, choices=resolutions)
    if workflow != "edit":
        duration_ranges = {"2.0": "4-15 s", "2.5": "4-30 s", "cinema4": "4-30 s", "kling3": "3-15 s"}
        parser.add_argument("--duration", type=int, default=None,
                            help=f"Duration in seconds (model range: {duration_ranges[version]}).")
    parser.add_argument("--generate-audio", dest="generate_audio", action=argparse.BooleanOptionalAction, default=None,
                        help="Generate native audio when supported by the model.")
    if (version in {"2.0", "2.5", "cinema4"} and workflow in {"text", "reference"}) or (version == "kling3" and workflow == "text"):
        aspect_ratios = ["16:9", "4:3", "1:1", "3:4", "9:16", "21:9"]
        if version == "cinema4":
            aspect_ratios = ["16:9"]
        elif version == "kling3":
            aspect_ratios = ["16:9", "9:16", "1:1"]
        parser.add_argument("--aspect-ratio", dest="aspect_ratio", default=None, choices=aspect_ratios)
    if version == "2.5":
        parser.add_argument("--output-format", dest="output_format", default=None, choices=["mp4", "mov"])
        parser.add_argument("--bitrate-mode", dest="bitrate_mode", default=None, choices=["high"],
                            help="Documented model-catalog option for Seedance 2.5.")
    if version == "kling3":
        parser.add_argument("--cfg-scale", dest="cfg_scale", type=float, default=None,
                            help="Prompt adherence scale from 0 to 1 (default: 0.5).")
        parser.add_argument("--multi-shots", dest="multi_shots", action=argparse.BooleanOptionalAction, default=None,
                            help="Enable multi-shot generation.")


def video_params(args: argparse.Namespace, version: str, workflow: str, client: SDKClient) -> Dict[str, Any]:
    if version == "cinema4":
        params: Dict[str, Any] = {"prompt": read_prompt(args, required=True)}
        duration = args.duration if args.duration is not None else 5
        resolution = args.resolution or "720p"
        aspect_ratio = args.aspect_ratio or "16:9"
        generate_audio = args.generate_audio if args.generate_audio is not None else True
        validate_range(duration, "duration", 4, 30)
        validate_enum(resolution, "resolution", ["480p", "720p"])
        validate_enum(aspect_ratio, "aspect_ratio", ["16:9"])
        params.update({"duration": duration, "resolution": resolution,
                       "aspect_ratio": aspect_ratio, "generate_audio": generate_audio})
        if workflow == "reference":
            image_urls = resolve_media_list(args.image_ref, client)
            video_urls = resolve_media_list(args.video_ref, client)
            if not image_urls and not video_urls:
                raise AppError("Cinema Studio reference workflow requires at least one image or video reference.",
                               kind="validation", exit_code=EXIT_VALIDATION)
            if len(image_urls or []) + len(video_urls or []) > 30:
                raise AppError("Cinema Studio accepts at most 30 image/video references.",
                               kind="validation", exit_code=EXIT_VALIDATION)
            if image_urls:
                params["image_urls"] = image_urls
            if video_urls:
                params["video_urls"] = video_urls
        return apply_params_json(params, args)

    if version == "kling3":
        params: Dict[str, Any] = {}
        prompt = read_prompt(args, required=workflow == "text")
        if prompt is not None:
            params["prompt"] = prompt
        duration = args.duration if args.duration is not None else 5
        sound = "on" if args.generate_audio is not False else "off"
        cfg_scale = args.cfg_scale if args.cfg_scale is not None else 0.5
        multi_shots = args.multi_shots if args.multi_shots is not None else False
        validate_range(duration, "duration", 3, 15)
        if cfg_scale < 0 or cfg_scale > 1:
            raise AppError("cfg_scale must be between 0 and 1.", kind="validation", exit_code=EXIT_VALIDATION)
        params.update({"duration": duration, "sound": sound, "cfg_scale": cfg_scale,
                       "multi_shots": multi_shots})
        if workflow == "text":
            params["aspect_ratio"] = getattr(args, "aspect_ratio", None) or "16:9"
            validate_enum(params["aspect_ratio"], "aspect_ratio", ["16:9", "9:16", "1:1"])
        else:
            params["image_url"] = resolve_media(args.image, client)
            if args.end_image:
                params["last_image_url"] = resolve_media(args.end_image, client)
        return apply_params_json(params, args)

    required_prompt = workflow in {"text", "edit", "extend"}
    params: Dict[str, Any] = {}
    prompt = read_prompt(args, required=required_prompt)
    if prompt is not None:
        params["prompt"] = prompt
    if version == "2.0":
        resolution = args.resolution or "720p"
        duration_arg = getattr(args, "duration", None)
        duration = duration_arg if duration_arg is not None else 5
        generate_audio = args.generate_audio if args.generate_audio is not None else True
        aspect_ratio = getattr(args, "aspect_ratio", None) or "16:9"
        validate_enum(resolution, "resolution", ["480p", "720p", "1080p", "4k"])
        validate_range(duration, "duration", 4, 15)
    else:
        resolution = args.resolution or "720p"
        generate_audio = args.generate_audio if args.generate_audio is not None else True
        aspect_ratio = getattr(args, "aspect_ratio", None) or "16:9"
        validate_enum(resolution, "resolution", ["480p", "720p"])
        duration_arg = getattr(args, "duration", None)
        duration = duration_arg if duration_arg is not None else 5
        if workflow in {"edit"}:
            duration = None
        else:
            validate_range(duration, "duration", 4, 30)
        if args.output_format is not None:
            validate_enum(args.output_format, "output_format", ["mp4", "mov"])
    params.update({"resolution": resolution, "generate_audio": generate_audio})
    if duration is not None:
        params["duration"] = duration
    if workflow in {"text", "reference"}:
        params["aspect_ratio"] = aspect_ratio
    if version == "2.5" and workflow != "image" and args.output_format is not None:
        params["output_format"] = args.output_format
    elif version == "2.5" and workflow in {"text", "image", "reference", "extend", "edit"}:
        params["output_format"] = args.output_format or "mp4"
    if version == "2.5" and args.bitrate_mode:
        params["bitrate_mode"] = args.bitrate_mode
    if workflow == "image":
        params["image_url"] = resolve_media(args.image, client)
        if args.end_image:
            params["end_image_url"] = resolve_media(args.end_image, client)
    elif workflow == "reference":
        image_urls = resolve_media_list(args.image_ref, client)
        video_urls = resolve_media_list(args.video_ref, client)
        audio_urls = resolve_media_list(args.audio_ref, client)
        if not image_urls and not video_urls and not audio_urls:
            raise AppError("Reference workflow requires at least one image, video, or audio reference.",
                           kind="validation", exit_code=EXIT_VALIDATION)
        if version == "2.0":
            if not image_urls and not video_urls:
                raise AppError("Seedance 2.0 reference workflow requires at least one image or video reference.",
                               kind="validation", exit_code=EXIT_VALIDATION)
            validate_urls(image_urls, "image references", 1, 9)
            validate_urls(video_urls, "video references", 1, 3)
            validate_urls(audio_urls, "audio references", 1, 3)
        else:
            validate_urls(image_urls, "image references", 1, 30)
            validate_urls(video_urls, "video references", 1, 10)
            validate_urls(audio_urls, "audio references", 1, 10)
        if image_urls:
            params["image_urls"] = image_urls
        if video_urls:
            params["video_urls"] = video_urls
        if audio_urls:
            params["audio_urls"] = audio_urls
    elif workflow in {"edit", "extend"}:
        params["video_url"] = resolve_media(args.video, client)
        image_urls = resolve_media_list(args.image_ref, client)
        video_urls = resolve_media_list(args.video_ref, client)
        audio_urls = resolve_media_list(args.audio_ref, client)
        validate_urls(image_urls, "image references", 1, 30)
        validate_urls(video_urls, "video references", 1, 10)
        validate_urls(audio_urls, "audio references", 1, 10)
        if image_urls:
            params["image_urls"] = image_urls
        if video_urls:
            params["video_urls"] = video_urls
        if audio_urls:
            params["audio_urls"] = audio_urls
    if workflow == "image" and version == "2.0":
        params.pop("aspect_ratio", None)
    return apply_params_json(params, args)


def image_params(args: argparse.Namespace, model: str, client: SDKClient) -> Dict[str, Any]:
    params: Dict[str, Any] = {"prompt": read_prompt(args, required=True)}
    if model == "soul2":
        if args.seed is not None:
            validate_range(args.seed, "seed", 1, 1000000)
            params["seed"] = args.seed
        if args.style_id:
            params["style_id"] = args.style_id
        params.update({"batch_size": args.batch_size or 1,
                       "resolution": args.resolution or "720p",
                       "aspect_ratio": args.aspect_ratio or "4:3",
                       "enhance_prompt": args.enhance_prompt if args.enhance_prompt is not None else True})
        validate_enum(params["batch_size"], "batch_size", [1, 4])
        validate_enum(params["resolution"], "resolution", ["720p", "1080p"])
        validate_enum(params["aspect_ratio"], "aspect_ratio", ["9:16", "16:9", "4:3", "3:4", "1:1", "2:3", "3:2"])
    elif model == "ideogram4":
        prompt_length = len(params["prompt"] or "")
        if prompt_length < 2 or prompt_length > 2048:
            raise AppError("Ideogram 4.0 prompts must contain 2–2048 characters.",
                           kind="validation", exit_code=EXIT_VALIDATION)
        if args.image:
            params["image_url"] = resolve_media(args.image, client)
        if args.image_weight is not None:
            validate_range(args.image_weight, "image_weight", 1, 100)
            params["image_weight"] = args.image_weight
        params.update({"aspect_ratio": args.aspect_ratio or "1:1",
                       "rendering_speed": args.rendering_speed or "DEFAULT"})
        validate_enum(params["aspect_ratio"], "aspect_ratio", [
            "1:1", "1:2", "2:1", "2:3", "3:2", "4:5", "5:4", "9:16", "16:9",
            "5:8", "8:5", "3:4", "4:3", "9:22", "22:9", "9:23", "23:9",
            "3:8", "8:3", "5:12", "12:5", "1:3", "3:1",
        ])
        validate_enum(params["rendering_speed"], "rendering_speed", ["TURBO", "DEFAULT", "QUALITY"])
    elif model == "marketing-studio":
        if len(params["prompt"] or "") > 5000:
            raise AppError("Marketing Studio prompts cannot exceed 5000 characters.",
                           kind="validation", exit_code=EXIT_VALIDATION)
        image_urls = resolve_media_list(args.image, client)
        if image_urls:
            validate_urls(image_urls, "image inputs", 1, 16)
            params["image_urls"] = image_urls
        if args.enhance_prompt:
            if not args.preset_id:
                raise AppError("--preset-id is required with --enhance-prompt.", kind="validation", exit_code=EXIT_VALIDATION)
            if not image_urls or len(image_urls) > 2:
                raise AppError("Enhanced Marketing Studio requests require one or two images.", kind="validation", exit_code=EXIT_VALIDATION)
            params["quality"] = "high"
        else:
            params["quality"] = args.quality or "high"
        params.update({"moderation": args.moderation or "auto", "resolution": args.resolution or "2k",
                       "aspect_ratio": args.aspect_ratio or "auto", "enhance_prompt": bool(args.enhance_prompt)})
        if args.preset_id:
            params["preset_id"] = args.preset_id
        validate_enum(params["quality"], "quality", ["low", "medium", "high"])
        validate_enum(params["moderation"], "moderation", ["auto", "low"])
        validate_enum(params["resolution"], "resolution", ["1k", "2k", "4k"])
        validate_enum(params["aspect_ratio"], "aspect_ratio", ["auto", "1:1", "3:2", "2:3", "4:3", "3:4", "16:9", "9:16", "21:9"])
        if args.enhance_prompt:
            params["preset_id"] = args.preset_id
    else:
        image_urls = resolve_media_list(args.image, client)
        if image_urls:
            params["image_urls"] = image_urls
        params.update({"quality": args.quality or "medium", "resolution": args.resolution or "1k",
                       "aspect_ratio": args.aspect_ratio or "auto"})
        # The current Grok reference does not publish a per-request image count limit.
        validate_enum(params["quality"], "quality", ["low", "medium"])
        validate_enum(params["resolution"], "resolution", ["1k", "2k"])
        validate_enum(params["aspect_ratio"], "aspect_ratio", ["auto", "1:1", "1:2", "2:1", "3:2", "2:3", "4:3", "3:4", "16:9", "9:16"])
    return apply_params_json(params, args)


MODEL_ENDPOINTS = {
    ("marketing-studio", "generate"): "/marketing-studio/image",
    ("grok-image-2", "generate"): "/xai/grok-imagine-image-2.0",
    ("soul2", "generate"): "/higgsfield-ai/soul/v2/standard",
    ("ideogram4", "generate"): "/ideogram/v4.0",
    ("seedance-2", "text"): "/bytedance/seedance-2.0/text-to-video",
    ("seedance-2", "image"): "/bytedance/seedance-2.0/image-to-video",
    ("seedance-2", "reference"): "/bytedance/seedance-2.0/reference-to-video",
    ("seedance-2.5", "text"): "/bytedance/seedance-2.5/text-to-video",
    ("seedance-2.5", "image"): "/bytedance/seedance-2.5/image-to-video",
    ("seedance-2.5", "reference"): "/bytedance/seedance-2.5/reference-to-video",
    ("seedance-2.5", "edit"): "/bytedance/seedance-2.5/video-edit",
    ("seedance-2.5", "extend"): "/bytedance/seedance-2.5/video-extend",
    ("cinema-studio-4.0", "text"): "/higgsfield/cinema-studio/4.0",
    ("cinema-studio-4.0", "reference"): "/higgsfield/cinema-studio/4.0",
    ("kling-3.0-standard", "text"): "/kling-video/v3.0/std/text-to-video",
    ("kling-3.0-standard", "image"): "/kling-video/v3.0/std/image-to-video",
}


def endpoint_for(model: str, workflow: str) -> str:
    try:
        return MODEL_ENDPOINTS[(model, workflow)]
    except KeyError as exc:
        raise AppError(f"Unsupported model/workflow: {model}/{workflow}.", kind="validation",
                       exit_code=EXIT_VALIDATION) from exc


def estimate_cost(client: SDKClient, endpoint: str, params: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    """Return the documented estimate, or mark the optional route unavailable."""
    try:
        payload, _ = client.json_request("POST", "/estimate" + endpoint, params)
    except ApiError as exc:
        if exc.http_status in (403, 404, 405):
            status = f"HTTP {exc.http_status}" if exc.http_status else "an API error"
            raise EstimateUnavailable(
                f"Cost estimate unavailable for {endpoint} ({status}); continuing without an estimate.",
                status=exc.http_status, correlation_id=exc.correlation_id,
            ) from exc
        raise
    if not isinstance(payload, dict):
        raise ApiError("Higgsfield returned an invalid cost estimate.", kind="api")
    result: Dict[str, Any] = {}
    if "credits" in payload:
        result["credits"] = payload["credits"]
    if "usd" in payload:
        result["usd"] = payload["usd"]
    return result or None


def request_metadata(payload: Mapping[str, Any], *, model: Optional[str] = None,
                     workflow: Optional[str] = None) -> Dict[str, Any]:
    metadata: Dict[str, Any] = {
        "status": payload.get("status"),
        "request_id": payload.get("request_id"),
    }
    for key in ("status_url", "cancel_url", "error"):
        if key in payload:
            metadata[key] = payload[key]
    if model:
        metadata["model"] = model
    if workflow:
        metadata["workflow"] = workflow
    return metadata


def fetch_status(client: SDKClient, request_id: str, status_url: Optional[str] = None) -> Dict[str, Any]:
    if not request_id.strip():
        raise AppError("REQUEST_ID must not be empty.", kind="validation", exit_code=EXIT_VALIDATION)
    url = status_url or f"/requests/{request_id}/status"
    payload, _ = client.json_request("GET", url, retry_get=True)
    if not isinstance(payload, dict):
        raise ApiError("Higgsfield returned an invalid request status.", kind="api")
    status = payload.get("status")
    if status not in SUPPORTED_STATUSES:
        raise ApiError("Higgsfield returned an unknown request status.", kind="api")
    return payload


def poll_request(client: SDKClient, initial: Mapping[str, Any], *, timeout: float,
                 interval: float, json_mode: bool, watch: bool = True,
                 progress: Optional[Callable[[str], None]] = None) -> Dict[str, Any]:
    request_id = str(initial.get("request_id") or "")
    if not request_id:
        raise ApiError("Higgsfield accepted a request without a request_id.", kind="api")
    status_url = initial.get("status_url")
    current = dict(initial)
    if progress and current.get("status"):
        progress(str(current["status"]))
    if current.get("status") in TERMINAL_STATUSES:
        return current
    started = time.monotonic()
    delay = max(0.1, interval)
    try:
        while watch:
            if time.monotonic() - started >= timeout:
                raise AppError(f"Timed out waiting for request {request_id}.", kind="timeout",
                               exit_code=EXIT_TIMEOUT, request_id=request_id)
            time.sleep(delay + (0 if json_mode else random.uniform(0, 0.5)))
            current = fetch_status(client, request_id, status_url)
            if progress and current.get("status") != initial.get("status"):
                progress(str(current.get("status")))
            if current.get("status") in TERMINAL_STATUSES:
                return current
            initial = current
            delay = min(delay * 1.5, 10.0)
    except KeyboardInterrupt as exc:
        raise AppError(
            f"Interrupted while waiting for request {request_id}; the request is still running.",
            kind="interrupted", exit_code=EXIT_INTERRUPTED, request_id=request_id,
        ) from exc
    return current


def classify_terminal(payload: Mapping[str, Any]) -> Optional[AppError]:
    status = payload.get("status")
    request_id = payload.get("request_id")
    if status == "failed":
        return AppError(str(payload.get("error") or "Generation failed."), kind="generation",
                        exit_code=EXIT_GENERATION, request_id=request_id)
    if status == "nsfw":
        return AppError("The request was rejected by Higgsfield content moderation.", kind="moderation",
                        exit_code=EXIT_MODERATION, request_id=request_id)
    if status == "canceled":
        return AppError("The Higgsfield request was canceled.", kind="canceled",
                        exit_code=EXIT_CANCELED, request_id=request_id)
    return None


def output_items(payload: Mapping[str, Any]) -> List[Dict[str, str]]:
    outputs: List[Dict[str, str]] = []
    for item in payload.get("images") or []:
        if isinstance(item, dict) and isinstance(item.get("url"), str):
            outputs.append({"type": "image", "url": item["url"]})
    for key in ("video", "audio"):
        item = payload.get(key)
        if isinstance(item, dict) and isinstance(item.get("url"), str):
            outputs.append({"type": key, "url": item["url"]})
    for key in ("audios",):
        for item in payload.get(key) or []:
            if isinstance(item, dict) and isinstance(item.get("url"), str):
                outputs.append({"type": "audio", "url": item["url"]})
    return outputs


def output_extension(url: str, media_type: str) -> str:
    suffix = Path(urlparse(url).path).suffix.lower()
    if suffix in {".jpg", ".jpeg", ".png", ".webp", ".gif", ".mp4", ".mov", ".wav", ".zip"}:
        return suffix
    return {"image": ".jpg", "video": ".mp4", "audio": ".wav"}.get(media_type, ".bin")


def safe_output_name(model: str, workflow: str, request_id: str, url: str, media_type: str) -> str:
    clean_model = re.sub(r"[^A-Za-z0-9_.-]+", "-", model)
    clean_workflow = re.sub(r"[^A-Za-z0-9_.-]+", "-", workflow)
    short_id = re.sub(r"[^A-Za-z0-9]+", "", request_id)[:8] or "request"
    return f"{clean_model}_{clean_workflow}_{short_id}{output_extension(url, media_type)}"


def unique_output_path(directory: Path, filename: str, overwrite: bool) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    candidate = directory / filename
    if overwrite or not candidate.exists():
        return candidate
    stem, suffix = candidate.stem, candidate.suffix
    for index in range(1, 10000):
        candidate = directory / f"{stem}-{index}{suffix}"
        if not candidate.exists():
            return candidate
    raise AppError("Could not find a collision-free output filename.", kind="api")


def download_outputs(client: SDKClient, outputs: List[Dict[str, str]], model: str,
                     workflow: str, request_id: str, directory: str, overwrite: bool) -> List[Dict[str, str]]:
    result: List[Dict[str, str]] = []
    target_dir = Path(directory)
    for output in outputs:
        filename = safe_output_name(model, workflow, request_id, output["url"], output["type"])
        target = unique_output_path(target_dir, filename, overwrite)
        try:
            client.download_file(output["url"], target)
        except KeyboardInterrupt as exc:
            raise AppError(
                f"Interrupted while downloading output for request {request_id}.",
                kind="interrupted", exit_code=EXIT_INTERRUPTED, request_id=request_id,
            ) from exc
        output = dict(output)
        output["file"] = str(target)
        result.append(output)
    return result


def human_progress(message: str) -> None:
    print(f"Status: {message}")


def build_result(*, model: Optional[str], workflow: Optional[str], payload: Mapping[str, Any],
                 estimate: Optional[Mapping[str, Any]] = None,
                 outputs: Optional[List[Dict[str, str]]] = None,
                 charged_cost: Any = None) -> Dict[str, Any]:
    result: Dict[str, Any] = {"ok": True}
    if model is not None:
        result["model"] = model
    if workflow is not None:
        result["workflow"] = workflow
    result.update(request_metadata(payload))
    result["estimated_cost"] = dict(estimate) if estimate is not None else None
    result["charged_cost"] = charged_cost
    result["remaining_credits"] = None
    if outputs is not None:
        result["outputs"] = outputs
    return result


def build_error_result(error: AppError) -> Dict[str, Any]:
    data: Dict[str, Any] = {"ok": False, "error": {"type": error.kind, "message": error.message}}
    if isinstance(error, ApiError) and error.http_status is not None:
        data["error"]["http_status"] = error.http_status
    if error.request_id:
        data["error"]["request_id"] = error.request_id
    if error.correlation_id:
        data["error"]["correlation_id"] = error.correlation_id
    return data


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=Path(sys.argv[0]).name,
        description="Generate Higgsfield images and videos with local media upload, polling, and downloads.",
        epilog="Examples: image marketing-studio --prompt 'Product on marble'; video seedance-2.5 text --prompt 'A coastal tracking shot'",
    )
    parser.add_argument("--version", action="version", version="%(prog)s " + CLI_VERSION)
    add_common_options(parser)
    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND")

    image = subparsers.add_parser("image", help="Image generation and editing workflows.")
    add_common_options(image, defaults=False)
    image_sub = image.add_subparsers(dest="image_model", metavar="MODEL")
    marketing = image_sub.add_parser("marketing-studio", help="Marketing Studio Image generation/editing.")
    add_common_options(marketing, defaults=False)
    add_prompt_options(marketing)
    marketing.add_argument("--image", action="append", help="Input image URL or local file; repeat for editing.")
    marketing.add_argument("--preset-id", help="Marketing Studio preset UUID for enhanced mode.")
    marketing.add_argument("--enhance-prompt", action="store_true", help="Apply a Marketing Studio preset.")
    marketing.add_argument("--quality", choices=["low", "medium", "high"])
    marketing.add_argument("--moderation", choices=["auto", "low"])
    marketing.add_argument("--resolution", choices=["1k", "2k", "4k"])
    marketing.add_argument("--aspect-ratio", dest="aspect_ratio",
                           choices=["auto", "1:1", "3:2", "2:3", "4:3", "3:4", "16:9", "9:16", "21:9"])

    grok = image_sub.add_parser("grok-image-2", help="Grok Imagine 2.0 generation/editing.")
    add_common_options(grok, defaults=False)
    add_prompt_options(grok)
    grok.add_argument("--image", action="append", help="Input image URL or local file; repeat for editing.")
    grok.add_argument("--quality", choices=["low", "medium"])
    grok.add_argument("--resolution", choices=["1k", "2k"])
    grok.add_argument("--aspect-ratio", dest="aspect_ratio",
                      choices=["auto", "1:1", "1:2", "2:1", "3:2", "2:3", "4:3", "3:4", "16:9", "9:16"])

    soul = image_sub.add_parser("soul2", help="Higgsfield Soul 2 image generation.")
    add_common_options(soul, defaults=False)
    add_prompt_options(soul)
    soul.add_argument("--seed", type=int, help="Reproducibility seed from 1 to 1000000.")
    soul.add_argument("--style-id", help="Optional Soul style ID.")
    soul.add_argument("--batch-size", type=int, choices=[1, 4], help="Number of result images (1 or 4).")
    soul.add_argument("--resolution", choices=["720p", "1080p"])
    soul.add_argument("--aspect-ratio", dest="aspect_ratio",
                      choices=["9:16", "16:9", "4:3", "3:4", "1:1", "2:3", "3:2"])
    soul.add_argument("--enhance-prompt", action=argparse.BooleanOptionalAction, default=None)

    ideogram = image_sub.add_parser("ideogram4", help="Ideogram 4.0 image generation/editing.")
    add_common_options(ideogram, defaults=False)
    add_prompt_options(ideogram)
    ideogram.add_argument("--image", help="Optional input image URL or local file.")
    ideogram.add_argument("--image-weight", type=int, help="Input-image influence from 1 to 100.")
    ideogram.add_argument("--rendering-speed", choices=["TURBO", "DEFAULT", "QUALITY"])
    ideogram.add_argument("--aspect-ratio", dest="aspect_ratio",
                          choices=["1:1", "1:2", "2:1", "2:3", "3:2", "4:5", "5:4", "9:16", "16:9",
                                   "5:8", "8:5", "3:4", "4:3", "9:22", "22:9", "9:23", "23:9",
                                   "3:8", "8:3", "5:12", "12:5", "1:3", "3:1"])

    video = subparsers.add_parser("video", help="Video generation, editing, and extension workflows.")
    add_common_options(video, defaults=False)
    video_sub = video.add_subparsers(dest="video_model", metavar="MODEL")
    for version, model_name in (("2.0", "seedance-2"), ("2.5", "seedance-2.5")):
        model = video_sub.add_parser(model_name, help=f"ByteDance Seedance {version} workflows.")
        workflows = model.add_subparsers(dest="workflow", metavar="WORKFLOW")
        for workflow, label in (("text", "Text to video"), ("image", "Image to video"), ("reference", "Reference to video")):
            leaf = workflows.add_parser(workflow, help=label + ".")
            add_common_options(leaf, defaults=False)
            add_video_common(leaf, version, workflow)
            if workflow == "image":
                leaf.add_argument("--image", required=True, help="First-frame image URL or local file.")
                leaf.add_argument("--end-image", help="Optional last-frame image URL or local file.")
            elif workflow == "reference":
                leaf.add_argument("--image-ref", action="append", help="Reference image URL or local file; repeatable.")
                leaf.add_argument("--video-ref", action="append", help="Reference video URL or local file; repeatable.")
                leaf.add_argument("--audio-ref", action="append", help="Reference audio URL or local file; repeatable.")

            else:
                # Keep the prompt options already added by add_video_common.
                pass
        if version == "2.5":
            for workflow, label in (("edit", "Edit an existing video"), ("extend", "Extend an existing video")):
                leaf = workflows.add_parser(workflow, help=label + ".")
                add_common_options(leaf, defaults=False)
                add_video_common(leaf, version, workflow)
                leaf.add_argument("--video", required=True, help="Source video URL or local file.")
                leaf.add_argument("--image-ref", action="append", help="Reference image URL or local file; repeatable.")
                leaf.add_argument("--video-ref", action="append", help="Reference video URL or local file; repeatable.")
                leaf.add_argument("--audio-ref", action="append", help="Reference audio URL or local file; repeatable.")

    cinema = video_sub.add_parser("cinema-studio-4.0", help="Cinema Studio 4.0 workflows.")
    cinema_workflows = cinema.add_subparsers(dest="workflow", metavar="WORKFLOW")
    cinema_text = cinema_workflows.add_parser("text", help="Text to video.")
    add_common_options(cinema_text, defaults=False)
    add_video_common(cinema_text, "cinema4", "text")
    cinema_reference = cinema_workflows.add_parser("reference", help="Reference to video.")
    add_common_options(cinema_reference, defaults=False)
    add_video_common(cinema_reference, "cinema4", "reference")
    cinema_reference.add_argument("--image-ref", action="append", help="Reference image URL or local file; repeatable.")
    cinema_reference.add_argument("--video-ref", action="append", help="Reference video URL or local file; repeatable.")

    kling = video_sub.add_parser("kling-3.0-standard", help="Kling 3.0 Standard workflows.")
    kling_workflows = kling.add_subparsers(dest="workflow", metavar="WORKFLOW")
    kling_text = kling_workflows.add_parser("text", help="Text to video.")
    add_common_options(kling_text, defaults=False)
    add_video_common(kling_text, "kling3", "text")
    kling_image = kling_workflows.add_parser("image", help="Image to video.")
    add_common_options(kling_image, defaults=False)
    add_video_common(kling_image, "kling3", "image")
    kling_image.add_argument("--image", required=True, help="First-frame image URL or local file.")
    kling_image.add_argument("--end-image", help="Optional last-frame image URL or local file.")

    presets = subparsers.add_parser("presets", help="List current visible Marketing Studio presets.")
    add_common_options(presets, defaults=False)
    presets.add_argument("--search", help="Filter returned preset names/IDs locally.")
    presets.add_argument("--size", type=int, default=50, help="Page size (default: 50).")
    presets.add_argument("--cursor", help="API pagination cursor.")
    presets.add_argument("--all", action="store_true", help="Follow all documented cursor pages.")

    status = subparsers.add_parser("status", help="Retrieve a request status; use --watch to poll.")
    add_common_options(status, defaults=False)
    status.add_argument("request_id")
    status.add_argument("--watch", action="store_true", help="Poll until a terminal status.")

    cancel = subparsers.add_parser("cancel", help="Cancel a queued request.")
    add_common_options(cancel, defaults=False)
    cancel.add_argument("request_id")

    credits = subparsers.add_parser("credits", help="Report public API balance support.")
    credits.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="Emit only machine-readable JSON.")
    credits.add_argument("--debug", action="store_true", default=argparse.SUPPRESS,
                         help="Include the unexpected exception class in diagnostics.")
    return parser


def normalise_args(args: argparse.Namespace) -> None:
    """Apply root defaults when a global option appeared after a subcommand."""
    defaults = {"json": False, "env_file": None, "no_wait": False, "timeout": 1800.0, "poll_interval": 2.0,
                "http_timeout": DEFAULT_HTTP_TIMEOUT, "download_timeout": DEFAULT_DOWNLOAD_TIMEOUT,
                "max_download_size": DEFAULT_MAX_DOWNLOAD_SIZE,
                "max_input_file_size": DEFAULT_MAX_INPUT_FILE_SIZE, "debug": False,
                "output_dir": ".", "no_download": False, "overwrite": False, "estimate_only": False}
    for key, value in defaults.items():
        if getattr(args, key, None) is None:
            setattr(args, key, value)


def make_client(args: argparse.Namespace) -> SDKClient:
    env_file = getattr(args, "env_file", None)
    credentials = load_credentials(dotenv_path=Path(env_file) if env_file else None)
    return SDKClient(
        credentials,
        base_url=resolve_base_url(),
        timeout=args.http_timeout,
        download_timeout=args.download_timeout,
        max_download_size=args.max_download_size,
    )


def run_generation(args: argparse.Namespace, model: str, workflow: str) -> Dict[str, Any]:
    client = make_client(args)
    if model in {"marketing-studio", "grok-image-2", "soul2", "ideogram4"}:
        params = image_params(args, model, client)
        endpoint = endpoint_for(model, "generate")
    else:
        video_version = {
            "seedance-2": "2.0", "seedance-2.5": "2.5",
            "cinema-studio-4.0": "cinema4", "kling-3.0-standard": "kling3",
        }[model]
        params = video_params(args, video_version, workflow, client)
        endpoint = endpoint_for(model, workflow)
    estimate_warning: Optional[str] = None
    try:
        estimate = estimate_cost(client, endpoint, params)
    except EstimateUnavailable as exc:
        if args.estimate_only:
            raise
        estimate = None
        estimate_warning = exc.message
        print(f"Warning: {estimate_warning}", file=sys.stderr)
    if args.estimate_only:
        result = {"ok": True, "model": model, "workflow": workflow, "status": "estimate_only",
                  "request_id": None, "estimated_cost": estimate, "charged_cost": None,
                  "remaining_credits": None}
        if not args.json:
            print("Estimated cost: unavailable" if estimate is None else
                  f"Estimated cost: {estimate.get('credits', '?')} credits / ${estimate.get('usd', '?')}")
        return result
    if not args.json:
        print("Submitting...")
    payload, response = client.json_request("POST", endpoint, params)
    if not isinstance(payload, dict):
        raise ApiError("Higgsfield returned an invalid submission response.", kind="api")
    request_id = payload.get("request_id")
    if not request_id:
        raise ApiError("Higgsfield accepted a request without a request_id.", kind="api")
    if not args.json:
        print(f"Request: {request_id}")
        if estimate:
            print(f"Estimated cost: {estimate.get('credits', '?')} credits / ${estimate.get('usd', '?')}")
    if args.no_wait:
        return build_result(model=model, workflow=workflow, payload=payload, estimate=estimate)
    final = poll_request(client, payload, timeout=args.timeout, interval=args.poll_interval,
                         json_mode=args.json, progress=None if args.json else human_progress)
    terminal_error = classify_terminal(final)
    if terminal_error:
        raise terminal_error
    outputs = output_items(final)
    if final.get("status") == "completed" and not outputs:
        raise ApiError("Higgsfield marked the request completed but returned no output URL.", kind="api",
                       request_id=request_id)
    if outputs and not args.no_download:
        outputs = download_outputs(client, outputs, model, workflow, request_id,
                                   args.output_dir, args.overwrite)
    charged = final.get("charged_cost") if "charged_cost" in final else None
    return build_result(model=model, workflow=workflow, payload=final, estimate=estimate,
                        outputs=outputs, charged_cost=charged)


def run_status(args: argparse.Namespace) -> Dict[str, Any]:
    client = make_client(args)
    current = fetch_status(client, args.request_id)
    if args.watch and current.get("status") not in TERMINAL_STATUSES:
        current = poll_request(client, current, timeout=args.timeout, interval=args.poll_interval,
                               json_mode=args.json, progress=None if args.json else human_progress)
    terminal_error = classify_terminal(current) if current.get("status") in TERMINAL_STATUSES else None
    if terminal_error:
        raise terminal_error
    outputs = output_items(current)
    if args.watch and current.get("status") == "completed" and outputs and not args.no_download:
        outputs = download_outputs(client, outputs, "request", "status", args.request_id,
                                   args.output_dir, args.overwrite)
    return build_result(model=None, workflow=None, payload=current, outputs=outputs)


def run_cancel(args: argparse.Namespace) -> Dict[str, Any]:
    client = make_client(args)
    client.request("POST", f"/requests/{args.request_id}/cancel")
    return {"ok": True, "request_id": args.request_id, "status": "canceled", "remaining_credits": None}


def run_presets(args: argparse.Namespace) -> Dict[str, Any]:
    client = make_client(args)
    if args.size < 1:
        raise AppError("--size must be positive.", kind="validation", exit_code=EXIT_VALIDATION)
    cursor = args.cursor
    all_items: List[Any] = []
    total: Optional[int] = None
    last_cursor: Optional[str] = cursor
    seen_cursors: set[str] = set()
    for page_number in range(1, MAX_PRESET_PAGES + 1):
        if cursor:
            if cursor in seen_cursors:
                raise ApiError("Higgsfield returned a repeated preset pagination cursor.", kind="api")
            seen_cursors.add(cursor)
        query = {"size": args.size}
        if cursor:
            query["cursor"] = cursor
        payload, _ = client.json_request("GET", "/marketing-studio/image/presets?" + urlencode(query), retry_get=True)
        if not isinstance(payload, dict):
            raise ApiError("Higgsfield returned an invalid preset response.", kind="api")
        if isinstance(payload.get("total"), int):
            total = payload["total"]
        items = payload.get("items") or []
        all_items.extend(items)
        last_cursor = payload.get("cursor")
        if not args.all or not last_cursor:
            break
        cursor = last_cursor
    else:
        raise ApiError(f"Preset pagination exceeded the {MAX_PRESET_PAGES}-page safety limit.", kind="api")
    if args.all:
        total = len(all_items)
    if args.search:
        needle = args.search.casefold()
        all_items = [item for item in all_items if needle in json.dumps(item, ensure_ascii=False).casefold()]
    return {"ok": True, "items": all_items, "total": total, "cursor": last_cursor,
            "remaining_credits": None}


def run_credits(args: argparse.Namespace) -> Dict[str, Any]:
    return {"ok": True, "supported": False, "remaining_credits": None,
            "message": "The public Higgsfield API does not document an authenticated account-balance endpoint."}


def emit_success(result: Mapping[str, Any], json_mode: bool) -> None:
    if json_mode:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return
    if result.get("message"):
        print(result["message"])
        return
    if result.get("status"):
        print(f"Status: {result['status']}")
    if result.get("request_id"):
        print(f"Request ID: {result['request_id']}")
    for output in result.get("outputs") or []:
        location = output.get("file") or output.get("url")
        print(f"Output ({output.get('type', 'media')}): {location}")
    if result.get("items") is not None:
        for item in result["items"]:
            print(f"{item.get('id', '')}\t{item.get('name', '')}")


def emit_error(error: AppError, json_mode: bool) -> None:
    if json_mode:
        print(json.dumps(build_error_result(error), ensure_ascii=False, sort_keys=True))
    else:
        print(f"Error: {error.message}", file=sys.stderr)


def sdk_error(exc: BaseException, credentials: Credentials) -> BaseException:
    """Convert official SDK/httpx failures to the CLI's stable error types."""
    chain: List[BaseException] = []
    seen: set[int] = set()
    current: Optional[BaseException] = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        chain.append(current)
        current = getattr(current, "__cause__", None) or getattr(current, "__context__", None)

    response = next((getattr(candidate, "response", None) for candidate in chain
                     if getattr(candidate, "response", None) is not None), None)
    status = getattr(response, "status_code", None)
    if status is not None:
        return error_from_http(
            int(status), getattr(response, "content", b"") or b"", credentials,
            dict(getattr(response, "headers", {}) or {}),
        )
    names = [type(candidate).__name__.lower() for candidate in chain]
    modules = [type(candidate).__module__.lower() for candidate in chain]
    details = redact(str(exc), credentials).strip()
    if any("insufficientcredit" in name for name in names):
        return AppError(details or "Available Higgsfield API credit is insufficient.",
                        kind="credits", exit_code=EXIT_CREDITS)
    if any("credential" in name or "auth" in name or "accessdenied" in name for name in names):
        return AppError("Invalid or missing Higgsfield credentials.",
                        kind="authentication", exit_code=EXIT_AUTHENTICATION)
    if any("sessionbusy" in name or "concurr" in name for name in names) or "concurr" in details.lower():
        return AppError("The account or model concurrency limit has been reached.",
                        kind="concurrency", exit_code=EXIT_CONCURRENCY)
    if any("timeout" in name for name in names):
        return AppError("Network timeout while contacting Higgsfield.",
                        kind="timeout", exit_code=EXIT_TIMEOUT)
    if any(module == "httpx" or module.startswith("httpx.") for module in modules):
        if any(any(token in name for token in ("connect", "network", "protocol", "readerror", "writeerror"))
               for name in names):
            return AppError("Network error while contacting Higgsfield.",
                            kind="network", exit_code=EXIT_NETWORK)
    if any(isinstance(candidate, (TimeoutError, URLError, OSError)) for candidate in chain):
        return AppError("Network error while contacting Higgsfield.",
                        kind="network", exit_code=EXIT_NETWORK)
    message = "Higgsfield SDK request failed."
    if details:
        message += f" Detail: {details}"
    return AppError(message, kind="api", exit_code=EXIT_API)


class SDKClient:
    """Client adapter using only the official higgsfield-client SDK for API calls."""

    def __init__(self, credentials: Credentials, base_url: str = BASE_URL,
                 timeout: float = DEFAULT_HTTP_TIMEOUT,
                 download_timeout: float = DEFAULT_DOWNLOAD_TIMEOUT,
                 max_download_size: int = DEFAULT_MAX_DOWNLOAD_SIZE, **_: Any):
        try:
            import higgsfield_client
        except ImportError as exc:
            raise AppError(
                "The official SDK is not installed. Run: python -m pip install -r requirements-sdk.txt",
                kind="configuration", exit_code=EXIT_VALIDATION,
            ) from exc
        try:
            sdk_version = package_version("higgsfield-client")
        except PackageNotFoundError:
            sdk_version = getattr(higgsfield_client, "__version__", None)
        version_match = re.match(r"^\s*(\d+)\.(\d+)", sdk_version or "")
        if version_match and (version_match.group(1), version_match.group(2)) != ("0", "2"):
            raise AppError(
                f"Unsupported higgsfield-client version {sdk_version}; install {SUPPORTED_SDK_REQUIREMENT}.",
                kind="configuration", exit_code=EXIT_VALIDATION,
            )
        self.credentials = credentials
        self.base_url = validate_base_url(base_url)
        self.timeout = timeout
        self.download_timeout = download_timeout
        if max_download_size < 0:
            raise AppError("--max-download-size must be zero or greater.",
                           kind="validation", exit_code=EXIT_VALIDATION)
        self.max_download_size = max_download_size
        self.sdk_version = sdk_version or "unknown"
        try:
            self.sdk = higgsfield_client.SyncClient(
                base_url=self.base_url,
                timeout=self.timeout,
                api_key=f"{credentials.key_id}:{credentials.secret}",
            )
        except Exception as exc:
            converted = sdk_error(exc, credentials)
            raise converted from exc
        self.controllers: Dict[str, Any] = {}

    def _authenticated_transport(self, operation: str, *, estimate: bool = False) -> Any:
        """Return the SDK transport required by legacy endpoints, with a clear guard."""
        try:
            sdk_http = getattr(self.sdk, "_client", None)
        except Exception as exc:
            sdk_http = None
            transport_error = exc
        else:
            transport_error = None
        if sdk_http is None or not callable(getattr(sdk_http, operation, None)):
            message = (
                f"The installed higgsfield-client ({self.sdk_version}) does not expose the "
                f"authenticated HTTP transport required for {operation}. Install "
                f"{SUPPORTED_SDK_REQUIREMENT} from requirements-sdk.txt, or use a CLI release "
                "compatible with the installed SDK."
            )
            error: AppError
            if estimate:
                error = EstimateUnavailable(message)
            else:
                error = AppError(message, kind="configuration", exit_code=EXIT_VALIDATION)
            if transport_error is not None:
                raise error from transport_error
            raise error
        return sdk_http

    @staticmethod
    def _request_id(path_or_url: str) -> Optional[str]:
        match = re.search(r"/requests/([^/]+)/(?:status|cancel)$", urlparse(path_or_url).path)
        return match.group(1) if match else None

    def _controller(self, request_id: str) -> Any:
        controller = self.controllers.get(request_id)
        if controller is None:
            controller = self.sdk.get_request_controller(request_id)
            self.controllers[request_id] = controller
        return controller

    @staticmethod
    def _status_name(status: Any) -> str:
        names = {
            "Queued": "queued", "InProgress": "in_progress", "Completed": "completed",
            "Failed": "failed", "NSFW": "nsfw", "Cancelled": "canceled",
        }
        name = type(status).__name__
        if name not in names:
            raise ApiError("The SDK returned an unknown request status.", kind="api")
        return names[name]

    @staticmethod
    def _terminal_result(controller: Any) -> Any:
        """Fetch a terminal response without making the controller poll status again."""
        transport = getattr(controller, "_transport", None)
        response_url = getattr(controller, "response_url", None)
        if transport is not None and response_url:
            return transport.request("GET", response_url).json()
        return controller.get()

    def _status_payload(self, controller: Any) -> Dict[str, Any]:
        try:
            status_name = self._status_name(controller.status())
            payload: Dict[str, Any] = {"status": status_name, "request_id": controller.request_id}
            if status_name in TERMINAL_STATUSES:
                result = self._terminal_result(controller)
                if isinstance(result, dict):
                    payload.update(result)
                payload.setdefault("status", status_name)
                payload.setdefault("request_id", controller.request_id)
            return payload
        except AppError:
            raise
        except Exception as exc:
            converted = sdk_error(exc, self.credentials)
            raise converted from exc

    def json_request(self, method: str, path_or_url: str,
                     payload: Optional[Mapping[str, Any]] = None,
                     *, retry_get: bool = False) -> Tuple[Any, Any]:
        method = method.upper()
        path = urlparse(path_or_url).path or path_or_url
        try:
            if method == "POST" and path.startswith("/estimate/"):
                # The public SDK has no estimate method. Use its authenticated
                # transport only for this documented non-generation operation.
                sdk_http = self._authenticated_transport("post", estimate=True)
                response = sdk_http.post(path, json=dict(payload or {}))
                if response.status_code >= 400:
                    raise error_from_http(
                        response.status_code, response.content, self.credentials,
                        dict(response.headers),
                    )
                return response.json(), response

            if method == "POST" and path in MODEL_ENDPOINTS.values():
                controller = self.sdk.submit(path, arguments=dict(payload or {}))
                self.controllers[controller.request_id] = controller
                return {
                    "status": "queued", "request_id": controller.request_id,
                    "status_url": controller.status_url, "cancel_url": controller.cancel_url,
                }, SimpleNamespace(status=202, headers={}, body=b"")

            if method == "GET":
                request_id = self._request_id(path_or_url)
                if request_id:
                    return self._status_payload(self._controller(request_id)), None
                sdk_http = self._authenticated_transport("get")
                response = sdk_http.get(path_or_url)
                if response.status_code >= 400:
                    raise error_from_http(
                        response.status_code, response.content, self.credentials,
                        dict(response.headers),
                    )
                return response.json(), response
        except AppError:
            raise
        except Exception as exc:
            converted = sdk_error(exc, self.credentials)
            raise converted from exc
        raise ApiError(f"Unsupported SDK operation: {method} {path}.", kind="api")

    def request(self, method: str, path_or_url: str, *, body: Optional[bytes] = None,
                headers: Optional[Mapping[str, str]] = None, auth: bool = True,
                retry_get: bool = False) -> Any:
        method = method.upper()
        if method == "POST":
            request_id = self._request_id(path_or_url)
            if request_id and path_or_url.rstrip("/").endswith("/cancel"):
                try:
                    self._controller(request_id).cancel()
                    return SimpleNamespace(status=200, headers={}, body=b"")
                except AppError:
                    raise
                except Exception as exc:
                    converted = sdk_error(exc, self.credentials)
                    raise converted from exc
        raise ApiError(f"Unsupported SDK operation: {method} {path_or_url}.", kind="api")

    def download_file(self, path_or_url: str, target: Path) -> None:
        """Stream a public output to disk with length and size checks."""
        temporary_path: Optional[Path] = None
        try:
            with urlopen(Request(path_or_url, method="GET"), timeout=self.download_timeout) as response:
                content_length: Optional[int] = None
                raw_length = header_value(response.headers, "Content-Length")
                if raw_length:
                    try:
                        content_length = int(raw_length)
                    except ValueError:
                        content_length = None
                if (self.max_download_size and content_length is not None
                        and content_length > self.max_download_size):
                    raise AppError(
                        f"Downloaded output exceeds the {self.max_download_size}-byte size limit.",
                        kind="api", exit_code=EXIT_API,
                    )
                with tempfile.NamedTemporaryFile(
                    mode="wb", dir=target.parent, prefix=f".{target.name}.", suffix=".part", delete=False,
                ) as temporary:
                    temporary_path = Path(temporary.name)
                    total = 0
                    while True:
                        chunk = response.read(DOWNLOAD_CHUNK_SIZE)
                        if not chunk:
                            break
                        total += len(chunk)
                        if self.max_download_size and total > self.max_download_size:
                            raise AppError(
                                f"Downloaded output exceeds the {self.max_download_size}-byte size limit.",
                                kind="api", exit_code=EXIT_API,
                            )
                        temporary.write(chunk)
                if content_length is not None and total != content_length:
                    raise AppError("Downloaded output was truncated.", kind="network", exit_code=EXIT_NETWORK)
            os.replace(temporary_path, target)
            temporary_path = None
        except HTTPError as exc:
            raise ApiError(f"Output download failed with HTTP {exc.code}.", kind="api") from exc
        except AppError:
            raise
        except (URLError, TimeoutError) as exc:
            raise AppError("Network error while downloading output.",
                           kind="network", exit_code=EXIT_NETWORK) from exc
        except OSError as exc:
            raise AppError(f"Cannot write downloaded output {target}: {exc}", kind="api") from exc
        finally:
            if temporary_path is not None:
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError:
                    pass

    def upload_file(self, path: Path) -> str:
        """Upload a path; the SDK derives its MIME type from the validated suffix."""
        try:
            return str(self.sdk.upload_file(path))
        except AppError:
            raise
        except Exception as exc:
            converted = sdk_error(exc, self.credentials)
            raise converted from exc


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = make_parser()
    try:
        args = parser.parse_args(argv)
        normalise_args(args)
        if args.command is None:
            parser.print_help()
            return EXIT_SUCCESS
        if args.command == "image":
            if not getattr(args, "image_model", None):
                raise AppError("The image command requires a model.",
                               kind="validation", exit_code=EXIT_VALIDATION)
            result = run_generation(args, args.image_model, "generate")
        elif args.command == "video":
            if not getattr(args, "video_model", None) or not getattr(args, "workflow", None):
                raise AppError("The video command requires a model and workflow.",
                               kind="validation", exit_code=EXIT_VALIDATION)
            result = run_generation(args, args.video_model, args.workflow)
        elif args.command == "status":
            result = run_status(args)
        elif args.command == "cancel":
            result = run_cancel(args)
        elif args.command == "presets":
            result = run_presets(args)
        elif args.command == "credits":
            result = run_credits(args)
        else:
            raise AppError("Unknown command.", kind="validation", exit_code=EXIT_VALIDATION)
        emit_success(result, bool(getattr(args, "json", False)))
        return EXIT_SUCCESS
    except SystemExit:
        raise
    except KeyboardInterrupt:
        request_id = getattr(locals().get("args"), "request_id", None)
        message = "Interrupted by user."
        if request_id:
            message += f" Request {request_id} may still be running."
        interrupted = AppError(message, kind="interrupted", exit_code=EXIT_INTERRUPTED,
                                request_id=request_id)
        emit_error(interrupted, bool(locals().get("args") and getattr(args, "json", False)))
        return EXIT_INTERRUPTED
    except AppError as error:
        emit_error(error, bool(locals().get("args") and getattr(args, "json", False)))
        return error.exit_code
    except Exception as error:  # Keep unexpected failures structured without leaking secrets.
        message = "Unexpected internal error."
        if bool(locals().get("args") and getattr(args, "debug", False)):
            message = f"Unexpected internal error ({type(error).__name__})."
        app_error = AppError(message, kind="api", exit_code=EXIT_API)
        emit_error(app_error, bool(locals().get("args") and getattr(args, "json", False)))
        return app_error.exit_code


if __name__ == "__main__":
    sys.exit(main())
