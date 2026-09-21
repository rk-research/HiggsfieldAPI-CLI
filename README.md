# HiggsfieldAPI-CLI

HiggsfieldAPI-CLI is a compact, SDK-backed command-line client for generating and managing Higgsfield image and video requests. It supports local media uploads, request polling, cancellation, optional cost estimates, preset discovery, safe output downloads, and JSON output for shell scripts and coding agents.

Currently supported models:

- Images: Marketing Studio, Grok Imagine 2, Soul 2, and Ideogram 4.
- Video: Seedance 2.0, Seedance 2.5, Cinema Studio 4.0, and Kling 3.0 Standard.

The exact workflow-specific options are listed in the command help and described in the model sections below.

`HiggsfieldAPI-CLI-sdk.py` is a single-file, Python 3.11+ command-line client for selected Higgsfield image and video APIs. It uses the official `higgsfield-client` package and is designed for terminals, shell scripts, and coding agents. The project is developed and tested with Python 3.14; the filename retains the historical `-sdk` suffix for compatibility.

Copyright (c) 2026 Richard Knuchel. Licensed under the BSD 2-Clause License; see [LICENSE](LICENSE).

## Requirements and credentials

Use Python 3.11 or newer on Windows, macOS, or Linux. The test matrix covers Python 3.11 through 3.14. Install the official package:

```sh
python -m pip install -r requirements-sdk.txt
```

The requirement pins the supported SDK series (`higgsfield-client>=0.2,<0.3`). The examples use `py` on Windows and `python3` elsewhere; use `python` if that is your local command.

Higgsfield provides the API key as one value in the form `ID:SECRET`. Split that value at the separator: put the part before `:` in `HF_API_KEY_ID` and the part after `:` in `HF_API_KEY_SECRET`. Do not search for a separate ID or put the complete `ID:SECRET` value into either variable.

Set the two credential parts with process environment variables:

```sh
export HF_API_KEY_ID="your-key-id"
export HF_API_KEY_SECRET="your-key-secret"
```

PowerShell:

```powershell
$env:HF_API_KEY_ID = "your-key-id"
$env:HF_API_KEY_SECRET = "your-key-secret"
```

Alternatively copy `.env.example` to `.env` (`cp .env.example .env`, or `Copy-Item .env.example .env`) and edit it. By default the CLI resolves this `.env` next to `HiggsfieldAPI-CLI-sdk.py`, independent of the current working directory; use `--env-file PATH` for an intentional alternate file. Process variables override the selected `.env`. `.env` contains secrets and is ignored by Git; `.env.example` has placeholders only. Do not put secrets in scripts unless you understand their exposure risk.

The CLI reads a deliberately small `.env` subset: blank lines, full-line `#` comments, and `NAME=value` assignments. Values may use matching single or double quotes. `export NAME=value` is not recognized and inline comments are not stripped; malformed lines abort the command with a validation error. See `parse_env_file()` in `HiggsfieldAPI-CLI-sdk.py` for the exact parser behavior.

## CLI usage

Start with `py HiggsfieldAPI-CLI-sdk.py -h` (or `python HiggsfieldAPI-CLI-sdk.py -h`). Common options may be placed before the command, after `image`/`video`, or on a workflow leaf: `--env-file FILE`, `--json`, `--no-wait`, `--timeout`, `--poll-interval`, `--http-timeout`, `--download-timeout`, `--max-download-size`, `--max-input-file-size`, `--estimate-only`, `--no-download`, `--output-dir DIR`, `--overwrite`, `--params-json FILE`, and `--debug`. Explicit workflow fields take precedence over imported JSON fields.

Print the current release version with `py HiggsfieldAPI-CLI-sdk.py --version` (or `python HiggsfieldAPI-CLI-sdk.py --version`).

`--http-timeout` controls SDK API calls and uploads (default: 90 seconds). `--download-timeout` controls output downloads separately (default: 300 seconds), so large artifacts have a more practical read-time allowance. Downloads stream to disk and are capped at 2 GiB by default; use `--max-download-size 0` to disable the cap or set another byte limit. All values are configurable per invocation.

Human-readable generation output includes `Request ID: ...`, including for `--no-wait`, so the request can be resumed with the `status` command. `--debug` adds only the unexpected exception class to catch-all diagnostics; it does not print raw exception details or credentials.

