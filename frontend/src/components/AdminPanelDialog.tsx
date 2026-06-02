import { useState, useEffect } from "react";
import { useAuthStore } from "../store/auth";

interface AdminUserItem {
  firebase_uid: string;
  email: string;
  is_approved: boolean;
  is_admin: boolean;
  created_at: string | null;
}

interface AdminPanelDialogProps {
  isOpen: boolean;
  onClose: () => void;
}

export function AdminPanelDialog({ isOpen, onClose }: AdminPanelDialogProps) {
  const token = useAuthStore((s) => s.token);
  const currentUser = useAuthStore((s) => s.user);
  
  const [users, setUsers] = useState<AdminUserItem[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [actionUid, setActionUid] = useState<string | null>(null);

  const fetchUsers = async () => {
    if (!token) return;
    setLoading(true);
    setError(null);
    try {
      const res = await fetch("/api/auth/admin/users", {
        headers: {
          "Authorization": `Bearer ${token}`
        }
      });
      if (!res.ok) {
        throw new Error(await res.text() || "Failed to load users");
      }
      const data = await res.json();
      setUsers(data);
    } catch (err: any) {
      console.error(err);
      setError(err.message || "Không thể tải danh sách tài khoản.");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    if (isOpen) {
      fetchUsers();
    }
  }, [isOpen, token]);

  const handleToggleApprove = async (targetUid: string, currentApproved: boolean) => {
    if (!token) return;
    setActionUid(targetUid);
    try {
      const res = await fetch("/api/auth/admin/approve", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "Authorization": `Bearer ${token}`
        },
        body: JSON.stringify({
          target_uid: targetUid,
          approve: !currentApproved
        })
      });
      if (!res.ok) {
        throw new Error(await res.text() || "Failed to update user");
      }
      await fetchUsers();
    } catch (err: any) {
      alert("Lỗi: " + (err.message || "Không thể cập nhật tài khoản."));
    } finally {
      setActionUid(null);
    }
  };

  if (!isOpen) return null;

  return (
    <div style={{
      position: "fixed",
      inset: 0,
      backgroundColor: "rgba(0, 0, 0, 0.75)",
      backdropFilter: "blur(12px)",
      WebkitBackdropFilter: "blur(12px)",
      display: "flex",
      alignItems: "center",
      justifyContent: "center",
      zIndex: 1000,
      fontFamily: "Inter, system-ui, sans-serif"
    }}>
      <div style={{
        width: "100%",
        maxWidth: 680,
        background: "rgba(20, 20, 25, 0.9)",
        border: "1px solid rgba(255, 255, 255, 0.08)",
        borderRadius: 16,
        padding: 24,
        boxShadow: "0 20px 50px rgba(0, 0, 0, 0.6)",
        display: "flex",
        flexDirection: "column",
        gap: 20
      }}>
        {/* Header */}
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
          <div>
            <h2 style={{ margin: 0, fontSize: 20, fontWeight: 700, color: "#fff" }}>
              🔑 Quản lý người dùng
            </h2>
            <p style={{ margin: "4px 0 0 0", fontSize: 12, color: "#9ca3af" }}>
              Kích hoạt hoặc thu hồi quyền truy cập hệ thống của tài khoản thành viên.
            </p>
          </div>
          <button 
            onClick={onClose}
            style={{
              background: "rgba(255,255,255,0.06)",
              border: "1px solid rgba(255,255,255,0.08)",
              color: "#9ca3af",
              borderRadius: "50%",
              width: 32,
              height: 32,
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
              cursor: "pointer",
              fontSize: 16,
              transition: "all 0.15s ease"
            }}
            onMouseEnter={(e) => { e.currentTarget.style.color = "#fff"; e.currentTarget.style.background = "rgba(255,255,255,0.1)"; }}
            onMouseLeave={(e) => { e.currentTarget.style.color = "#9ca3af"; e.currentTarget.style.background = "rgba(255,255,255,0.06)"; }}
          >
            ✕
          </button>
        </div>

        {/* Content body */}
        <div style={{ maxHeight: 380, overflowY: "auto", paddingRight: 4 }}>
          {loading && users.length === 0 ? (
            <div style={{ textAlign: "center", padding: "40px 0", color: "#9ca3af", fontSize: 13 }}>
              Đang tải danh sách tài khoản...
            </div>
          ) : error ? (
            <div style={{ padding: 12, borderRadius: 8, background: "rgba(239, 68, 68, 0.1)", border: "1px solid rgba(239, 68, 68, 0.2)", color: "#f87171", fontSize: 13, textAlign: "center" }}>
              {error}
            </div>
          ) : users.length === 0 ? (
            <div style={{ textAlign: "center", padding: "40px 0", color: "#6b7280", fontSize: 13 }}>
              Chưa có tài khoản nào được tạo.
            </div>
          ) : (
            <table style={{ width: "100%", borderCollapse: "collapse", textAlign: "left", fontSize: 13 }}>
              <thead>
                <tr style={{ borderBottom: "1px solid rgba(255, 255, 255, 0.08)" }}>
                  <th style={{ padding: "8px 12px", color: "#9ca3af", fontWeight: 600 }}>Tài khoản</th>
                  <th style={{ padding: "8px 12px", color: "#9ca3af", fontWeight: 600 }}>Vai trò</th>
                  <th style={{ padding: "8px 12px", color: "#9ca3af", fontWeight: 600 }}>Trạng thái</th>
                  <th style={{ padding: "8px 12px", color: "#9ca3af", fontWeight: 600, textAlign: "right" }}>Thao tác</th>
                </tr>
              </thead>
              <tbody>
                {users.map((u) => {
                  const isSelf = u.firebase_uid === currentUser?.uid;
                  return (
                    <tr key={u.firebase_uid} style={{ 
                      borderBottom: "1px solid rgba(255, 255, 255, 0.04)",
                      backgroundColor: isSelf ? "rgba(124, 92, 255, 0.03)" : "transparent"
                    }}>
                      <td style={{ padding: "12px 12px" }}>
                        <div style={{ color: "#fff", fontWeight: 500 }}>{u.email}</div>
                        <div style={{ fontSize: 11, color: "#6b7280" }}>UID: {u.firebase_uid}</div>
                      </td>
                      <td style={{ padding: "12px 12px" }}>
                        <span style={{
                          padding: "2px 6px",
                          borderRadius: 4,
                          fontSize: 10,
                          fontWeight: 600,
                          background: u.is_admin ? "rgba(124, 92, 255, 0.15)" : "rgba(255, 255, 255, 0.05)",
                          color: u.is_admin ? "#a78bfa" : "#9ca3af",
                          border: u.is_admin ? "1px solid rgba(124, 92, 255, 0.25)" : "1px solid rgba(255, 255, 255, 0.08)"
                        }}>
                          {u.is_admin ? "Admin 🔑" : "Thành viên"}
                        </span>
                      </td>
                      <td style={{ padding: "12px 12px" }}>
                        <span style={{
                          display: "inline-flex",
                          alignItems: "center",
                          gap: 5,
                          fontSize: 12,
                          color: u.is_approved ? "#34d399" : "#f87171"
                        }}>
                          <span style={{
                            width: 6,
                            height: 6,
                            borderRadius: "50%",
                            background: u.is_approved ? "#34d399" : "#f87171"
                          }} />
                          {u.is_approved ? "Đã kích hoạt" : "Chờ duyệt"}
                        </span>
                      </td>
                      <td style={{ padding: "12px 12px", textAlign: "right" }}>
                        {isSelf ? (
                          <span style={{ fontSize: 11, color: "#6b7280" }}>Đang dùng</span>
                        ) : (
                          <button
                            onClick={() => handleToggleApprove(u.firebase_uid, u.is_approved)}
                            disabled={actionUid === u.firebase_uid}
                            style={{
                              padding: "4px 12px",
                              borderRadius: 6,
                              fontSize: 11,
                              fontWeight: 600,
                              cursor: "pointer",
                              transition: "all 0.15s ease",
                              background: u.is_approved 
                                ? "rgba(239, 68, 68, 0.12)" 
                                : "rgba(52, 211, 153, 0.12)",
                              border: u.is_approved 
                                ? "1px solid rgba(239, 68, 68, 0.2)" 
                                : "1px solid rgba(52, 211, 153, 0.2)",
                              color: u.is_approved ? "#f87171" : "#34d399"
                            }}
                            onMouseEnter={(e) => {
                              e.currentTarget.style.background = u.is_approved 
                                ? "rgba(239, 68, 68, 0.2)" 
                                : "rgba(52, 211, 153, 0.2)";
                            }}
                            onMouseLeave={(e) => {
                              e.currentTarget.style.background = u.is_approved 
                                ? "rgba(239, 68, 68, 0.12)" 
                                : "rgba(52, 211, 153, 0.12)";
                            }}
                          >
                            {actionUid === u.firebase_uid 
                              ? "..." 
                              : u.is_approved 
                                ? "Khóa 🚫" 
                                : "Kích hoạt ✓"
                            }
                          </button>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          )}
        </div>

        {/* Footer */}
        <div style={{ display: "flex", justifyContent: "flex-end", borderTop: "1px solid rgba(255, 255, 255, 0.08)", paddingTop: 16 }}>
          <button
            onClick={fetchUsers}
            style={{
              padding: "8px 16px",
              background: "rgba(255,255,255,0.05)",
              border: "1px solid rgba(255,255,255,0.08)",
              borderRadius: 8,
              color: "#fff",
              fontSize: 13,
              fontWeight: 500,
              cursor: "pointer",
              transition: "all 0.15s ease"
            }}
            onMouseEnter={(e) => e.currentTarget.style.background = "rgba(255,255,255,0.08)"}
            onMouseLeave={(e) => e.currentTarget.style.background = "rgba(255,255,255,0.05)"}
          >
            Làm mới 🔄
          </button>
        </div>
      </div>
    </div>
  );
}
