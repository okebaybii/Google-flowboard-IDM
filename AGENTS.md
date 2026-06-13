# Flowboard IDM — Agent Onboarding

## 1. Project Overview
Flowboard IDM is an infinite-canvas workspace for AI video creation and multi-channel
publishing. Users build a directed graph: reference characters/assets → composed images →
storyboards → videos (Google Veo 3.1 i2v) → social posts. Generation is proxied through a
Chrome MV3 extension riding the user's logged-in Google Flow session.

## 2. Tech Stack
- Backend: Python 3.11+, FastAPI, SQLModel + SQLite, websockets (`agent/`)
- Frontend: React 18 + TypeScript + Vite + React Flow + Zustand (`frontend/`)
- Extension: Chrome Manifest V3 (`extension/`)
- AI: Claude / Gemini / OpenAI Codex via local CLI
- Media: moviepy + ffmpeg, Edge TTS / gTTS, InsightFace

## 3. Dev Commands
- Install: `install-all.bat` (Windows) / `make install`
- Run all: `start-all.bat`
- Backend: `cd agent && python -m uvicorn flowboard.main:app --host 127.0.0.1 --port 8101 --reload`
- Frontend: `cd frontend && npm run dev`
- Tests: `cd agent && .venv\Scripts\python -m pytest -q` (Windows) / `.venv/bin/python -m pytest -q`

## 4. Key Constraints
- `FLOWBOARD_WS_HOST` must stay loopback (the extension WS is unauthenticated).
- DB is configured via `FLOWBOARD_DB` / `FLOWBOARD_STORAGE`, not `DATABASE_URL`.
- AI keys live in the in-app secrets store, not committed env files.
- `ffmpeg` must be on PATH for video assembly.
- Keep `.claude/docs` references relative.

## 5. Additional Documentation
- [Architecture Overview](.claude/docs/architecture.md)
- [State Management Rules](.claude/docs/state_management.md)
- [Generation & Pipeline Logic](.claude/docs/date_logic.md)
