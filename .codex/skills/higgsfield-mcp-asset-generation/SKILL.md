---
name: higgsfield-mcp-asset-generation
description: Generate and manage Higgsfield image and video assets through a connected Higgsfield MCP server when the user requests the MCP workflow.
---

Copyright (c) 2026 Richard Knuchel. Licensed under the BSD 2-Clause License.

# Higgsfield MCP asset generation

Use this skill for visual asset requests through a connected Higgsfield MCP server. Call its tools using their advertised schemas. This skill requires only an MCP connection; it does not require a source checkout, local scripts, or another skill. If the connection is unavailable, report that and ask for the server to be connected through the client's MCP configuration. Do not silently switch a requested MCP workflow to another paid generation interface.

## Connection and safety

Use the MCP connection already configured in the client. The server may be local or remote; do not assume a particular URL, installation path, transport, or filesystem layout. Discover the available Higgsfield tools before selecting a workflow. Server administrators configure allowed input directories and the output directory; tool calls cannot change them.

Provider credentials are managed by the server, and MCP connection authentication is managed by the client. Never send credentials in tool arguments, prompts, media, or logs. Report authentication failures so the connection or server credentials can be corrected through their configuration. Do not perform live paid generation to test connectivity; discover tools instead.

## Choose the workflow

Discover the connected server's tools and inspect the selected schema before calling it. Generation tools are named `higgsfield_<model>_<workflow>`; matching estimates are `higgsfield_estimate_<model>_<workflow>`. Client interfaces may add a server prefix to these names.

| Model | Tool suffixes |
| --- | --- |
| Marketing Studio | `marketing_studio_generate` |
| Grok Imagine 2 | `grok_image_2_generate` |
| Soul 2 | `soul2_generate` |
| Ideogram 4 | `ideogram4_generate` |
| Seedance 2.0 | `seedance_2_text`, `seedance_2_image`, `seedance_2_reference` |
| Seedance 2.5 | `seedance_2_5_text`, `seedance_2_5_image`, `seedance_2_5_reference`, `seedance_2_5_edit`, `seedance_2_5_extend` |
| Cinema Studio 4.0 | `cinema_studio_4_0_text`, `cinema_studio_4_0_reference` |
| Kling 3.0 Standard | `kling_3_0_standard_text`, `kling_3_0_standard_image` |

Use `text` for text-to-video, `image` for start-image animation, `reference` for supported reference media, `edit` for modifying an existing video, and `extend` for continuing it. Image editing uses optional image inputs on the relevant image generation tool. Schema choices and validation define supported resolutions, durations, aspect ratios, audio, and model options; do not invent unsupported parameters.

Pass prompts directly as `prompt` strings. Use only arguments exposed by the connected tool's schema. Use optional `advanced_params`, when advertised, only for necessary model fields absent from the typed schema; it cannot override typed fields, authentication, or endpoints.

## Media and examples

Local media paths refer to the server's filesystem and must be regular files within its configured input roots. A client-local file is not automatically available to a remote server. Use public HTTPS media URLs or arrange an authorized transfer into an allowed server directory. `higgsfield_upload(path=...)` uploads an allowed server-local file and returns a provider URL. It cannot upload an arbitrary client-local path.

Example arguments for `higgsfield_grok_image_2_generate`:

```json
{"prompt": "A premium skincare bottle on pale stone, soft morning light", "quality": "medium", "resolution": "2k", "aspect_ratio": "4:3"}
```

Example arguments for `higgsfield_seedance_2_5_image`:

```json
{"image": "https://media.example.org/start.png", "prompt": "Slow camera push toward the product", "duration": 5, "resolution": "720p"}
```

Replace example media URLs with actual authorized inputs. `higgsfield_presets` discovers Marketing Studio presets; use its search and pagination fields as needed. `higgsfield_credits` reports balance capability, not an invented account balance.

## Estimates and asynchronous lifecycle

Call the matching estimate tool when an estimate is requested or needed before submission. Estimates do not submit generation. Generation tools submit paid work asynchronously without automatically estimating; an unavailable estimate does not mean zero cost.

Preserve the returned `request_id` immediately. Use `higgsfield_status(request_id=...)` to inspect progress or `higgsfield_wait(request_id=...)` for a bounded wait. An unfinished wait can return `ok: true` with progress; inspect `status` and `next_action` before treating it as complete. On completion, use `higgsfield_result(request_id=..., download=true)` to retrieve and download outputs. Use `higgsfield_cancel` when cancellation is requested.

Never resubmit the original generation after a timeout or ambiguous network failure. Resume with the saved request ID. If submission failed before returning an ID, report the ambiguous outcome instead of automatically repeating a potentially charged request.

## Result handling

Read the structured tool result and check `ok`, `status`, and any error fields. Distinguish validation, authentication, credits, provider failures, moderation, cancellation, and waiting/transport failures. A waiting timeout is not proof that generation failed.

Return `request_id`, terminal status, and downloaded `outputs[].file` paths when available. These paths are server-local; identify them as such when the client is remote, and return output URLs when local access is unavailable or downloads were not requested. Do not claim a remote server path exists on the user's machine.

Keep `estimated_cost` separate from `charged_cost` and confirmed cost metadata. Unknown amounts remain unknown; scalar charges do not imply USD, and provider credits cannot be converted to USD without an authoritative conversion. If a gateway tracks spending, report the accounting metadata returned by the tools without assuming accounting is enabled or changing gateway configuration.
