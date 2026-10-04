# Higgsfield MCP Server

Copyright (c) 2026 Richard Knuchel. Licensed under the BSD 2-Clause License; see [LICENSE](../LICENSE).

`HiggsfieldAPI-MCP.py` is a standalone Model Context Protocol server for the Higgsfield workflows already supported by this repository's CLI. It uses the official `higgsfield-client` SDK through the shared implementation in `HiggsfieldAPI-CLI-sdk.py`. The server exposes typed MCP tool schemas and supports Streamable HTTP and stdio.

## Install and start

Python 3.12 or newer is required. Install the dedicated dependency list; it includes the existing SDK requirement:

```sh
python3 -m pip install -r requirements-mcp.txt
python3 HiggsfieldAPI-MCP.py --version
python3 HiggsfieldAPI-MCP.py --transport http --host 127.0.0.1 --port 8765
```

On FreeBSD or Linux, use the Python 3.12+ interpreter provided by the host. PowerShell on Windows:

```powershell
py -m pip install -r requirements-mcp.txt
py .\HiggsfieldAPI-MCP.py --transport http --host 127.0.0.1 --port 8765
```

The default Streamable HTTP URL is `http://127.0.0.1:8765/mcp`. For stdio, configure the MCP client to launch `python3 HiggsfieldAPI-MCP.py --transport stdio` (or `py` on Windows). Do not send diagnostic output to stdout in stdio mode.

Set Higgsfield credentials using the same `HF_API_KEY_ID` and `HF_API_KEY_SECRET` process variables or trusted `.env` file used by the CLI. Never pass provider credentials in MCP tool arguments. `HF_API_BASE_URL`, when used, is process-environment-only and must be an explicitly trusted HTTPS endpoint.

### Environment variables

| Variable | Purpose |
| --- | --- |
| `HF_API_KEY_ID` | Higgsfield API key ID |
| `HF_API_KEY_SECRET` | Higgsfield API key secret |
| `HF_API_BASE_URL` | Optional trusted HTTPS provider-compatible endpoint override; process environment only |
| `HIGGSFIELD_MCP_BEARER_TOKEN` | Default HTTP bearer token; required when binding outside loopback |
| `HF_MCP_LITELLM_SERVER` | LiteLLM MCP server name used to scope callback accounting; defaults to `higgsfield` |
| `HF_MCP_ACCOUNTING_DB` | Optional SQLite accounting ledger path; default is `outputs/mcp/higgsfield-mcp-accounting.sqlite3` beside the MCP source |

The bearer variable name can be changed with `--auth-token-env`. The MCP bearer is read from the process environment only; it is not loaded from `.env`. The SDK reads Higgsfield credentials through its existing environment and `.env` behavior.

## Architecture and behavior

The server imports the CLI module directly and reuses its model endpoint catalog, request builders, validation, credential loading, SDK adapter, status handling, error mapping, and download code. Tool work that calls the synchronous SDK runs in a worker thread so it does not block the MCP event loop. Generation tools submit asynchronously and return a request ID; callers use status/result tools later. The server does not make a cost-estimate request before generation. Estimation happens only when the caller invokes the corresponding estimate tool.

The MCP layer accepts HTTPS media URLs and local paths under configured input roots. It resolves paths before checking them, which also enforces the roots across symlinks. It rejects private/local remote media URLs. Input uploads have the CLI's 1 MiB upload limit; result downloads are size limited and stored under the server output directory. Results identify downloaded files as paths on the server host. A remote MCP client may not be able to read those paths directly.

## Server options

