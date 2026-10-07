# Media Engines Reference: Voice and Images

How self-hosted speech-to-text (STT), text-to-speech (TTS) and image-generation engines
expose their APIs, and what that means for the gateway. Researched 2026-10-07 by reading
each project's route and schema code, not only its README. Items marked *(unverified)*
couldn't be confirmed from code.

The gateway's client contract is **OpenAI's API shape**: `/v1/audio/speech`,
`/v1/audio/transcriptions`, `/v1/audio/translations` and `/v1/images/generations`. Engines
that don't speak it get an adapter. Design decisions live in [DECISIONS.md](DECISIONS.md).

---

## Text-to-speech

| Engine | Status (last commit) | API | Port | Auth | Notes |
|---|---|---|---|---|---|
| **Kokoro-FastAPI** (remsky) | Very active (2026-10-05, v0.9.0) | OpenAI `/v1/audio/speech` + `/v1/audio/voices` | 8880 | none | Best fit. Streams by default (`stream: true`) |
| **vLLM-Omni** | Active (2026-10-07) | OpenAI `/v1/audio/speech`, voices CRUD, WS `/v1/audio/speech/stream` | 8091 | `--api-key` | Qwen3-TTS, CosyVoice3, Voxtral-TTS, Fish S2. One model per server |
| **speaches** | Slowing (2026-04-17) | OpenAI `/v1/audio/speech` | 8000 | optional `API_KEY` | Also serves Kokoro and Piper voices, and STT |
| **Piper** (OHF-Voice/piper1-gpl) | Active (2026-09-28) | `POST /synthesize` (not OpenAI) | 5000 | none | CPU, WAV only, no streaming |
| **Chatterbox-TTS-Server** (devnen) | Active (2026-05) | OpenAI `/v1/audio/speech` + `/tts` | 8004 | none | Voice cloning by reference file |
| **chatterbox-tts-api** (travisvn) | 2025-12 | OpenAI shape, but **ignores `voice`/`speed`**, always WAV | 4123 | none | Prefer devnen |
| **Orpheus-FastAPI** | Slowing (2025-07) | OpenAI shape, WAV only, no streaming | 5005 | none | Needs a second LLM backend (two hops) |
| **xtts-api-server** (Coqui XTTS) | **Stale** (2024-07) | Non-OpenAI | 8020 | none | Not recommended |

### Kokoro knobs (what the dashboard can expose)
- **Voices:** `GET /v1/audio/voices` → `{"voices":[{"id","name",...}], "default_voice":"af_heart"}`.
  72 voice files in the repo (55 standard). Build lists from the endpoint, not a hard-coded set.
- **Language and gender come from the voice ID.** The first letter is the language, the
  second is the gender (`f`/`m`):

  | Prefix | Language | | Prefix | Language |
  |---|---|---|---|---|
  | `a` | American English | | `i` | Italian |
  | `b` | British English | | `j` | Japanese |
  | `e` | Spanish | | `p` | Brazilian Portuguese |
  | `f` | French | | `z` | Mandarin |
  | `h` | Hindi | | | |

  `lang_code` (one of those letters) overrides the language inferred from the voice. Long
  aliases like `en-us` are probably rejected *(unverified)*.
- **Voice blending:** `af_bella(2)+af_sky(1)`. `-` subtracts a voice, and weights are
  normalized to 1 (server setting `VOICE_WEIGHT_NORMALIZATION`).
- **Per-request settings:**
  - `speed` 0.25–4.0 (default 1.0)
  - `response_format` mp3|opus|aac|flac|wav|pcm (default mp3; pcm = 16-bit at 24 kHz)
  - `volume_multiplier` 0–10
  - `normalization_options` (url, email, phone, units, caps, emoji, …)
  - `stream` (default true)
- **Server-only settings:** `DEFAULT_VOICE`, GPU/device, text chunk sizes, `MAX_INPUT_LENGTH`,
  feature kill switches.

### vLLM-Omni TTS knobs
- `voice`, `language` (default `Auto`), `instructions` and `task_type` (CustomVoice, VoiceDesign, Base).
- `speed` 0.25–4.0, `response_format` wav|mp3|flac|pcm|opus, `sample_rate`, `seed`.
- Streaming (`stream_format` audio|sse) needs pcm or wav output and `speed` 1.0.
- Voice cloning upload: `POST /v1/audio/voices` (multipart, ≤10 MB).
- Supported languages come from the model, not an endpoint. For Qwen3-TTS: Auto, Chinese,
  English, Japanese, Korean, German, French, Russian, Portuguese, Spanish, Italian.

