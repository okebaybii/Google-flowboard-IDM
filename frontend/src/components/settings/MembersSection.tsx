import { useEffect, useMemo, useState } from "react";
import { useAuthStore } from "../../store/auth";

interface UserRecord {
  firebase_uid: string;
  email: string;
  is_approved: boolean;
  is_admin: boolean;
  expires_at: string | null;
  created_at: string | null;
}

const PAGE_SIZE = 5;

export function MembersSection() {
  const { user: currentUser, getFreshToken } = useAuthStore();
  const [users, setUsers] = useState<UserRecord[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [updatingUid, setUpdatingUid] = useState<string | null>(null);
  const [deletingUid, setDeletingUid] = useState<string | null>(null);
  const [confirmDeleteUid, setConfirmDeleteUid] = useState<string | null>(null);
  const [editingExpiryUid, setEditingExpiryUid] = useState<string | null>(null);
  const [expiryValue, setExpiryValue] = useState("");

  // Search & pagination
  const [search, setSearch] = useState("");
  const [currentPage, setCurrentPage] = useState(1);

  // Filtered list
  const filtered = useMemo(() => {
    if (!search.trim()) return users;
    const q = search.toLowerCase().trim();
    return users.filter(u => u.email.toLowerCase().includes(q));
  }, [users, search]);

  // Pagination
  const totalPages = Math.max(1, Math.ceil(filtered.length / PAGE_SIZE));
  const paged = useMemo(() => {
    const start = (currentPage - 1) * PAGE_SIZE;
    return filtered.slice(start, start + PAGE_SIZE);
  }, [filtered, currentPage]);

  // Reset page when search changes
  useEffect(() => { setCurrentPage(1); }, [search]);

  async function fetchUsers() {
    setLoading(true);
    setError(null);
    try {
      const token = await getFreshToken();
      if (!token) { setError("Không có token xác thực."); setLoading(false); return; }
      const res = await fetch("/api/auth/admin/users", { headers: { Authorization: `Bearer ${token}` } });
      if (!res.ok) throw new Error(`Lỗi tải danh sách: ${res.statusText}`);
      const data = await res.json();
      setUsers(data);
    } catch (err: any) {
      setError(err.message || "Không thể tải danh sách thành viên.");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => { fetchUsers(); }, []);

  async function handleUpdateUser(uid: string, updates: Partial<UserRecord>) {
    setUpdatingUid(uid);
    try {
      const token = await getFreshToken();
      if (!token) throw new Error("Không tìm thấy token.");
      const target = users.find(u => u.firebase_uid === uid);
      if (!target) return;
      const payload = {
        is_approved: updates.is_approved !== undefined ? updates.is_approved : target.is_approved,
        is_admin: updates.is_admin !== undefined ? updates.is_admin : target.is_admin,
        expires_at_iso: updates.expires_at !== undefined ? updates.expires_at : target.expires_at
      };
      const res = await fetch(`/api/auth/admin/users/${uid}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
        body: JSON.stringify(payload)
      });
      if (!res.ok) {
        const errData = await res.json().catch(() => ({}));
        throw new Error(errData.detail || "Không thể cập nhật thành viên.");
      }
      setUsers(prev => prev.map(u => u.firebase_uid === uid ? {
        ...u, is_approved: payload.is_approved, is_admin: payload.is_admin, expires_at: payload.expires_at_iso
      } : u));
      setEditingExpiryUid(null);
    } catch (err: any) {
      alert(`Lỗi: ${err.message}`);
    } finally {
      setUpdatingUid(null);
    }
  }

  async function handleDeleteUser(uid: string) {
    setDeletingUid(uid);
    try {
      const token = await getFreshToken();
      if (!token) throw new Error("Không tìm thấy token.");
      const res = await fetch(`/api/auth/admin/users/${uid}`, {
        method: "DELETE",
        headers: { Authorization: `Bearer ${token}` }
      });
      if (!res.ok) {
        const errData = await res.json().catch(() => ({}));
        throw new Error(errData.detail || "Không thể xóa thành viên.");
      }
      setUsers(prev => prev.filter(u => u.firebase_uid !== uid));
      setConfirmDeleteUid(null);
    } catch (err: any) {
      alert(`Lỗi: ${err.message}`);
    } finally {
      setDeletingUid(null);
    }
  }

  function handleSetQuickExpiry(uid: string, days: number | null) {
    if (days === null) {
      handleUpdateUser(uid, { expires_at: null });
    } else {
      const d = new Date();
      d.setDate(d.getDate() + days);
      handleUpdateUser(uid, { expires_at: d.toISOString() });
    }
  }

  function formatDate(isoStr: string | null) {
    if (!isoStr) return "Vô thời hạn";
    try {
      return new Date(isoStr).toLocaleDateString("vi-VN", {
        year: "numeric", month: "2-digit", day: "2-digit",
        hour: "2-digit", minute: "2-digit"
      });
    } catch { return isoStr; }
  }

  function getInitials(email: string): string {
    const name = email.split("@")[0];
    if (name.length <= 2) return name.toUpperCase();
    return name.slice(0, 2).toUpperCase();
  }

  function getAvatarColor(email: string): string {
    let hash = 0;
    for (let i = 0; i < email.length; i++) hash = email.charCodeAt(i) + ((hash << 5) - hash);
    const hue = Math.abs(hash) % 360;
    return `hsl(${hue}, 55%, 45%)`;
  }

  function getExpiryStatus(expiresAt: string | null): { label: string; cls: string } {
    if (!expiresAt) return { label: "∞ Vĩnh viễn", cls: "ms-expiry--permanent" };
    const diff = new Date(expiresAt).getTime() - Date.now();
    if (diff < 0) return { label: "Đã hết hạn", cls: "ms-expiry--expired" };
    if (diff < 3 * 24 * 3600 * 1000) return { label: `Còn ${Math.ceil(diff / (24 * 3600 * 1000))} ngày`, cls: "ms-expiry--warning" };
    return { label: formatDate(expiresAt), cls: "ms-expiry--ok" };
  }

  function renderPageNumbers() {
    const pages: (number | "...")[] = [];
    if (totalPages <= 7) {
      for (let i = 1; i <= totalPages; i++) pages.push(i);
    } else {
      pages.push(1);
      if (currentPage > 3) pages.push("...");
      for (let i = Math.max(2, currentPage - 1); i <= Math.min(totalPages - 1, currentPage + 1); i++) pages.push(i);
      if (currentPage < totalPages - 2) pages.push("...");
      pages.push(totalPages);
    }
    return pages;
  }

  return (
    <div className="ms">
      {/* Header */}
      <div className="ms__header">
        <div className="ms__header-left">
          <h4 className="ms__title">Quản lý thành viên</h4>
          <span className="ms__count">{filtered.length}/{users.length}</span>
        </div>
        <button type="button" className="ms__refresh" onClick={fetchUsers} disabled={loading}>
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <path d="M21.5 2v6h-6M2.5 22v-6h6M2 11.5a10 10 0 0118.8-4.3M22 12.5a10 10 0 01-18.8 4.3"/>
          </svg>
          {loading ? "Đang tải..." : "Làm mới"}
        </button>
      </div>

      {/* Search bar */}
      <div className="ms__search-wrap">
        <svg className="ms__search-icon" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <circle cx="11" cy="11" r="8"/><path d="M21 21l-4.35-4.35"/>
        </svg>
        <input
          type="text"
          className="ms__search"
          placeholder="Tìm kiếm theo email..."
          value={search}
          onChange={(e) => setSearch(e.target.value)}
        />
        {search && (
          <button type="button" className="ms__search-clear" onClick={() => setSearch("")}>
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M18 6L6 18M6 6l12 12"/></svg>
          </button>
        )}
      </div>

      {error && <div className="ms__error">{error}</div>}

      {loading && users.length === 0 ? (
        <div className="ms__loading">
          <div className="ms__loading-spinner" />
          <span>Đang tải danh sách...</span>
        </div>
      ) : filtered.length === 0 ? (
        <div className="ms__empty">
          <span>Không tìm thấy thành viên nào</span>
          {search && <span className="ms__empty-hint">Thử tìm với từ khóa khác</span>}
        </div>
      ) : (
        <>
          <div className="ms__list">
            {paged.map((u) => {
              const isSelf = u.firebase_uid === currentUser?.uid;
              const isExpired = u.expires_at ? new Date(u.expires_at) < new Date() : false;
              const expiry = getExpiryStatus(u.expires_at);
              const busy = updatingUid === u.firebase_uid;

              return (
                <div key={u.firebase_uid} className={`ms-card${isExpired ? " ms-card--expired" : ""}${isSelf ? " ms-card--self" : ""}`}>
                  {/* Top row: avatar + info + badges */}
                  <div className="ms-card__top">
                    <div className="ms-card__avatar" style={{ background: getAvatarColor(u.email) }}>
                      {getInitials(u.email)}
                    </div>
                    <div className="ms-card__info">
                      <div className="ms-card__email-row">
                        <span className="ms-card__email">{u.email}</span>
                        {isSelf && <span className="ms-tag ms-tag--you">Bạn</span>}
                      </div>
                      <div className="ms-card__meta">
                        <span className="ms-card__date">Đăng ký: {formatDate(u.created_at)}</span>
                        <span className="ms-card__dot">•</span>
                        <span className={`ms-card__expiry ${expiry.cls}`}>{expiry.label}</span>
                      </div>
                    </div>
                    <div className="ms-card__badges">
                      {u.is_admin && <span className="ms-tag ms-tag--admin">Admin</span>}
                      {!u.is_approved && <span className="ms-tag ms-tag--pending">Chờ duyệt</span>}
                      {isExpired && <span className="ms-tag ms-tag--expired">Hết hạn</span>}
                    </div>
                  </div>

                  {/* Controls row */}
                  <div className="ms-card__controls">
                    <div className="ms-card__toggles">
                      <label className={`ms-toggle${isSelf ? " ms-toggle--disabled" : ""}`}>
                        <div className={`ms-toggle__track${u.is_approved ? " ms-toggle__track--on" : ""}`}>
                          <div className="ms-toggle__thumb" />
                          <input
                            type="checkbox"
                            checked={u.is_approved}
                            disabled={isSelf || busy}
                            onChange={(e) => handleUpdateUser(u.firebase_uid, { is_approved: e.target.checked })}
                          />
                        </div>
                        <span className="ms-toggle__label">Kích hoạt</span>
                      </label>

                      <label className={`ms-toggle${isSelf ? " ms-toggle--disabled" : ""}`}>
                        <div className={`ms-toggle__track${u.is_admin ? " ms-toggle__track--on ms-toggle__track--admin" : ""}`}>
                          <div className="ms-toggle__thumb" />
                          <input
                            type="checkbox"
                            checked={u.is_admin}
                            disabled={isSelf || busy}
                            onChange={(e) => handleUpdateUser(u.firebase_uid, { is_admin: e.target.checked })}
                          />
                        </div>
                        <span className="ms-toggle__label">Admin</span>
                      </label>
                    </div>

                    <div className="ms-card__actions">
                      <button
                        type="button"
                        className="ms-btn ms-btn--subtle"
                        onClick={() => {
                          if (editingExpiryUid === u.firebase_uid) {
                            setEditingExpiryUid(null);
                          } else {
                            setEditingExpiryUid(u.firebase_uid);
                            setExpiryValue(u.expires_at ? u.expires_at.slice(0, 16) : "");
                          }
                        }}
                        disabled={busy}
                      >
                        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><rect x="3" y="4" width="18" height="18" rx="2"/><path d="M16 2v4M8 2v4M3 10h18"/></svg>
                        Hạn dùng
                      </button>

                      {confirmDeleteUid === u.firebase_uid ? (
                        <div className="ms-card__confirm">
                          <button
                            type="button"
                            className="ms-btn ms-btn--danger"
                            disabled={deletingUid === u.firebase_uid}
                            onClick={() => handleDeleteUser(u.firebase_uid)}
                          >
                            {deletingUid === u.firebase_uid ? "Đang xóa..." : "Xác nhận xóa"}
                          </button>
                          <button type="button" className="ms-btn ms-btn--subtle" onClick={() => setConfirmDeleteUid(null)}>
                            Hủy
                          </button>
                        </div>
                      ) : (
                        <button
                          type="button"
                          className="ms-btn ms-btn--ghost-danger"
                          disabled={isSelf}
                          onClick={() => setConfirmDeleteUid(u.firebase_uid)}
                          title="Xóa tài khoản và toàn bộ dữ liệu"
                        >
                          <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M3 6h18M19 6v14a2 2 0 01-2 2H7a2 2 0 01-2-2V6M8 6V4a2 2 0 012-2h4a2 2 0 012 2v2"/></svg>
                          Xóa
                        </button>
                      )}
                    </div>
                  </div>

                  {/* Expiry editor (expanded) */}
                  {editingExpiryUid === u.firebase_uid && (
                    <div className="ms-card__expiry-panel">
                      <div className="ms-card__quick-btns">
                        <button type="button" onClick={() => handleSetQuickExpiry(u.firebase_uid, 1)}>+1 ngày</button>
                        <button type="button" onClick={() => handleSetQuickExpiry(u.firebase_uid, 7)}>+1 tuần</button>
                        <button type="button" onClick={() => handleSetQuickExpiry(u.firebase_uid, 30)}>+1 tháng</button>
                        <button type="button" onClick={() => handleSetQuickExpiry(u.firebase_uid, 90)}>+3 tháng</button>
                        <button type="button" className="ms-card__quick-btn--inf" onClick={() => handleSetQuickExpiry(u.firebase_uid, null)}>∞ Vĩnh viễn</button>
                      </div>
                      <div className="ms-card__custom-row">
                        <input
                          type="datetime-local"
                          value={expiryValue}
                          onChange={(e) => setExpiryValue(e.target.value)}
                        />
                        <button
                          type="button"
                          className="ms-btn ms-btn--primary"
                          onClick={() => {
                            if (!expiryValue) {
                              handleUpdateUser(u.firebase_uid, { expires_at: null });
                            } else {
                              handleUpdateUser(u.firebase_uid, { expires_at: new Date(expiryValue).toISOString() });
                            }
                          }}
                        >
                          Lưu
                        </button>
                      </div>
                    </div>
                  )}
                </div>
              );
            })}
          </div>

          {/* Pagination */}
          {totalPages > 1 && (
            <div className="ms__pagination">
              <button
                type="button"
                className="ms__page-btn ms__page-btn--arrow"
                disabled={currentPage <= 1}
                onClick={() => setCurrentPage(p => Math.max(1, p - 1))}
              >
                <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M15 18l-6-6 6-6"/></svg>
              </button>

              {renderPageNumbers().map((p, i) =>
                p === "..." ? (
                  <span key={`dots-${i}`} className="ms__page-dots">…</span>
                ) : (
                  <button
                    key={p}
                    type="button"
                    className={`ms__page-btn${currentPage === p ? " ms__page-btn--active" : ""}`}
                    onClick={() => setCurrentPage(p as number)}
                  >
                    {p}
                  </button>
                )
              )}

              <button
                type="button"
                className="ms__page-btn ms__page-btn--arrow"
                disabled={currentPage >= totalPages}
                onClick={() => setCurrentPage(p => Math.min(totalPages, p + 1))}
              >
                <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M9 18l6-6-6-6"/></svg>
              </button>

              <span className="ms__page-info">Trang {currentPage}/{totalPages}</span>
            </div>
          )}
        </>
      )}
    </div>
  );
}