| Option | Default | Description |
| --- | --- | --- |
| `--transport` | `http` | `http` (Streamable HTTP) or `stdio` |
| `--host` | `127.0.0.1` | HTTP bind address |
| `--port` | `8765` | HTTP TCP port |
| `--endpoint` | `/mcp` | Streamable HTTP path |
| `--operation-timeout` | `1800` | Maximum duration for explicit status waiting |
| `--wait-call-timeout` | `40` | Maximum duration of each MCP wait call; configure below the gateway tool timeout |
| `--http-timeout` | CLI default (90s) | Provider SDK request/upload timeout |
| `--download-timeout` | CLI default (300s) | Output read timeout |
| `--max-download-size` | CLI default (2 GiB) | Download byte cap; server caps at 2 GiB |
| `--max-input-file-size` | `1 MiB` | Prompt files and uploaded media cap; configurable up to `2 GiB` |
| `--max-request-body-size` | 2 MiB | Maximum HTTP request body |
| `--poll-interval` | `2` | Initial interval used by a requested wait |
| `--input-dir PATH` | None | Repeatable allowed local input root; local input paths are disabled without this option |
| `--output-dir PATH` | `outputs/mcp` beside the server | Dedicated output directory |
| `--env-file PATH` | CLI-adjacent `.env` | Explicit trusted credential file; must exist and be a regular file when credentials are loaded |
| `--auth-token-env NAME` | `HIGGSFIELD_MCP_BEARER_TOKEN` | Environment variable holding HTTP bearer token |
| `--allowed-host HOST` | Bind host, loopback hostnames, and `host.docker.internal` at the selected port | Repeatable Host header allowlist for HTTP; replaces defaults |
| `--allowed-origin ORIGIN` | None | Repeatable browser Origin allowlist for HTTP |
| `--litellm-costs` | Off | Enable optional LiteLLM cost callback behavior |

HTTP defaults to loopback. Binding to a non-loopback interface requires the configured bearer token environment variable to be set. For remote use, terminate TLS at a trusted reverse proxy, limit network access to the LiteLLM gateway or approved MCP clients, and keep the application bind private to that proxy. The service can submit paid operations and upload files, so do not expose it directly to an untrusted network.

Example enabling local input from one dedicated directory:

```sh
python3 HiggsfieldAPI-MCP.py --transport http --input-dir /srv/higgsfield-input --output-dir /srv/higgsfield-output
```

Windows example:

```powershell
$env:HIGGSFIELD_MCP_BEARER_TOKEN = "replace-with-a-long-random-token"
py .\HiggsfieldAPI-MCP.py --transport http --host 0.0.0.0 --port 8765 --input-dir 'D:\MCP\inputs' --output-dir 'D:\MCP\outputs'
```

Use access controls and firewall rules so only the intended gateway can connect. For containers or FreeBSD jails, ensure the configured input/output directories are mounted and resolved inside the server's allowed filesystem view; a host path is not automatically visible inside the jail/container.

Docker clients can use `http://host.docker.internal:8765/mcp`. The default Host allowlist includes this hostname at the configured port while retaining DNS rebinding protection. Restart the server after updating. For older versions, add `--allowed-host host.docker.internal:8765` to the server command to resolve `421 Misdirected Request` with `Invalid Host header`. Explicit `--allowed-host` options replace the default list; repeat the option for any additional client hostnames.

## Tools

The HTTP endpoint uses stateless Streamable HTTP with JSON responses. Each HTTP request gets an independent MCP transport; no `Mcp-Session-Id` is issued or required. Tool discovery and calls still use MCP JSON-RPC. This avoids reusing closed session channels when a gateway reconnects or sends late notifications. Generation progress remains available through the provider's `request_id` across connections. HTTP session termination and cancellation notifications do not cancel a provider job; use `higgsfield_cancel` explicitly. After updating, restart the server and reconnect the gateway so it refreshes its transport state.

MCP waiting is bounded by `--wait-call-timeout` (40 seconds by default) and `--operation-timeout`, whichever is shorter. A generation still running at this boundary returns `ok: true`, `wait_complete: false`, its latest status, and a `next_action` instruction. Call `higgsfield_wait` again with the same `request_id`; never submit a replacement generation. A slow status lookup also returns successful progress, with `status: null` if no status was retrieved yet. Real provider errors and failed generations remain errors. This avoids gateway failures caused by normal generation durations when the gateway allows 60 seconds per tool call.