```sh
py HiggsfieldAPI-CLI-sdk.py image grok-image-2 --prompt-file prompt.txt --quality medium --resolution 2k --aspect-ratio 16:9
```

The CLI joins `HF_API_KEY_ID` and `HF_API_KEY_SECRET` back into the official SDK's `api_key="key-id:key-secret"` constructor argument. It does not print or persist credentials.

By default, requests use `https://api.higgsfield.ai`. An optional `HF_API_BASE_URL` override is read only from the trusted process environment, not from `.env`; it must be an HTTPS URL with a host and no embedded credentials, query, or fragment. When it differs from the default, the CLI warns that the SDK will send the API key to that endpoint. Set it only for a deliberately trusted compatible service.

### Image workflows

```sh
py HiggsfieldAPI-CLI-sdk.py image marketing-studio --prompt "Product on marble" --resolution 2k
py HiggsfieldAPI-CLI-sdk.py image marketing-studio --prompt "Campaign image" --enhance-prompt --preset-id PRESET_UUID --image ./product.png
py HiggsfieldAPI-CLI-sdk.py image grok-image-2 --prompt "Refine this product" --image ./product.png --quality medium --resolution 2k
py HiggsfieldAPI-CLI-sdk.py image soul2 --prompt "Fashion editorial portrait" --resolution 1080p --batch-size 4
py HiggsfieldAPI-CLI-sdk.py image ideogram4 --prompt "A precise product label" --rendering-speed QUALITY --aspect-ratio 3:2
```

Marketing Studio supports `quality` (`low|medium|high`), `moderation` (`auto|low`), `resolution` (`1k|2k|4k`), all documented aspect ratios, up to 16 editing images, and enhanced mode with one or two images plus `preset_id`. Grok supports `quality` (`low|medium`), `resolution` (`1k|2k`), all documented aspect ratios, and optional repeated `--image` inputs. Soul 2 supports optional `seed`, `style_id`, `batch_size` (`1|4`), `resolution` (`720p|1080p`), aspect ratio, and prompt enhancement. Ideogram 4.0 supports one optional input image, image influence (`1–100`), rendering speed (`TURBO|DEFAULT|QUALITY`), and its documented aspect ratios.

### Seedance workflows

Seedance 2.0:

```sh
py HiggsfieldAPI-CLI-sdk.py video seedance-2 text --prompt "A coastal tracking shot" --duration 5 --resolution 720p
py HiggsfieldAPI-CLI-sdk.py video seedance-2 image --image ./start.jpg --end-image ./end.jpg --prompt "Animate gently"
py HiggsfieldAPI-CLI-sdk.py video seedance-2 reference --prompt "A cinematic transformation" --image-ref a.jpg --video-ref motion.mp4 --audio-ref sound.wav
```

Seedance 2.5:

```sh
py HiggsfieldAPI-CLI-sdk.py video seedance-2.5 text --prompt "A coastal tracking shot" --duration 10 --output-format mp4
py HiggsfieldAPI-CLI-sdk.py video seedance-2.5 image --image ./start.jpg --end-image ./end.jpg
py HiggsfieldAPI-CLI-sdk.py video seedance-2.5 reference --image-ref character.jpg --video-ref motion.mp4
py HiggsfieldAPI-CLI-sdk.py video seedance-2.5 edit --video ./source.mp4 --prompt "Change the lighting"
py HiggsfieldAPI-CLI-sdk.py video seedance-2.5 extend --video ./source.mp4 --prompt "Continue the camera move" --duration 5
```

Seedance 2.0 accepts 4–15 seconds, 480p/720p/1080p/4k, and image/video/audio reference limits of 9/3/3. Seedance 2.5 accepts 4–30 seconds, 480p/720p, `mp4|mov`, and reference limits of 30 images, 10 videos, and 10 audio files. Edit omits duration; extend includes it. Local media is uploaded through the documented presigned URL flow; HTTPS URLs are used directly. Supported local types are JPEG/JPG, PNG, WebP, GIF, MP4, and WAV.

Cinema Studio 4.0 and Kling 3.0 Standard:

