---
name: higgsfield-asset-generation
description: Generate image and video assets through the repository's Higgsfield SDK CLI when a user asks an agent to create visual media.
---

Copyright (c) 2026 Richard Knuchel. Licensed under the BSD 2-Clause License.

# Higgsfield asset generation

Use this skill when the user asks for a visual asset to be created with Higgsfield, especially wording such as "generate an image for the website", "create a video with Higgsfield", "make a product visual", or "render a short clip". Treat the request as authorization to run the repository CLI, but never expose or request API secrets in command arguments.

## Tool and safety

Run `HiggsfieldAPI-CLI-sdk.py` from any directory. Its default `.env` is resolved next to the CLI script; use the explicit `--env-file` option for another trusted credentials file. Use `--json` for agent calls so stdout contains only machine-readable JSON. Put generated files in a dedicated directory such as `output/` unless the user specifies another location.

Credentials come from `HF_API_KEY_ID` and `HF_API_KEY_SECRET`, either in the process environment or the repository `.env`. `HF_API_BASE_URL` is process-environment-only and must be a trusted HTTPS endpoint because the SDK sends the API key there. Never put credentials in prompts, command arguments, source files, logs, or generated files. Do not submit a paid request merely to test the integration.

For long prompts, write UTF-8 text to a file and use `--prompt-file`; external and temporary paths are allowed. The default input-file limit is 1 MiB; adjust deliberately with `--max-input-file-size`. Use `--prompt -` only when piping a prompt on stdin. Do not combine prompt sources.

Review `--prompt-file` and `--params-json` paths carefully: the CLI reads arbitrary local files and sends their contents to Higgsfield. Never point them at credentials or other sensitive documents.

## Choose the workflow

Use the most specific workflow that matches the request:

- `image grok-image-2`: general image generation or editing. Supports repeated `--image`, quality `low|medium`, resolution `1k|2k`, and aspect ratio `auto|1:1|1:2|2:1|3:2|2:3|4:3|3:4|16:9|9:16`.
- `image marketing-studio`: marketing/product image generation or editing. Supports repeated `--image`, `--preset-id`, `--enhance-prompt`, quality `low|medium|high`, moderation `auto|low`, resolution `1k|2k|4k`, and aspect ratio `auto|1:1|3:2|2:3|4:3|3:4|16:9|9:16|21:9`.
- `image soul2`: Soul 2 image generation. Supports `--seed`, `--style-id`, batch size `1|4`, resolution `720p|1080p`, documented aspect ratios, and `--enhance-prompt`/`--no-enhance-prompt`.
- `image ideogram4`: Ideogram 4.0 generation/editing. Supports one optional `--image`, `--image-weight` from 1-100, documented aspect ratios, and rendering speed `TURBO|DEFAULT|QUALITY`.
- `video seedance-2 text`: text-to-video.
- `video seedance-2 image`: image-to-video; `--image` is required and `--end-image` is optional.
- `video seedance-2 reference`: reference-to-video with repeated `--image-ref`, `--video-ref`, and `--audio-ref`.
- `video seedance-2.5 text`: text-to-video.
- `video seedance-2.5 image`: image-to-video; `--image` is required and `--end-image` is optional.
- `video seedance-2.5 reference`: reference-to-video with repeated image, video, and audio references.
- `video seedance-2.5 edit`: video editing; `--video` is required.
- `video seedance-2.5 extend`: video extension; `--video` is required.
- `video cinema-studio-4.0 text`: cinematic text-to-video with 4-30 second duration, 480p/720p, 16:9, and native audio.
- `video cinema-studio-4.0 reference`: cinematic video with repeated `--image-ref`/`--video-ref` references, up to 30 total.
- `video kling-3.0-standard text`: Kling 3.0 Standard text-to-video with 3-15 second duration, 16:9/9:16/1:1, sound, CFG scale, and multi-shot mode.
- `video kling-3.0-standard image`: Kling 3.0 Standard image-to-video; `--image` is required and `--end-image` is optional.

