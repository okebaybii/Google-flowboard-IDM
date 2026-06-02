import React, { useState } from "react";
import { useAuthStore } from "../store/auth";
import { hasRealFirebase } from "../lib/firebase";

export function LoginScreen() {
  const [isRegister, setIsRegister] = useState(false);
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  
  const login = useAuthStore((s) => s.login);
  const register = useAuthStore((s) => s.register);
  const signInWithGoogle = useAuthStore((s) => s.signInWithGoogle);
  const error = useAuthStore((s) => s.error);
  const loading = useAuthStore((s) => s.loading);
  const clearError = useAuthStore((s) => s.clearError);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!email || !password) return;
    if (isRegister) {
      await register(email, password);
    } else {
      await login(email, password);
    }
  };

  const handleToggleMode = () => {
    setIsRegister(!isRegister);
    clearError();
  };

  return (
    <div className="login-screen" style={{
      display: "flex",
      alignItems: "center",
      justifyContent: "center",
      width: "100vw",
      height: "100vh",
      background: "radial-gradient(circle at 50% 50%, #1e1b4b 0%, #09090b 100%)",
      color: "#f4f4f5",
      fontFamily: "Inter, system-ui, sans-serif"
    }}>
      {/* Background glowing ambient blobs */}
      <div style={{
        position: "absolute",
        width: 300,
        height: 300,
        background: "rgba(124, 58, 237, 0.15)",
        filter: "blur(80px)",
        borderRadius: "50%",
        top: "20%",
        left: "30%",
        pointerEvents: "none"
      }} />
      <div style={{
        position: "absolute",
        width: 350,
        height: 350,
        background: "rgba(99, 102, 241, 0.12)",
        filter: "blur(100px)",
        borderRadius: "50%",
        bottom: "15%",
        right: "25%",
        pointerEvents: "none"
      }} />

      {/* Main glassmorphic card */}
      <div style={{
        width: "100%",
        maxWidth: 400,
        padding: "40px 32px",
        background: "rgba(15, 15, 20, 0.65)",
        backdropFilter: "blur(16px)",
        WebkitBackdropFilter: "blur(16px)",
        border: "1px solid rgba(255, 255, 255, 0.08)",
        borderRadius: 16,
        boxShadow: "0 20px 40px rgba(0, 0, 0, 0.5)",
        zIndex: 10,
        display: "flex",
        flexDirection: "column",
        gap: 24,
        boxSizing: "border-box"
      }}>
        {/* App Title */}
        <div style={{ textAlign: "center", display: "flex", flexDirection: "column", gap: 6 }}>
          <div style={{ fontSize: 28, fontWeight: 800, background: "linear-gradient(135deg, #a78bfa 0%, #6366f1 100%)", WebkitBackgroundClip: "text", WebkitTextFillColor: "transparent" }}>
            🎬 Flowboard IDM
          </div>
          <div style={{ fontSize: 13, color: "#9ca3af", fontWeight: 500 }}>
            Hệ thống biên tập phim AI chuyên nghiệp
          </div>
        </div>

        {/* Development Mock Mode Banner */}
        {!hasRealFirebase && (
          <div style={{
            background: "rgba(245, 158, 11, 0.1)",
            border: "1px solid rgba(245, 158, 11, 0.25)",
            color: "#fbbf24",
            borderRadius: 8,
            padding: "8px 12px",
            fontSize: 11,
            textAlign: "center",
            fontWeight: 500
          }}>
            ⚙️ Mock Auth Mode: Nhập bất kỳ email/pass để kiểm tra!
          </div>
        )}

        {/* Auth Error Message */}
        {error && (
          <div style={{
            background: "rgba(239, 68, 68, 0.1)",
            border: "1px solid rgba(239, 68, 68, 0.2)",
            color: "#f87171",
            borderRadius: 8,
            padding: "10px 12px",
            fontSize: 12,
            textAlign: "center"
          }}>
            ❌ {error}
          </div>
        )}

        {/* Input Form */}
        <form onSubmit={handleSubmit} style={{ display: "flex", flexDirection: "column", gap: 16 }}>
          <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
            <label style={{ fontSize: 12, fontWeight: 600, color: "#9ca3af" }}>Email</label>
            <input
              type="email"
              placeholder="example@domain.com"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              required
              disabled={loading}
              style={{
                width: "100%",
                padding: "12px 14px",
                background: "rgba(255, 255, 255, 0.04)",
                border: "1px solid rgba(255, 255, 255, 0.08)",
                borderRadius: 8,
                color: "#fff",
                fontSize: 14,
                outline: "none",
                transition: "all 0.15s ease",
                boxSizing: "border-box"
              }}
              onFocus={(e) => {
                e.target.style.border = "1px solid #818cf8";
                e.target.style.background = "rgba(255, 255, 255, 0.06)";
              }}
              onBlur={(e) => {
                e.target.style.border = "1px solid rgba(255, 255, 255, 0.08)";
                e.target.style.background = "rgba(255, 255, 255, 0.04)";
              }}
            />
          </div>

          <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
            <label style={{ fontSize: 12, fontWeight: 600, color: "#9ca3af" }}>Mật khẩu</label>
            <input
              type="password"
              placeholder="••••••••"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              required
              disabled={loading}
              style={{
                width: "100%",
                padding: "12px 14px",
                background: "rgba(255, 255, 255, 0.04)",
                border: "1px solid rgba(255, 255, 255, 0.08)",
                borderRadius: 8,
                color: "#fff",
                fontSize: 14,
                outline: "none",
                transition: "all 0.15s ease",
                boxSizing: "border-box"
              }}
              onFocus={(e) => {
                e.target.style.border = "1px solid #818cf8";
                e.target.style.background = "rgba(255, 255, 255, 0.06)";
              }}
              onBlur={(e) => {
                e.target.style.border = "1px solid rgba(255, 255, 255, 0.08)";
                e.target.style.background = "rgba(255, 255, 255, 0.04)";
              }}
            />
          </div>

          {/* Submit button */}
          <button
            type="submit"
            disabled={loading}
            style={{
              width: "100%",
              padding: "12px",
              background: "linear-gradient(135deg, #7c3aed 0%, #4f46e5 100%)",
              border: "none",
              borderRadius: 8,
              color: "#fff",
              fontSize: 14,
              fontWeight: 600,
              cursor: "pointer",
              transition: "transform 0.1s ease, filter 0.15s ease",
              marginTop: 8
            }}
            onMouseEnter={(e) => e.currentTarget.style.filter = "brightness(1.1)"}
            onMouseLeave={(e) => e.currentTarget.style.filter = "none"}
          >
            {loading ? "Đang xử lý..." : isRegister ? "Tạo tài khoản" : "Đăng nhập"}
          </button>
        </form>

        {/* Separator */}
        <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
          <div style={{ flex: 1, height: 1, background: "rgba(255,255,255,0.08)" }} />
          <span style={{ fontSize: 11, color: "#6b7280", fontWeight: 500 }}>Hoặc</span>
          <div style={{ flex: 1, height: 1, background: "rgba(255,255,255,0.08)" }} />
        </div>

        {/* Google Login */}
        <button
          type="button"
          onClick={signInWithGoogle}
          disabled={loading}
          style={{
            width: "100%",
            padding: "12px",
            background: "rgba(255,255,255,0.04)",
            border: "1px solid rgba(255,255,255,0.08)",
            borderRadius: 8,
            color: "#fff",
            fontSize: 14,
            fontWeight: 500,
            cursor: "pointer",
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            gap: 10,
            transition: "background 0.15s ease"
          }}
          onMouseEnter={(e) => e.currentTarget.style.background = "rgba(255,255,255,0.08)"}
          onMouseLeave={(e) => e.currentTarget.style.background = "rgba(255,255,255,0.04)"}
        >
          {/* Custom Google logo */}
          <svg width="18" height="18" viewBox="0 0 24 24">
            <path fill="#4285F4" d="M23.745 12.27c0-.7-.06-1.4-.19-2.07H12v3.927h6.6c-.29 1.5-.14 3.01-.98 4.22l3.41 2.64c2-1.84 3.14-4.55 3.14-7.717z"/>
            <path fill="#34A853" d="M12 24c3.24 0 5.97-1.07 7.96-2.91l-3.41-2.64c-.95.63-2.17 1.01-3.55 1.01-2.73 0-5.04-1.84-5.87-4.31l-3.53 2.73C3.58 21.01 7.5 24 12 24z"/>
            <path fill="#FBBC05" d="M6.13 15.15A7.17 7.17 0 0 1 5.7 12c0-.77.13-1.52.37-2.24l-3.53-2.73C1.65 8.78 1 10.32 1 12c0 1.68.65 3.22 1.54 4.59l3.59-2.74z"/>
            <path fill="#EA4335" d="M12 4.75c1.77 0 3.35.61 4.6 1.8l3.42-3.42C17.96 1.19 15.24 0 12 0 7.5 0 3.58 2.99 1.74 7.03l3.53 2.73c.83-2.47 3.14-4.31 5.87-4.31z"/>
          </svg>
          Đăng nhập bằng Google
        </button>

        {/* Register Toggle */}
        <div style={{ textAlign: "center", fontSize: 13, color: "#9ca3af" }}>
          {isRegister ? "Đã có tài khoản? " : "Chưa có tài khoản? "}
          <button
            type="button"
            onClick={handleToggleMode}
            style={{
              background: "none",
              border: "none",
              color: "#a78bfa",
              fontWeight: 600,
              cursor: "pointer",
              textDecoration: "underline",
              padding: 0
            }}
          >
            {isRegister ? "Đăng nhập ngay" : "Đăng ký ngay"}
          </button>
        </div>
      </div>
    </div>
  );
}