```sh
py HiggsfieldAPI-CLI-sdk.py video cinema-studio-4.0 text --prompt "A cinematic tracking shot" --duration 10 --resolution 720p
py HiggsfieldAPI-CLI-sdk.py video cinema-studio-4.0 reference --prompt "A character enters the room" --image-ref ./character.jpg --duration 8
py HiggsfieldAPI-CLI-sdk.py video kling-3.0-standard text --prompt "A coastal tracking shot" --duration 5 --aspect-ratio 16:9
py HiggsfieldAPI-CLI-sdk.py video kling-3.0-standard image --image ./start.jpg --duration 5 --no-generate-audio
```

Cinema Studio 4.0 supports 480p/720p, 16:9, native audio, and `--duration` from 4–30 seconds with up to 30 image/video references. Kling 3.0 Standard supports text-to-video and image-to-video, `--duration` from 3–15 seconds, aspect ratios `16:9|9:16|1:1` for text-to-video, optional sound, CFG scale, multi-shot mode, and an optional last image for image-to-video. The `--duration` help text shows each model's range in seconds.

Prompts can come from `--prompt`, `--prompt-file prompt.txt`, or `--prompt -` for stdin. Conflicting prompt sources are rejected.

`--prompt-file` and `--params-json` intentionally allow arbitrary local paths, including absolute paths and agent-created temporary files, and send their contents to Higgsfield as part of the request. Review those paths carefully when an agent supplies them; do not point them at credentials, private documents, or other sensitive files. Text inputs are limited to 1 MiB by default; use `--max-input-file-size BYTES` or `0` for a deliberate override. `--prompt-file -` reads from stdin and is not a filesystem path.

## Repository layout and maintenance

```text
HiggsfieldAPI-CLI-sdk.py              Supported single-file CLI
test_higgsfield_cli_sdk.py            Deterministic mocked unit tests
requirements-sdk.txt                  Runtime dependency declaration
.codex/.claude/.cline/skills/         Synchronized asset-generation guidance
AGENTS.md                             Repository and contribution conventions
CLAUDE.md                             Claude Code repository pointer
```

Clone and verify the project with:

```sh
git clone <REPOSITORY_URL> HiggsfieldAPI-CLI
cd HiggsfieldAPI-CLI
python -m py_compile HiggsfieldAPI-CLI-sdk.py
python -m unittest -v
```

The repository is maintained as a compact SDK-backed CLI. Changes should preserve JSON stdout purity, keep paid API calls out of tests, update the relevant skill copies together, and include focused tests and documentation for user-visible behavior. See [AGENTS.md](AGENTS.md) before changing the CLI or agent integrations.

### Troubleshooting

- Credentials rejected: confirm the exact `HF_API_KEY_ID` and `HF_API_KEY_SECRET` names, check for whitespace or malformed `.env` lines, and remember that process variables override `.env`. Never print the secret while debugging.
- Estimate returns 404/405: the optional estimate route is unavailable for that model/account. Normal generation warns and continues without an estimate; `--estimate-only` returns `error.type: "estimate_unavailable"` with exit code `12`.
- A request times out locally: do not submit it again. Keep the returned `request_id` and resume with `status REQUEST_ID --watch`.

## Requests, costs, outputs, and agents

Accepted generations return a `request_id`. The CLI polls from two seconds up to ten seconds with jitter until `completed`, `failed`, `nsfw`, or `canceled`. Use `--no-wait`, `status REQUEST_ID`, `status REQUEST_ID --watch`, or `cancel REQUEST_ID` for lifecycle control. Completed output is downloaded by default to the current directory with collision-safe names; use `--no-download`, `--output-dir`, or `--overwrite` to change this.

The documented estimate route (`/estimate` plus the model endpoint) is called before submission when available. If a model/account does not expose that optional route, the CLI warns and continues without an estimate; a real 403 from the generation endpoint still stops with the credit error. `--estimate-only` never submits generation and fails clearly when no estimate is available. Estimates are reported as `estimated_cost`; `charged_cost` remains `null` unless an authoritative response field is supplied. Failed, moderated, and successfully canceled requests are documented as refunded/not charged.

The client uses the official SDK's submit/status/cancel/upload operations. Because the public SDK does not expose a dedicated estimate method, estimate support uses the SDK's authenticated transport when available and otherwise reports the estimate as unavailable without submitting generation. The CLI currently supports the pinned `higgsfield-client>=0.2,<0.3` series and fails early with an actionable configuration error if an incompatible SDK is installed or its private transport is unavailable.