### Piper knobs
- **Voices:** `GET /voices` returns `{voice_id: <config incl. language, sample_rate, speakers>}`.
  IDs look like `en_US-lessac-medium`.
- **Per-request:** `voice`, `speaker`, `length_scale` (1.0; higher is *slower*, so it's the
  inverse of speed), `noise_scale` (0.667), `noise_w_scale` (0.8).

---

## Speech-to-text

| Engine | Status | API | Port | Auth | Concurrency |
|---|---|---|---|---|---|
| **vLLM** (`vllm[audio]`) | Active (v0.29) | OpenAI `/v1/audio/transcriptions` and `/translations` | 8000 | `--api-key` | **Continuous batching**; best at volume |
| **speaches** (formerly faster-whisper-server) | Slowing (2026-04) | OpenAI transcriptions and translations, WS realtime | 8000 | optional | Model loads on first use, unloads after TTL |
| **whisper.cpp server** | Active (v1.9.5) | `POST /inference` (path configurable) | 8080 | none | **One request at a time** (global lock) |
| **NVIDIA ASR NIM** | 26.x | OpenAI-shaped transcriptions | 9000 | — | *(unverified: docs blocked)* |
| whisper-asr-webservice | 2026-08 | `POST /asr` (not OpenAI) | 9000 | none | — |

### STT knobs
- **Common to all:** `model`, `language` (omit for auto-detect), `prompt`, `temperature`,
  `response_format` (json|text|verbose_json|srt|vtt), `timestamp_granularities[]`
  (word|segment, with verbose_json).
- **speaches:**
  - Adds `hotwords` and SSE `stream`.
  - **Not settable per request:** beam size and VAD. Silero VAD always runs.
- **vLLM:**
  - Adds `hotwords`, beam search, `top_p`/`top_k`/`seed` and other sampling settings.
  - Streams SSE `transcription.chunk` objects, which are *not* OpenAI's event names.
  - Upload limit `VLLM_MAX_AUDIO_CLIP_FILESIZE_MB` (25).
- **whisper.cpp:**
  - The most settings: `beam_size`, `best_of`, temperature fallback, thresholds, `diarize`,
    and VAD settings (needs `--vad-model` at startup *(unverified)*).
  - **`language` defaults to `en`.** Send `auto` to detect.
  - **Never expose `/load`:** it swaps the model by filesystem path.

---

## Image generation

| Engine | Status | API | Mode | Port | Auth |
|---|---|---|---|---|---|
| **ComfyUI** | Very active (v0.39) | `/prompt` with a workflow graph | **Async queue** + WS progress | 8188 | none |
| **Forge Neo** (active A1111 fork) | Active | `/sdapi/v1/txt2img`, `img2img` | Sync, **one at a time**; poll `/progress` | 7860 | HTTP Basic |
| **SD.Next** | Active | `/sdapi/v1/*` superset | Sync + poll | 7860 | `--auth` |
| **vLLM-Omni** | Active | OpenAI `/v1/images/generations`, `/edits` | Sync | — | `--api-key` |
| **LocalAI** | Active (v4.11) | OpenAI `/v1/images/generations` | Sync | 8080 | Bearer |
| **stable-diffusion.cpp `sd-server`** | Active | **Both** OpenAI and `/sdapi/v1` routes | Sync | — | — |
| **InvokeAI** | Active (v6.14) | Graph queue `/api/v1/queue/.../enqueue_batch` | Async + Socket.IO | 9090 | optional |
| AUTOMATIC1111 / original Forge | **Stale** (2024-07 / 2025-06) | `/sdapi/v1/*` | Sync | 7860 | Basic |
| Ollama | — | **No image generation** (returns 400) | — | — | — |

FLUX runs on ComfyUI (most common), Forge Neo, SD.Next, vLLM-Omni (FLUX.1/.2, Qwen-Image,
Z-Image), LocalAI diffusers and sd.cpp.

