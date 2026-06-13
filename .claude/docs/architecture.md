# Flowboard IDM Architecture

## Core Principles
Flowboard is split into three independently-running processes that talk over HTTP and
WebSocket on loopback only:
1. **FastAPI agent** (`agent/`) — the brain: REST API, request queue, schedulers, DB.
2. **React + Vite canvas** (`frontend/`) — the infinite-canvas UI (React Flow).
3. **Chrome MV3 extension** (`extension/`) — the bridge to Google Flow that forwards
   image/video generation requests (with reCAPTCHA tokens) through the user's logged-in
   browser session.

```
Chrome MV3 ext  ◄──WS :9223──►  FastAPI agent (127.0.0.1:8101)  ◄──►  SQLite (storage/)
                                          ▲
                                          │ HTTP/WS
                                  React + Vite canvas (127.0.0.1:1234)
```

## Backend Component Structure (`agent/flowboard/`)
- **`main.py`** — app factory + `lifespan`. Boots 4 asyncio background tasks:
  request `worker`, extension `ws_server` (:9223), `social_scheduler` (60s loop),
  `account_expiry_scheduler` (60s loop). Refuses to boot if `WS_HOST` is non-loopback.
- **`config.py`** — all env-driven config (ports, DB paths, planner model, allow-lists).
- **`routes/`** — HTTP endpoints, one module per domain: `boards`, `nodes`, `edges`
  (canvas graph); `media`, `upload`, `references`, `vision`; `prompt`, `llm`, `chat`,
  `plans` (AI); `video_assembly`, `ffmpeg_assembly` (rendering); `social`, `social_block`,
  `oauth` (publishing); `auth`, `firebase_auth`, `requests`, `activity`.
- **`services/`** — business logic: `flow_client` / `flow_sdk` (Google Flow), `llm/`
  (Claude/Gemini/OpenAI via CLI + secrets), `tts`, `vision`, `face_swapper`,
  `prompt_synth`, `pipeline_executor`, `planner`, `platform_poster`, `ws_server`.
- **`worker/`** — `processor` (drains the generation request queue) and
  `social_scheduler` (publishes scheduled posts).
- **`db/`** — SQLModel `models` + `session` over SQLite at `storage/flowboard.db`.

## Request Flow (generation)
1. Frontend creates a node and POSTs a generation request → row in `Request` (status `queued`).
2. The worker picks it up, builds the Flow payload, and pushes it to the extension over WS.
3. The extension calls Google Flow in the browser, then POSTs the result back to
   `/api/ext/callback` (guarded by an HMAC `X-Callback-Secret`).
4. `flow_client.resolve_callback` matches the response to the pending request; the node
   updates and the media is stored.

## Key Constraints
- Loopback-only: the extension WS is unauthenticated; never expose it on the network.
- All AI calls go through CLI tools; do not hardcode provider API keys.
- `ffmpeg` is required on PATH for `ffmpeg_assembly` / `video_assembly`.