Terminal waits return `wait_complete: true`. If `download: true` was requested, a completed wait returns `download_pending: true` and directs the client to `higgsfield_result` for downloading; downloads keep their separate timeout and can still require a larger gateway timeout for large media. Restart the server after changing these options.

The server exposes generation tools, matching estimate tools, and common tools for request and media management. Tool names use `higgsfield_<model>_<workflow>` for generation and `higgsfield_estimate_<model>_<workflow>` for cost estimation; model hyphens become underscores. The MCP schemas expose the relevant typed workflow fields and choices from the CLI, including prompts, model options, and media references.

| Model | Workflow suffixes |
| --- | --- |
| Marketing Studio | `marketing_studio_generate` |
| Grok Image 2 | `grok_image_2_generate` |
| Soul 2 | `soul2_generate` |
| Ideogram 4 | `ideogram4_generate` |
| Seedance 2.0 | `seedance_2_text`, `seedance_2_image`, `seedance_2_reference` |
| Seedance 2.5 | `seedance_2_5_text`, `seedance_2_5_image`, `seedance_2_5_reference`, `seedance_2_5_edit`, `seedance_2_5_extend` |
| Cinema Studio 4.0 | `cinema_studio_4_0_text`, `cinema_studio_4_0_reference` |
| Kling 3.0 Standard | `kling_3_0_standard_text`, `kling_3_0_standard_image` |

The server also provides these common tools:

| Tool | Arguments | Behavior |
| --- | --- | --- |
| `higgsfield_status` | `request_id`, optional `wait`, `download` | Read status; optionally wait for a terminal response and download completed outputs |
| `higgsfield_wait` | `request_id`, optional `download` | Poll for up to 40 seconds by default; return successful progress or terminal status |
| `higgsfield_result` | `request_id`, optional `download` | Retrieve the latest status/result, optionally downloading media |
| `higgsfield_cancel` | `request_id` | Request cancellation of queued work |
| `higgsfield_presets` | optional `search`, `size` (`1..1000`), `cursor`, `all_pages` | List/search Marketing Studio presets and paginate |
| `higgsfield_credits` | none | Report the provider's documented balance endpoint capability (currently unsupported) |
| `higgsfield_upload` | local `path` | Upload one allowed local media file and return its provider URL |

Workflow-specific options follow the existing CLI. For example, image workflows accept `prompt`; image editing workflows also expose optional image inputs. Video schemas include duration, resolution, aspect ratio, audio, and reference inputs where supported. Other model-specific options include Ideogram rendering speed/image weight, Soul seed/style/batch size, Seedance output format/bitrate mode, and Kling CFG/multi-shot settings. Unsupported combinations and out-of-range values are rejected. The exact rules remain in the CLI model builders and are mirrored in generated MCP schemas.

Each generation and estimate tool also accepts optional `advanced_params`, a bounded JSON object for model fields not represented as typed first-class arguments. The server rejects conflicts with typed fields, unsafe field names, non-JSON values, excessive nesting, large text, and non-public/non-HTTPS/local paths. This does not expose arbitrary provider HTTP calls. Media uploads can happen during generation or separately through `higgsfield_upload`.

### Model/workflow coverage

- Marketing Studio image generation and editing by optional image input.
- Grok Imagine 2 image generation and editing by optional image inputs.
- Soul 2 image generation.
- Ideogram 4 image generation and editing by optional image input.
- Seedance 2.0 text-to-video, image-to-video, and reference-to-video.
- Seedance 2.5 text-to-video, image-to-video, reference-to-video, video editing, and video extension.
- Cinema Studio 4.0 text-to-video and reference-to-video.
- Kling 3.0 Standard text-to-video and image-to-video.