### Image knobs and discovery
- **A1111 family (Forge Neo, SD.Next, sd.cpp):**
  - Lists: `/sdapi/v1/sd-models`, `/samplers`, `/schedulers`, `/loras`, `/upscalers`.
  - Per request: `prompt`, `negative_prompt`, `seed` (-1 = random), `steps` (default 50),
    `cfg_scale` (7.0), `distilled_cfg_scale` (FLUX, 3.5), `width`/`height`, `batch_size`,
    `sampler_name`, `scheduler`, hires fix.
  - LoRAs go in the prompt as `<lora:name:weight>`. Switch checkpoint per request with
    `override_settings`; the global `/options` changes it for everyone.
- **ComfyUI:** settings are **node inputs inside a workflow graph**, not flat parameters.
  - `GET /object_info` lists every node's inputs with allowed values: checkpoints, samplers,
    schedulers, LoRAs.
  - `GET /models/{folder}` lists files.
  - The gateway needs **workflow templates** plus a mapping of which node input is
    prompt/seed/steps/size.
- **OpenAI contract:**
  - `prompt`, `model`, `n` (1–10), `size` (`WxH`).
  - Newer models: `quality`, `background`, `output_format`.
  - Returns base64. Streaming partial images via SSE `image_generation.partial_image`.
- **vLLM-Omni / LocalAI extensions:** `negative_prompt`, `num_inference_steps` / `step`,
  `guidance_scale`, `seed`.

---

## Setting up engines behind the gateway
- **Keep engines on a private network.** Most have no authentication (Kokoro, ComfyUI,
  whisper.cpp, Piper); the gateway is the auth boundary. ComfyUI and whisper.cpp bind to
  127.0.0.1 by default.
- **whisper.cpp:**
  - Run one instance per concurrent user; it serializes requests.
  - `--inference-path /v1/audio/transcriptions` makes the path OpenAI-like, but its fields
    still differ.
  - Non-WAV input needs `--convert` (ffmpeg), which its README flags as a security risk.
- **A1111 family:** start with `--api` (or `--nowebui` for API only). It generates one image
  at a time.
- **speaches:** models must be downloaded (`POST /v1/models/{id}`) or preloaded
  (`PRELOAD_MODELS`) before use.
- **vLLM-Omni:** `vllm serve <model> --omni`, one model per server.

## Differences the gateway smooths over
- **Discovery shapes differ.** Voice lists come back as objects, strings, config maps or
  filenames. Languages come from voice prefixes (Kokoro), explicit fields (speaches, Piper)
  or model config (vLLM-Omni). Some engines report nothing, so the gateway keeps its own
  registry.
- **Streaming signals differ:**
  - Kokoro uses `stream` and defaults to on; OpenAI and vLLM-Omni use `stream_format`.
  - STT SSE event names: `transcription.chunk` (vLLM) vs `transcript.text.*` (OpenAI,
    speaches).
- **Format coverage:** several engines are WAV-only.
- **Ranges differ** (Kokoro speed 0.25–4.0, speaches-Kokoro 0.5–2.0), and so do language code
  styles (ISO-639-1 vs BCP-47 on NIM).
- **Some engines silently ignore fields** (`voice`/`speed` on travisvn Chatterbox, `model` on
  whisper.cpp).
- **Images:** step and guidance field names vary, `size` vs `width`/`height`, base64 in JSON vs
  binary `/view`, sync vs async queue, LoRA as prompt syntax vs graph node, and model choice
  per request vs fixed per server.
- **Realtime voice (WebSocket):** every engine has its own event dialect, and sample rates
  differ (OpenAI 24 kHz, vLLM 16 kHz).

## Sources
Kokoro-FastAPI, hexgrad/kokoro, speaches, whisper.cpp `examples/server`, vLLM
`docs/serving/online_serving/speech_to_text.md`, vLLM-Omni `docs/serving/{speech,image_generation}_api.md`,
OHF-Voice/piper1-gpl, devnen/Chatterbox-TTS-Server, travisvn/chatterbox-tts-api,
openai-python 3.26.0 types, ComfyUI `server.py`, AUTOMATIC1111 `modules/api`,
Haoming02/sd-webui-forge-classic (`neo`), vladmandic/sdnext, invoke-ai/InvokeAI,
mudler/LocalAI, ollama/ollama, leejet/stable-diffusion.cpp, NVIDIA Speech NIM docs (search
extracts only). All on GitHub, fetched 2026-10-07.
