# Flowboard IDM — Engineering Onboarding

## 1. Project Overview
Flowboard IDM is an infinite-canvas workspace for AI video creation and multi-channel
publishing. Users build a directed graph of nodes — reference characters/assets →
composed images → storyboards → videos (Google Veo 3.1 i2v) → social posts. Image/video
generation is proxied through a Chrome MV3 extension that rides the user's logged-in
Google Flow session. Captions, voice-over and scheduled posting are automated.

## 2. Tech Stack
- **Backend (`agent/`)**: Python 3.11+, FastAPI, SQLModel + SQLite, websockets, uvicorn.
- **Frontend (`frontend/`)**: React 18, TypeScript (strict), Vite, React Flow (`@xyflow/react`),
  Zustand state, Firebase Auth.
- **Extension (`extension/`)**: Chrome Manifest V3 (content / injected / background scripts).
- **AI**: Claude / Gemini / OpenAI Codex via local CLI (not direct API keys).
- **Media**: moviepy + ffmpeg (binary), Edge TTS / gTTS, InsightFace face swap.

## 3. Dev Commands
- Install (Windows): run `install-all.bat`  •  (macOS/Linux): `make install`
- Run everything (Windows): run `start-all.bat`
- Backend only: `cd agent && python -m uvicorn flowboard.main:app --host 127.0.0.1 --port 8101 --reload`
- Frontend only: `cd frontend && npm run dev`  (serves on http://localhost:1234)
- Backend tests: `cd agent && .venv\Scripts\python -m pytest -q`  (Windows)
  / `.venv/bin/python -m pytest -q` (macOS/Linux)
- Frontend type-check: `cd frontend && npm run lint`

## 4. Architecture Summary
```
Chrome MV3 ext  ◄──WS :9223──►  FastAPI agent (127.0.0.1:8101)  ◄──►  SQLite (storage/)
                                          ▲
                                          │ HTTP/WS
                                  React + Vite canvas (127.0.0.1:1234)
```
- `agent/flowboard/main.py` boots FastAPI + 4 background tasks: request worker, extension
  WS server (:9223), social scheduler (every 60s), account-expiry scheduler.
- `routes/` = HTTP endpoints, `services/` = business logic (Flow client, LLM, TTS, vision,
  face swap, pipeline), `worker/` = queue processor + scheduler, `db/` = SQLModel models.
- See `.claude/docs/architecture.md` for detail.

## 5. Key Constraints
- `FLOWBOARD_WS_HOST` MUST stay loopback — the extension WS is unauthenticated by design
  (main.py refuses to boot otherwise).
- Database is configured via `FLOWBOARD_DB` / `FLOWBOARD_STORAGE`, NOT `DATABASE_URL`.
- AI runs through CLI tools on PATH; API keys are stored via the in-app secrets store
  (`FLOWBOARD_SECRETS_PATH`), not committed env files.
- `ffmpeg` must be on PATH for video assembly / audio muxing.
- Requires a paid Google Flow (Pro/Ultra) account for Veo 3.1 i2v + GEM_PIX_2.

## 6. Additional Documentation
- [Architecture Detailed Overview](.claude/docs/architecture.md)
- [State Management Rules](.claude/docs/state_management.md)
- [Generation & Pipeline Logic](.claude/docs/date_logic.md)
- [HUONG_DAN_TAO_PHIM.md](HUONG_DAN_TAO_PHIM.md) — kịch bản, lồng tiếng, đồng bộ video
- [OAUTH_SETUP_GUIDE.md](OAUTH_SETUP_GUIDE.md) — Firebase Auth + device limits
