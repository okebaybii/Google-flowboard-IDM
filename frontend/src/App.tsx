import { useEffect, useRef } from "react";
import { ReactFlowProvider } from "@xyflow/react";
import { Board } from "./canvas/Board";
import { AddNodePalette } from "./canvas/AddNodePalette";
import { StatusBar } from "./components/StatusBar";
import { Toolbar } from "./components/Toolbar";
// import { ChatSidebar } from "./components/ChatSidebar";
import { ProjectSidebar } from "./components/ProjectSidebar";
import { ReferencesPanel } from "./components/ReferencesPanel";
import { Toaster } from "./components/Toaster";
import { GenerationDialog } from "./components/GenerationDialog";
import { ResultViewer } from "./components/ResultViewer";
import { SocialBlockDialog } from "./components/SocialBlockDialog";
import { ForcedSetupGate } from "./components/ForcedSetupGate";
import { useBoardStore } from "./store/board";
import { useReferencesStore } from "./store/references";
import { useAuthStore } from "./store/auth";
import { LoginScreen } from "./components/LoginScreen";

export function App() {
  const loadInitialBoard = useBoardStore((s) => s.loadInitialBoard);
  const loadReferences = useReferencesStore((s) => s.load);
  const loading = useBoardStore((s) => s.loading);
  const boardId = useBoardStore((s) => s.boardId);
  const ran = useRef(false);

  // Authentication store fields
  const initAuth = useAuthStore((s) => s.init);
  const authUser = useAuthStore((s) => s.user);
  const authLoading = useAuthStore((s) => s.loading);
  const sessionConflict = useAuthStore((s) => s.sessionConflict);
  const logout = useAuthStore((s) => s.logout);
  const token = useAuthStore((s) => s.token);
  const sessionId = useAuthStore((s) => s.sessionId);

  useEffect(() => {
    initAuth();
  }, [initAuth]);

  // Heartbeat polling to detect session conflict
  useEffect(() => {
    if (!authUser || !token || sessionConflict) return;

    const interval = setInterval(async () => {
      try {
        const response = await fetch("/api/auth/heartbeat", {
          headers: {
            "Authorization": `Bearer ${token}`,
            "X-Session-ID": sessionId
          }
        });
        if (response.status === 401) {
          const err = await response.json().catch(() => ({}));
          if (err.detail === "session_conflict") {
            useAuthStore.getState().triggerConflict();
          }
        }
      } catch (err) {
        console.warn("Heartbeat error:", err);
      }
    }, 10000); // 10 seconds

    return () => clearInterval(interval);
  }, [authUser, token, sessionConflict, sessionId]);

  useEffect(() => {
    if (!authUser) return; // Only load board after authenticated
    if (ran.current) return;
    ran.current = true;
    loadInitialBoard();
    // Fire-and-forget: panel renders the loading state inline and the
    // app stays usable even if references fail to hydrate.
    void loadReferences();
  }, [loadInitialBoard, loadReferences, authUser]);

  // 1. Session Conflict Locked Screen (Premium Glassmorphism)
  if (sessionConflict) {
    return (
      <div className="session-conflict-screen" style={{
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        width: "100vw",
        height: "100vh",
        background: "radial-gradient(circle at 50% 50%, #271c19 0%, #09090b 100%)",
        color: "#f4f4f5",
        fontFamily: "Inter, system-ui, sans-serif"
      }}>
        <div style={{
          width: "100%",
          maxWidth: 420,
          padding: "40px 32px",
          background: "rgba(15, 10, 10, 0.7)",
          backdropFilter: "blur(20px)",
          WebkitBackdropFilter: "blur(20px)",
          border: "1px solid rgba(239, 68, 68, 0.25)",
          borderRadius: 16,
          boxShadow: "0 20px 45px rgba(0, 0, 0, 0.6)",
          textAlign: "center",
          display: "flex",
          flexDirection: "column",
          gap: 20
        }}>
          <div style={{ fontSize: 50 }}>⚠️</div>
          <div style={{ fontSize: 22, fontWeight: 800, color: "#f87171" }}>
            Xung đột thiết bị đăng nhập!
          </div>
          <p style={{ fontSize: 13, color: "#d1d5db", lineHeight: 1.6, margin: 0 }}>
            Tài khoản của bạn vừa được đăng nhập trên một thiết bị hoặc phiên trình duyệt khác. 
            Phiên làm việc này đã bị vô hiệu hóa để bảo vệ bảo mật tài khoản.
          </p>
          <button
            onClick={() => logout()}
            style={{
              padding: "12px 24px",
              background: "linear-gradient(135deg, #ef4444 0%, #dc2626 100%)",
              border: "none",
              borderRadius: 8,
              color: "#fff",
              fontSize: 14,
              fontWeight: 600,
              cursor: "pointer",
              transition: "filter 0.15s ease",
              marginTop: 10
            }}
            onMouseEnter={(e) => e.currentTarget.style.filter = "brightness(1.1)"}
            onMouseLeave={(e) => e.currentTarget.style.filter = "none"}
          >
            Đăng nhập lại 🔄
          </button>
        </div>
      </div>
    );
  }

  // 2. Loading state
  if (authLoading) {
    return (
      <div className="app-loading-screen" style={{
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        width: "100vw",
        height: "100vh",
        background: "#09090b",
        color: "#f4f4f5",
        fontFamily: "Inter, system-ui, sans-serif"
      }}>
        <div style={{ display: "flex", flexDirection: "column", alignItems: "center", gap: 16 }}>
          <div className="app-loading-spinner" style={{
            width: 32,
            height: 32,
            border: "4px solid rgba(124, 92, 255, 0.1)",
            borderBottomColor: "#7c5cff",
            borderRadius: "50%",
            animation: "app-loading-spin 1s linear infinite"
          }} />
          <style>{`
            @keyframes app-loading-spin {
              0% { transform: rotate(0deg); }
              100% { transform: rotate(360deg); }
            }
          `}</style>
          <span style={{ fontSize: 13, fontWeight: 500, color: "#9ca3af" }}>Đang tải tài khoản...</span>
        </div>
      </div>
    );
  }

  // 3. Login Redirect
  if (!authUser) {
    return <LoginScreen />;
  }

  return (
    <div className="app">
      <ProjectSidebar />
      <ReactFlowProvider>
        <div className="canvas-wrap">
          <Toolbar />
          {loading && boardId === null ? (
            <div className="canvas-loading">Loading board…</div>
          ) : (
            <>
              <Board />
              <AddNodePalette />
            </>
          )}
          <StatusBar />
          <ReferencesPanel />
        </div>
      </ReactFlowProvider>
      {/* <ChatSidebar /> */}
      <Toaster />
      <GenerationDialog />
      <SocialBlockDialog />
      <ResultViewer />
      <ForcedSetupGate />
    </div>
  );
}
