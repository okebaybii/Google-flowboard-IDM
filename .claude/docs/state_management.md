# State Management (Frontend)

## Store Structure
Frontend state lives in Zustand stores under `frontend/src/store/`, one slice per domain:
- `board` — nodes, edges, selection, canvas viewport (the React Flow graph).
- `generation` — in-flight generation requests and their polling status.
- `pipeline` — multi-step pipeline runs across connected nodes.
- `chat` — AI chat sidebar messages.
- `references` — uploaded character / visual-asset reference nodes.
- `socialBlock` — social-post composition and scheduling state.
- `settings` — AI provider config, members, social accounts.
- `auth` — Firebase auth/session state.

## Data Flow
- The canvas (`canvas/Board.tsx`) reads graph state from the `board` store and dispatches
  mutations on user interaction (add/move/connect nodes).
- API calls go through `api/client.ts`; results update the relevant store, which re-renders
  subscribed components.
- **Derived state**: computed values (e.g. node readiness, pipeline progress) are derived
  from raw store state via selectors — do not duplicate server-owned truth in the client.

## Mutation Rules
- Node/edge mutations should round-trip to the backend (`routes/nodes.py`, `routes/edges.py`)
  so the SQLite graph stays the source of truth; the store mirrors it.
- Generation status is owned by the backend `Request` table; the frontend polls/subscribes
  rather than inventing its own status transitions.