The server does not add provider operations that are absent from the CLI. The public Higgsfield API does not document authenticated account balance lookup, so `higgsfield_credits` reports unsupported rather than inventing a balance.

Key typed fields and model limits include:

| Model/workflow | Main typed fields and limits |
| --- | --- |
| Marketing Studio image | `prompt` (up to 5,000 chars), optional `image` list (up to 16); quality `low/medium/high`, moderation `auto/low`, resolution `1k/2k/4k`, documented aspect ratios, and optional preset/enhanced mode (requires one or two images) |
| Grok Image 2 | `prompt`, optional image list; quality `low/medium`, resolution `1k/2k`, documented aspect ratios |
| Soul 2 | `prompt`, optional seed `1..1,000,000`, style ID, batch size `1/4`, resolution `720p/1080p`, documented aspect ratios, prompt enhancement |
| Ideogram 4 | `prompt` length `2..2,048`, optional image and influence `1..100`, speed `TURBO/DEFAULT/QUALITY`, documented aspect ratios |
| Seedance 2.0 video | Duration `4..15` seconds, resolutions `480p/720p/1080p/4k`; reference limits: 9 images, 3 videos, 3 audio files |
| Seedance 2.5 video | Duration `4..30` seconds (video edit omits duration), resolutions `480p/720p`, output `mp4/mov`; reference limits: 30 images, 10 videos, 10 audio files; optional high bitrate mode |
| Cinema Studio 4.0 | Duration `4..30` seconds, `480p/720p`, 16:9, native audio, up to 30 combined image/video references |
| Kling 3.0 Standard | Duration `3..15` seconds, sound, CFG scale `0..1`, optional multi-shot; text aspect ratios `16:9/9:16/1:1`, image workflow supports optional last image |

The SDK's supported local media formats are JPEG/JPG, PNG, WebP, GIF, MP4, and WAV. HTTPS references must point to public media hosts. Tool schemas and CLI validation remain authoritative if provider options change.

## Generation examples

Agent skills for MCP asset workflows are available in `.codex/skills/higgsfield-mcp-asset-generation/SKILL.md`, `.claude/skills/higgsfield-mcp-asset-generation/SKILL.md`, and `.cline/skills/higgsfield-mcp-asset-generation/SKILL.md`. These identical copies cover tool selection, asynchronous generation, timeout recovery, costs, and server-local media paths. Use them for MCP requests; the existing `higgsfield-asset-generation` skills cover CLI requests.

After connecting an MCP client to the server, call the typed workflow tools directly. For example, a Marketing Studio generation submits asynchronously:

```json
{
  "prompt": "A premium skincare bottle on pale stone, soft morning light",
  "quality": "high",
  "resolution": "2k",
  "aspect_ratio": "4:3"
}
```

An image-to-video request supplies a start image and prompt:

```json
{
  "image": "https://media.example.org/start.png",
  "prompt": "Slow camera push toward the product",
  "duration": 5,
  "resolution": "720p"
}
```

For reference-to-video, pass one or more appropriate `image_ref`, `video_ref`, or `audio_ref` inputs as supported by the model. Local paths are accepted only under `--input-dir` roots; HTTPS URLs are supported. Reference count, format, duration, and parameter rules follow the CLI validation. Consult the tool schema for the exact fields offered by a model/workflow.

## Asynchronous lifecycle and media

1. Call a generation tool. It returns `request_id`, queued status, and nullable cost fields promptly.
2. Call `higgsfield_status(request_id=...)` to inspect progress. Set `wait=true` only when the MCP request should remain open until terminal status or timeout.
3. On successful completion, call `higgsfield_result(request_id=..., download=true)` to retrieve output URLs and optionally save the media locally.
4. Reuse the same request ID after a timeout or network interruption. The server does not automatically retry generation by resubmitting. Never submit again just because a prior client call did not return a final status; if the submission failed before returning a request ID, its provider outcome may be ambiguous and a duplicate may incur another charge.