For video, use the documented options exposed by the selected help page: `--resolution`, `--duration`, `--generate-audio` or `--no-generate-audio`, applicable `--aspect-ratio`, and for Seedance 2.5 `--output-format mp4|mov` and optional `--bitrate-mode high`. The `--duration` help text includes the model-specific range in seconds: Seedance 2.0 accepts 4-15, Seedance 2.5 and Cinema Studio 4.0 accept 4-30, and Kling 3.0 Standard accepts 3-15. Seedance 2.0 reference limits are 9 images, 3 videos, and 3 audio files; Seedance 2.5 limits are 30 images, 10 videos, and 10 audio files.

Local media paths are uploaded automatically through the SDK. HTTPS media URLs are passed through without uploading. The same mechanism covers images, videos, audio, first/end frames, source videos, and references.

## Typical commands

Use one command per request. Examples:

`py HiggsfieldAPI-CLI-sdk.py image grok-image-2 --prompt-file prompt.txt --quality medium --resolution 2k --aspect-ratio 16:9 --output-dir output --json`

`py HiggsfieldAPI-CLI-sdk.py image marketing-studio --prompt "Clean product hero image" --preset-id PRESET_ID --enhance-prompt --output-dir output --json`

`py HiggsfieldAPI-CLI-sdk.py image soul2 --prompt "Fashion editorial portrait" --resolution 1080p --output-dir output --json`

`py HiggsfieldAPI-CLI-sdk.py image ideogram4 --prompt "A precise product label" --rendering-speed QUALITY --output-dir output --json`

`py HiggsfieldAPI-CLI-sdk.py video seedance-2 text --prompt-file prompt.txt --duration 8 --resolution 720p --aspect-ratio 16:9 --output-dir output --json`

`py HiggsfieldAPI-CLI-sdk.py video seedance-2 image --prompt "Animate this scene" --image ./start.jpg --end-image ./end.jpg --duration 6 --output-dir output --json`

`py HiggsfieldAPI-CLI-sdk.py video seedance-2 reference --prompt-file prompt.txt --image-ref ./character.png --video-ref ./motion.mp4 --audio-ref ./sound.wav --output-dir output --json`

`py HiggsfieldAPI-CLI-sdk.py video seedance-2.5 edit --prompt "Replace the background" --video ./source.mp4 --output-format mp4 --output-dir output --json`

`py HiggsfieldAPI-CLI-sdk.py video seedance-2.5 extend --prompt "Continue the motion" --video ./source.mp4 --duration 8 --output-dir output --json`

`py HiggsfieldAPI-CLI-sdk.py video cinema-studio-4.0 text --prompt "A cinematic tracking shot" --duration 10 --output-dir output --json`

`py HiggsfieldAPI-CLI-sdk.py video kling-3.0-standard text --prompt "A coastal tracking shot" --duration 5 --output-dir output --json`

Use `--no-download` only when remote URLs are wanted. By default, completed outputs are downloaded and `outputs[].file` is the preferred path to return to the user. Use `--output-dir` for the requested asset location; files are not silently overwritten.

## Estimates, waiting, and administration

Use `--estimate-only` to validate and estimate without submitting a paid generation. Use `--no-wait` when the caller needs the request ID immediately. For a submitted request, preserve `request_id` from the JSON response and resume with:

`py HiggsfieldAPI-CLI-sdk.py status REQUEST_ID --watch --json`

Use `cancel REQUEST_ID --json` to cancel a queued request. Never repeat the original paid POST after a timeout or ambiguous network failure. Use `presets --json` or `presets --search TERM --all --json` to discover Marketing Studio preset IDs. `credits --json` reports that no documented public balance endpoint is available when applicable.

`--params-json FILE` is an advanced escape hatch for future fields. Explicit CLI options take precedence; it cannot change authentication or the destination endpoint.

## Result handling

Parse the JSON object and return the local file paths from `outputs[].file`, plus `request_id`, `status`, and `estimated_cost` when present. Treat `charged_cost` as authoritative only when the API supplies it; do not relabel an estimate.

Handle `ok: false` without retrying blindly. Distinguish authentication, credits, validation/API errors, generation failure, moderation, timeout, network failure, and cancellation. A timeout is local waiting failure, not proof that generation failed; use the saved request ID with `status --watch`.
