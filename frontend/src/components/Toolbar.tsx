import { useState, useRef, type KeyboardEvent } from "react";
import { useBoardStore } from "../store/board";
import { useAuthStore } from "../store/auth";
import { ActivityBell } from "./activity/ActivityBell";
import { AiProviderBadge } from "./AiProviderBadge";
import { AdminPanelDialog } from "./AdminPanelDialog";

export function Toolbar() {
  const boardName = useBoardStore((s) => s.boardName);
  const renameBoard = useBoardStore((s) => s.renameBoard);
  const authUser = useAuthStore((s) => s.user);
  const logout = useAuthStore((s) => s.logout);

  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState("");
  const inputRef = useRef<HTMLInputElement>(null);
  const [isAdminOpen, setIsAdminOpen] = useState(false);

  function startEdit() {
    setDraft(boardName);
    setEditing(true);
    requestAnimationFrame(() => inputRef.current?.select());
  }

  function commitEdit() {
    setEditing(false);
    const trimmed = draft.trim();
    if (trimmed && trimmed !== boardName) {
      renameBoard(trimmed);
    }
  }

  function onKeyDown(e: KeyboardEvent<HTMLInputElement>) {
    if (e.key === "Enter") inputRef.current?.blur();
    if (e.key === "Escape") {
      setEditing(false);
    }
  }

  return (
    <div className="toolbar">
      <span className="toolbar-wordmark">Flowboard</span>
      <span className="toolbar-sep" aria-hidden="true">/</span>
      {editing ? (
        <input
          ref={inputRef}
          className="toolbar-name-input"
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onBlur={commitEdit}
          onKeyDown={onKeyDown}
          aria-label="Board name"
        />
      ) : (
        <button
          className="toolbar-name-btn"
          onClick={startEdit}
          aria-label="Rename board"
          title="Click to rename"
        >
          {boardName || "Untitled"}
        </button>
      )}

      <div className="toolbar-actions" style={{ display: "flex", alignItems: "center", gap: 12 }}>
        <ActivityBell />
        <AiProviderBadge />
        {authUser && (
          <div className="toolbar-user-menu" style={{ 
            display: "flex", 
            alignItems: "center", 
            gap: 8, 
            marginLeft: 12,
            borderLeft: "1px solid rgba(255, 255, 255, 0.1)",
            paddingLeft: 12
          }}>
            <span style={{ fontSize: 12, color: "#9ca3af" }} title={authUser.email}>
              {authUser.name || authUser.email}
            </span>
            
            {authUser.is_admin && (
              <button
                onClick={() => setIsAdminOpen(true)}
                style={{
                  background: "rgba(124, 92, 255, 0.12)",
                  border: "1px solid rgba(124, 92, 255, 0.25)",
                  color: "#a78bfa",
                  padding: "4px 10px",
                  borderRadius: 6,
                  fontSize: 11,
                  cursor: "pointer",
                  fontWeight: 600,
                  transition: "all 0.15s ease",
                  display: "flex",
                  alignItems: "center",
                  gap: 4
                }}
                onMouseEnter={(e) => {
                  e.currentTarget.style.background = "rgba(124, 92, 255, 0.25)";
                  e.currentTarget.style.color = "#fff";
                }}
                onMouseLeave={(e) => {
                  e.currentTarget.style.background = "rgba(124, 92, 255, 0.12)";
                  e.currentTarget.style.color = "#a78bfa";
                }}
              >
                Quản lý 🔑
              </button>
            )}

            <button
              onClick={() => logout()}
              style={{
                background: "rgba(239, 68, 68, 0.12)",
                border: "1px solid rgba(239, 68, 68, 0.25)",
                color: "#f87171",
                padding: "4px 10px",
                borderRadius: 6,
                fontSize: 11,
                cursor: "pointer",
                fontWeight: 600,
                transition: "all 0.15s ease",
                display: "flex",
                alignItems: "center",
                gap: 4
              }}
              onMouseEnter={(e) => {
                e.currentTarget.style.background = "rgba(239, 68, 68, 0.25)";
                e.currentTarget.style.color = "#fff";
              }}
              onMouseLeave={(e) => {
                e.currentTarget.style.background = "rgba(239, 68, 68, 0.12)";
                e.currentTarget.style.color = "#f87171";
              }}
            >
              Đăng xuất 🚪
            </button>
          </div>
        )}
      </div>

      <AdminPanelDialog isOpen={isAdminOpen} onClose={() => setIsAdminOpen(false)} />
    </div>
  );
}