Generated URLs remain in the result even when no download is requested. Downloaded results include server-local file paths under the configured output directory. MCP clients running on another host cannot assume those paths are mounted or readable.

The server checks size limits and path containment before reading local input, including symlink resolution. Local inputs must be regular files in one of the configured roots. It does not provide shell execution or arbitrary provider request tools.

## Cost estimates and actual costs

Each workflow has an explicit `higgsfield_estimate_*` tool. Calling it sends only the documented estimate operation; it never submits a generation. The response identifies an estimate separately in `estimated_cost`. If the estimate endpoint is missing or unavailable, the estimate result indicates unavailability and does not claim zero cost. Generation is allowed independently and is never blocked by optional estimate endpoint availability.

Generation results expose `estimated_cost`, `charged_cost`, and a `cost` object. A charge is treated as authoritative only when it is present in the provider's terminal `charged_cost` field. An explicit USD amount is reportable as USD. A scalar charge is preserved as an amount with unknown currency and is never treated as USD. Credits-only charges are marked confirmed and retained in `credits`, with `actual_cost` and `currency` null; credits cannot be converted to USD or billed as a USD amount. Unknown costs remain null. Estimated costs are never copied into `charged_cost` or submitted as actual expenditure.

Cost metadata contains the request ID, terminal status, actual amount/currency when known, source, confirmation, billability, and accounting status. Failed, moderated, and canceled outcomes are not represented as successful generation. If the provider supplies a charge for a non-success terminal outcome, the raw confirmed provider charge can still be preserved in result metadata; it is not reclassified as a successful generation.

The CLI's response model does not define a stable provider-wide currency schema for `charged_cost`; therefore the MCP server does not infer USD from a numeric field. Provider-side charge details may be unavailable. In that case the result reports `actual_cost: null`, `confirmed: false`, and no LiteLLM cost is assigned. Credits-only charges remain confirmed in provider credits, but have no USD amount and cannot be passed to LiteLLM as a USD cost.

## LiteLLM MCP Gateway and cost accounting

LiteLLM's MCP Gateway connects to the server's Streamable HTTP endpoint. Its documented MCP configuration uses `transport: http` and the server URL. For example, merge this server entry into the gateway's configuration:

```yaml
mcp_servers:
  higgsfield:
    url: "http://127.0.0.1:8765/mcp"
    transport: "http"
    auth_type: "bearer_token"
    auth_value: os.environ/HIGGSFIELD_MCP_BEARER_TOKEN
    description: "Higgsfield generation and request management"
litellm_settings:
  callbacks:
    - HiggsfieldAPI-MCP.higgsfield_cost_tracker
```

Keep both `HiggsfieldAPI-MCP.py` and `HiggsfieldAPI-CLI-sdk.py` beside the LiteLLM configuration on the gateway host: LiteLLM loads the callback from the MCP source, and that source imports the CLI implementation directly. The hyphenated module path is supported by LiteLLM's custom callback loader, which resolves Python files using `spec_from_file_location` ([loader source](https://raw.githubusercontent.com/BerriAI/litellm/main/litellm/proxy/types_utils/utils.py)). The gateway runs in its own Python environment; do not install `requirements-mcp.txt` there or replace its newer MCP SDK with the server's MCP SDK 1.x pin. Install `requirements-mcp.txt` only in the separate Higgsfield MCP server environment. Start the MCP service separately with cost reporting enabled:

```sh
python3 HiggsfieldAPI-MCP.py --transport http --litellm-costs
```

Start the LiteLLM gateway with its normal CLI:

```sh
litellm --config litellm.yaml --num_workers 1
```

The gateway's configured server alias must match `HF_MCP_LITELLM_SERVER` (default `higgsfield`). For correct attribution, configure `HF_MCP_ACCOUNTING_DB` to a persistent SQLite path shared by the LiteLLM gateway workers. By default, the callback writes `outputs/mcp/higgsfield-mcp-accounting.sqlite3` beside the MCP source. The MCP service itself does not write this ledger. Restrict write access to the database. LiteLLM versions may change proxy CLI options; use the gateway's installed version and its current deployment guide.