For agents, add `--json`: stdout contains exactly one JSON object and diagnostics go to stderr. The per-command success shapes are:

| Command/result | Stable JSON fields |
| --- | --- |
| `image ...`, `video ...` completed | `ok`, `model`, `workflow`, `request_id`, `status`, `estimated_cost`, `charged_cost`, `remaining_credits`, `outputs` |
| `image ...`, `video ... --no-wait` | `ok`, `model`, `workflow`, `request_id`, `status`, `estimated_cost`, `charged_cost`, `remaining_credits` |
| `image ...`, `video ... --estimate-only` | `ok`, `model`, `workflow`, `status: "estimate_only"`, `request_id: null`, `estimated_cost`, `charged_cost: null`, `remaining_credits` |
| `status [--watch]` | `ok`, `request_id`, `status`, `estimated_cost`, `charged_cost`, `remaining_credits`, `outputs` |
| `cancel` | `ok`, `request_id`, `status: "canceled"`, `remaining_credits` |
| `presets` | `ok`, `items`, `total`, `cursor`, `remaining_credits` |
| `credits` | `ok`, `supported`, `remaining_credits`, `message` |
| Any failed command | `ok: false`, `error.type`, `error.message`; API failures may also include `http_status`, `request_id`, and `correlation_id` |

`remaining_credits` is always `null`; the public API does not document an authenticated account-balance endpoint. `presets` supports `--search`, `--size`, `--cursor`, and `--all`.

Exit codes: `0` success, `2` CLI/input validation or configuration, `3` authentication, `4` insufficient credits, `5` API/unavailable service, `6` generation failure, `7` moderation, `8` polling timeout, `9` network failure, `10` canceled request, `11` concurrency limit, `12` estimate unavailable during `--estimate-only`, `130` interrupted by the user.

Error types are `validation`, `configuration`, `authentication`, `credits`, `api`, `concurrency`, `generation`, `moderation`, `timeout`, `network`, `canceled`, `interrupted`, and `estimate_unavailable`. HTTP 400 responses mentioning concurrency are classified as `concurrency`; HTTP 403 responses are classified as credits only when their detail mentions credit, balance, or quota. Unavailable estimates use `estimate_unavailable`/12 and are distinct from genuine API failures/5; normal generation continues without an estimate.

### Generation failure versus timeout

`Generation failed` means Higgsfield has accepted the request and later returned the terminal API status `failed`. The CLI reports this as `error.type: "generation"` with exit code `6`; the API's `error` detail is preserved when available.

A local wait timeout is different. If the request remains `queued` or `in_progress` longer than `--timeout` (default: 1800 seconds), the CLI stops polling and reports `error.type: "timeout"` with exit code `8`. A network transport timeout is also reported as a timeout, while the server-side generation failure is never caused by the CLI polling deadline. Use `--no-wait` to return the `request_id` immediately, then continue later with `status REQUEST_ID --watch --timeout 3600`. HTTP request timeouts are controlled independently by `--http-timeout` and `--download-timeout`. Pressing Ctrl-C during polling reports `interrupted` with exit code `130` and includes the known request ID.

### Recommended Codex and Claude Code workflow

The most reliable agent interface is `--json` plus a dedicated output directory. Agents should keep credentials in `.env` or the process environment, put long prompts in a UTF-8 file, and read `outputs[].file` from the JSON result instead of parsing human progress text.

For a short direct prompt:

```powershell
py .\HiggsfieldAPI-CLI-sdk.py image grok-image-2 `
  --prompt "Photorealistic panoramic sunset landscape over alpine mountains, warm cinematic light" `
  --quality medium --resolution 2k --aspect-ratio 16:9 `
  --output-dir .\output --json
```

For a longer agent-generated prompt, let Codex or Claude Code write the prompt file and then execute:

```powershell
@'
Photorealistic panoramic sunset landscape over alpine mountains,
golden-hour light, realistic atmospheric haze, natural colors,
wide cinematic composition, no text, no watermark.
'@ | Set-Content -Encoding utf8 .\prompt.txt