If LiteLLM is on another host or in another container/jail, use a reachable private service address instead of loopback and restrict it with firewall and proxy policy. Gateway key authentication and upstream MCP authentication are separate layers. Keep the LiteLLM virtual key at the gateway and configure the Higgsfield server's bearer token as upstream authentication where applicable. See the [LiteLLM MCP Gateway guide](https://docs.litellm.ai/docs/mcp) for the current gateway configuration fields and auth options.

LiteLLM's documented dynamic-cost extension is `async_post_mcp_tool_call_hook`: a `CustomLogger` callback receives the call response and assigns `response_obj.hidden_params.response_cost`. Configure that callback under `litellm_settings.callbacks`; a fixed `mcp_server_cost_info` is unsuitable for variable provider charges. See [LiteLLM MCP Cost Tracking](https://docs.litellm.ai/docs/mcp_cost) for the supported callback contract.

When `--litellm-costs` is enabled, the MCP server's integration mode is active. LiteLLM separately loads `HiggsfieldAPI-MCP.higgsfield_cost_tracker` as shown above. Regular MCP operation does not require LiteLLM installed. The callback only supplies a confirmed explicit USD charge and associates it with the originating gateway key where that identity is available; it must never substitute an estimate.

The ledger records an originating key hash under the provider/server pair `(server, request_id)`. Only a later call with that same key identity can claim the confirmed USD charge. A different key cannot claim it. If the submission callback did not receive key context, no origin record exists and the charge is skipped instead of being assigned to the caller that later polls. Repeated status/result retrievals under the origin key do not create another booking. This is at-most-once best effort, not exactly once: a crash after the ledger claim but before LiteLLM persists the charge can lose that booking. Inspect the gateway ledger and spend records before recovery; do not blindly replay an ambiguous charge.

Do not enable LiteLLM's fixed per-call MCP cost entries for these variable Higgsfield operations alongside the dynamic callback, or the same operation may be charged twice. Cost attribution is limited to the request identity/context LiteLLM exposes to the callback; direct MCP clients without LiteLLM context can still receive cost metadata but are not booked to a gateway key.

## Security and deployment

- Store `HF_API_KEY_ID` and `HF_API_KEY_SECRET` in the process environment or a trusted ignored `.env` file. Set `HIGGSFIELD_MCP_BEARER_TOKEN` in the process environment; it is not loaded from `.env`. Do not add credentials to tool schemas, URLs, logs, or source control.
- Keep the MCP HTTP bind on loopback unless a protected reverse proxy or private network requires another bind. Non-loopback binding requires `HIGGSFIELD_MCP_BEARER_TOKEN` by default; configure a different environment variable name with `--auth-token-env` if needed.
- Terminate TLS at a trusted reverse proxy for remote traffic. Use proxy authentication/network restrictions and do not expose the process to public clients.
- Configure only necessary `--input-dir` roots. Remote clients can ask the server to upload files within these roots and can initiate paid generations.
- Treat public media URLs as untrusted input. The server requires HTTPS and rejects local/private addresses; output downloads are size limited.
- Use a dedicated output directory with appropriate filesystem permissions. The returned paths are server local.
- Keep `HF_API_BASE_URL` at the provider default unless an administrator deliberately configures a trusted HTTPS endpoint. The SDK sends the Higgsfield API key to that endpoint.
- Do not share the local accounting ledger between unrelated environments without controlling access and backup behavior.

For an Nginx TLS terminator in front of the loopback server, keep buffering disabled so Streamable HTTP responses can flow while a tool is working. The example assumes Nginx handles TLS and that clients already have the configured upstream bearer token:

```nginx
server {
    listen 443 ssl;
    server_name higgsfield-mcp.example.org;

    location /mcp {
        proxy_pass http://127.0.0.1:8765/mcp;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header Authorization $http_authorization;
        proxy_buffering off;
        proxy_read_timeout 1900s;
        proxy_send_timeout 1900s;
        client_max_body_size 2m;
    }
}
```

Set matching `--allowed-host higgsfield-mcp.example.org` and `--allowed-origin https://higgsfield-mcp.example.org` when clients send those headers. Keep the process bound to loopback and use network-level egress restrictions. Hostname validation alone cannot prevent DNS rebinding or server-side URL fetches to private network addresses.

For FreeBSD jails, Linux containers, and Windows services, verify that process environment variables, trusted certificate stores, input mounts, output directories, and reverse proxy routes are visible to the service account. FreeBSD dependency wheels may be unavailable and may require a compiler toolchain. Final runtime verification was performed on Windows with Python 3.14; Linux and FreeBSD were not runtime tested.

## Zero-cost tests and verification

CI measures line and branch coverage for both Python implementations while running the offline suite. Each Python matrix job prints uncovered lines and branches in its log and uploads XML and browsable HTML reports as a coverage artifact. To reproduce locally:

```sh
python -m pip install -r requirements-dev.txt
python -m coverage run test_offline_suite.py
python -m coverage report
python -m coverage html
```

Open `htmlcov/index.html` to inspect unexercised code. Coverage reporting does not impose a minimum percentage; test failures still fail the CI job.

The offline acceptance suite covers both the CLI and MCP server. Install `requirements-mcp.txt`, then run it with external networking blocked process-wide. The test runner reports the current test count, failures, and skips:

```sh
python test_offline_suite.py
python -m py_compile HiggsfieldAPI-CLI-sdk.py HiggsfieldAPI-MCP.py
```

`python -m unittest -v` is also available for direct suite execution, but `test_offline_suite.py` is the primary zero-network command because it blocks external DNS and sockets before discovering either suite. Windows is the tested runtime platform; Linux and FreeBSD compatibility has been addressed but was not runtime verified.

The tests must patch SDK/provider operations and block unexpected network access. They use synthetic request IDs, URLs, media and charges; no real credentials are required and no generation request is made. Live provider tests are intentionally excluded from automated verification.

## Troubleshooting and limitations

- **Startup says `mcp` is missing:** install `requirements-mcp.txt` using the same interpreter that launches the server.
- **Higgsfield authentication fails:** check the two `HF_API_KEY_*` variables and ensure process environment precedence is understood; do not print secret values.
- **A local media path is rejected:** add its containing directory with `--input-dir`; symlink targets must also resolve within an allowed root.
- **A remote URL is rejected:** use HTTPS to a public host. HTTP, credentials embedded in the URL, local/private addresses, and nonstandard ports are blocked.
- **No actual cost appears:** the provider may omit `charged_cost`, or provide no explicit currency. Estimates are kept separate and cannot fill this field.
- **LiteLLM does not show cost:** ensure the callback is registered in LiteLLM, `--litellm-costs` is enabled, the completed provider response contains an authoritative USD charge, and the callback has originating key context. The startup flag alone does not install a gateway callback.
- **The MCP call timed out:** keep the returned `request_id` and use status/result retrieval. Do not repeat a generation submission.
- **Cancellation was accepted but the request completed:** cancellation applies to queued work and can race with provider execution. Retrieve status after a cancellation request; a cancel response does not prove the provider stopped or refunded the operation.
- **A request failed after submission timed out:** the SDK wrapper does not retry by submitting another paid generation. The outcome may remain uncertain until the same `request_id` can be queried; never submit a duplicate to resolve that uncertainty.
- **Balance lookup returns unsupported:** the public Higgsfield API does not document an authenticated balance endpoint.

The implementation is tested with mocked Higgsfield operations. A mock passing confirms the tested application contract, not live provider or gateway behavior. Verify deployment networking and the current LiteLLM callback API in the target versions before production rollout.