py .\HiggsfieldAPI-CLI-sdk.py image grok-image-2 `
  --prompt-file .\prompt.txt --quality medium --resolution 2k `
  --aspect-ratio 16:9 --output-dir .\output --json
```

For long-running video generation, submit without waiting, capture the JSON `request_id`, and ask the agent to resume polling:

```powershell
$job = py .\HiggsfieldAPI-CLI-sdk.py video seedance-2.5 text `
  --prompt-file .\video-prompt.txt --duration 10 --output-format mp4 `
  --output-dir .\output --no-wait --json | ConvertFrom-Json

py .\HiggsfieldAPI-CLI-sdk.py status $job.request_id `
  --watch --timeout 3600 --output-dir .\output --json
```

An effective instruction to either coding agent is: “Use `HiggsfieldAPI-CLI-sdk.py` to create the requested asset, use `--json`, do not expose credentials, wait for completion unless I ask for `--no-wait`, and report the local file path from `outputs[].file` plus the request ID.”

### Reuse the Higgsfield skill in another project

The repository contains one platform-neutral Agent Skill in three identical copies. Keep the contents of `SKILL.md` identical; only the integration path differs between agents. Copy the CLI (and its runtime dependency) into the target project as well. The CLI can be invoked from any working directory; its default `.env` is resolved next to the script.

#### Claude Code

Copy the skill directory to the target project's Claude Code skills directory and add a short pointer to `CLAUDE.md`:

```sh
mkdir -p .claude/skills/higgsfield-asset-generation
cp path/to/HiggsfieldAPI-CLI/.claude/skills/higgsfield-asset-generation/SKILL.md \\
  .claude/skills/higgsfield-asset-generation/SKILL.md
```

```md
# Repository instructions

For Higgsfield image or video requests, use
`.claude/skills/higgsfield-asset-generation/SKILL.md`.
```

#### Codex

Copy the same `SKILL.md` to `.codex/skills/higgsfield-asset-generation/` and add the equivalent repository instruction to `AGENTS.md`:

```sh
mkdir -p .codex/skills/higgsfield-asset-generation
cp path/to/HiggsfieldAPI-CLI/.codex/skills/higgsfield-asset-generation/SKILL.md \\
  .codex/skills/higgsfield-asset-generation/SKILL.md
```

```md
For Higgsfield image or video requests, use
`.codex/skills/higgsfield-asset-generation/SKILL.md`.
```

#### Cline

Cline's recommended project skill directory is `.cline/skills/`. Copy the unchanged skill there; its frontmatter description allows Cline to activate it for matching Higgsfield requests:

```sh
mkdir -p .cline/skills/higgsfield-asset-generation
cp path/to/HiggsfieldAPI-CLI/.cline/skills/higgsfield-asset-generation/SKILL.md \\
  .cline/skills/higgsfield-asset-generation/SKILL.md
```

If the project also uses a Cline rule file, add an explicit pointer such as `.clinerules/higgsfield.md`:

```md
For Higgsfield image or video requests, use
`.cline/skills/higgsfield-asset-generation/SKILL.md`.
```

On Windows, replace `mkdir -p` and `cp` with `New-Item -ItemType Directory -Force` and `Copy-Item`. Cline also supports `.clinerules/skills/` and may detect `.claude/skills/`, but `.cline/skills/` is the reliable recommended project location. Commit the skill and the agent-specific pointer files, but keep `.env` and generated media out of version control. The skill's workflow, credential rules, JSON handling, polling/resume behavior, and output contract remain the same in all three agents.

## Testing and safety

Run the entirely local mocked suite:

```sh
py -m unittest -v
py -m py_compile HiggsfieldAPI-CLI-sdk.py
```

If `py` is unavailable, use `python -m unittest -v` and `python -m py_compile HiggsfieldAPI-CLI-sdk.py`. Tests use mocks, temporary files, fake SDK responses, fake upload URLs, and fake polling responses; they need no account, credentials, Internet, installed SDK, or credits. Live tests are not included, and implementation/final verification performed no paid image or video generation.

## Verified API limitations

The current official docs and model catalog agree on the endpoints and workflows above. The model-catalog code examples show optional Seedance 2.5 `bitrate_mode: "high"`, while the model input tables omit it; the CLI exposes it only as an explicit opt-in. No undocumented values are accepted. The public API does not document a remaining-credit endpoint.
