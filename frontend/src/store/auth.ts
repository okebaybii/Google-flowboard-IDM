import { create } from "zustand";
import { 
  signInWithEmailAndPassword, 
  createUserWithEmailAndPassword, 
  signOut, 
  GoogleAuthProvider, 
  signInWithPopup,
  onAuthStateChanged,
  sendEmailVerification 
} from "firebase/auth";
import { firebaseAuth, hasRealFirebase } from "../lib/firebase";

function generateUUID() {
  return "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, (c) => {
    const r = (Math.random() * 16) | 0;
    const v = c === "x" ? r : (r & 0x3) | 0x8;
    return v.toString(16);
  });
}

// Retrieve or generate a session ID for this browser tab session
let cachedSessionId = sessionStorage.getItem("flowboard_session_id");
if (!cachedSessionId) {
  cachedSessionId = generateUUID();
  sessionStorage.setItem("flowboard_session_id", cachedSessionId);
}

interface UserProfile {
  uid: string;
  email: string;
  name?: string;
  photoURL?: string;
  is_admin?: boolean;
  is_approved?: boolean;
}

interface AuthState {
  user: UserProfile | null;
  token: string | null;
  sessionId: string;
  loading: boolean;
  sessionConflict: boolean;
  error: string | null;
  
  init: () => Promise<void>;
  login: (email: string, password: string) => Promise<void>;
  register: (email: string, password: string) => Promise<void>;
  signInWithGoogle: () => Promise<void>;
  logout: () => Promise<void>;
  registerSessionOnBackend: (idToken: string) => Promise<any>;
  triggerConflict: () => void;
  clearError: () => void;
}

export const useAuthStore = create<AuthState>((set, get) => {
  
  async function registerSessionOnBackend(idToken: string): Promise<any> {
    const sessId = get().sessionId;
    try {
      const response = await fetch("/api/auth/register-session", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ id_token: idToken, session_id: sessId })
      });
      if (response.ok) {
        return await response.json();
      }
      
      const errData = await response.json().catch(() => ({}));
      if (response.status === 403 && errData.detail === "account_not_approved") {
        set({ error: "Tài khoản của bạn đang chờ Admin kích hoạt duyệt. Vui lòng liên hệ quản trị viên!" });
      } else if (errData.detail === "session_conflict") {
        set({ sessionConflict: true });
      } else {
        set({ error: errData.detail || "Đăng nhập thất bại." });
      }
      return null;
    } catch (err) {
      console.error("Failed to register session on backend:", err);
      set({ error: "Không thể kết nối tới máy chủ xác thực." });
      return null;
    }
  }

  return {
    user: null,
    token: null,
    sessionId: cachedSessionId,
    loading: true,
    sessionConflict: false,
    error: null,

    init: async () => {
      set({ loading: true });
      if (hasRealFirebase && firebaseAuth) {
        onAuthStateChanged(firebaseAuth, async (fbUser) => {
          if (fbUser) {
            if (!fbUser.emailVerified) {
              await signOut(firebaseAuth);
              set({ 
                user: null, 
                token: null, 
                loading: false, 
                error: "Tài khoản của bạn chưa được kích hoạt. Vui lòng xác thực email trong hộp thư để tiếp tục." 
              });
              return;
            }
            try {
              const token = await fbUser.getIdToken();
              const backendData = await registerSessionOnBackend(token);
              if (backendData && backendData.ok) {
                set({
                  user: {
                    uid: fbUser.uid,
                    email: fbUser.email || "",
                    name: fbUser.displayName || undefined,
                    photoURL: fbUser.photoURL || undefined,
                    is_admin: backendData.is_admin,
                    is_approved: backendData.is_approved
                  },
                  token,
                  sessionConflict: false,
                  loading: false,
                });
              } else {
                await signOut(firebaseAuth);
                set({ user: null, token: null, loading: false });
              }
            } catch (err: any) {
              set({ error: err.message, loading: false });
            }
          } else {
            set({ user: null, token: null, loading: false, sessionConflict: false });
          }
        });
      } else {
        // Mock Auth Fallback
        const mockUserStr = localStorage.getItem("flowboard_mock_user");
        if (mockUserStr) {
          try {
            const mockUser = JSON.parse(mockUserStr);
            const token = `mock_${mockUser.uid}`;
            const backendData = await registerSessionOnBackend(token);
            if (backendData && backendData.ok) {
              set({
                user: {
                  ...mockUser,
                  is_admin: backendData.is_admin,
                  is_approved: backendData.is_approved
                },
                token,
                sessionConflict: false,
                loading: false,
              });
            } else {
              set({ user: null, token: null, loading: false });
            }
          } catch (e) {
            localStorage.removeItem("flowboard_mock_user");
            set({ user: null, token: null, loading: false });
          }
        } else {
          set({ user: null, token: null, loading: false });
        }
      }
    },

    login: async (email, password) => {
      set({ loading: true, error: null });
      if (hasRealFirebase && firebaseAuth) {
        try {
          const cred = await signInWithEmailAndPassword(firebaseAuth, email, password);
          if (!cred.user.emailVerified) {
            await sendEmailVerification(cred.user);
            await signOut(firebaseAuth);
            set({
              user: null,
              token: null,
              loading: false,
              error: "Tài khoản chưa được kích hoạt! Một email xác nhận mới đã được gửi tới hòm thư của bạn. Vui lòng xác thực trước khi đăng nhập."
            });
            return;
          }
          const token = await cred.user.getIdToken();
          const backendData = await registerSessionOnBackend(token);
          if (backendData && backendData.ok) {
            set({
              user: {
                uid: cred.user.uid,
                email: cred.user.email || "",
                name: cred.user.displayName || undefined,
                photoURL: cred.user.photoURL || undefined,
                is_admin: backendData.is_admin,
                is_approved: backendData.is_approved
              },
              token,
              sessionConflict: false,
              loading: false
            });
          } else {
            await signOut(firebaseAuth);
            set({ user: null, token: null, loading: false });
          }
        } catch (err: any) {
          set({ error: err.message, loading: false });
        }
      } else {
        // Mock Login
        const uid = email.split("@")[0] || "user123";
        const mockUser = { uid, email, name: uid.toUpperCase() };
        localStorage.setItem("flowboard_mock_user", JSON.stringify(mockUser));
        const token = `mock_${uid}`;
        const backendData = await registerSessionOnBackend(token);
        if (backendData && backendData.ok) {
          set({
            user: {
              ...mockUser,
              is_admin: backendData.is_admin,
              is_approved: backendData.is_approved
            },
            token,
            sessionConflict: false,
            loading: false,
          });
        } else {
          set({ loading: false });
        }
      }
    },

    register: async (email, password) => {
      set({ loading: true, error: null });
      if (hasRealFirebase && firebaseAuth) {
        try {
          const cred = await createUserWithEmailAndPassword(firebaseAuth, email, password);
          // Send verification email
          await sendEmailVerification(cred.user);
          // Sign out immediately so they cannot login without verifying
          await signOut(firebaseAuth);
          set({
            user: null,
            token: null,
            loading: false,
            error: "Đăng ký thành công! Vui lòng kiểm tra email của bạn để xác thực/kích hoạt tài khoản trước khi đăng nhập."
          });
        } catch (err: any) {
          set({ error: err.message, loading: false });
        }
      } else {
        // Mock Register
        const uid = email.split("@")[0] || "user123";
        const mockUser = { uid, email, name: uid.toUpperCase() };
        localStorage.setItem("flowboard_mock_user", JSON.stringify(mockUser));
        const token = `mock_${uid}`;
        const backendData = await registerSessionOnBackend(token);
        if (backendData && backendData.ok) {
          set({
            user: {
              ...mockUser,
              is_admin: backendData.is_admin,
              is_approved: backendData.is_approved
            },
            token,
            sessionConflict: false,
            loading: false,
          });
        } else {
          set({ loading: false });
        }
      }
    },

    signInWithGoogle: async () => {
      set({ loading: true, error: null });
      if (hasRealFirebase && firebaseAuth) {
        try {
          const provider = new GoogleAuthProvider();
          const cred = await signInWithPopup(firebaseAuth, provider);
          const token = await cred.user.getIdToken();
          const backendData = await registerSessionOnBackend(token);
          if (backendData && backendData.ok) {
            set({
              user: {
                uid: cred.user.uid,
                email: cred.user.email || "",
                name: cred.user.displayName || undefined,
                photoURL: cred.user.photoURL || undefined,
                is_admin: backendData.is_admin,
                is_approved: backendData.is_approved
              },
              token,
              sessionConflict: false,
              loading: false
            });
          } else {
            await signOut(firebaseAuth);
            set({ user: null, token: null, loading: false });
          }
        } catch (err: any) {
          set({ error: err.message, loading: false });
        }
      } else {
        // Mock Google Login
        const mockUser = { uid: "google_user", email: "google@example.com", name: "Google Tester" };
        localStorage.setItem("flowboard_mock_user", JSON.stringify(mockUser));
        const token = "mock_google_user";
        const backendData = await registerSessionOnBackend(token);
        if (backendData && backendData.ok) {
          set({
            user: {
              ...mockUser,
              is_admin: backendData.is_admin,
              is_approved: backendData.is_approved
            },
            token,
            sessionConflict: false,
            loading: false,
          });
        } else {
          set({ loading: false });
        }
      }
    },

    logout: async () => {
      set({ loading: true });
      if (hasRealFirebase && firebaseAuth) {
        try {
          await signOut(firebaseAuth);
        } catch (err) {
          console.error("Signout failed:", err);
        }
      } else {
        localStorage.removeItem("flowboard_mock_user");
      }
      set({ user: null, token: null, loading: false, sessionConflict: false });
    },

    registerSessionOnBackend,
    
    triggerConflict: () => {
      set({ sessionConflict: true });
    },

    clearError: () => {
      set({ error: null });
    }
  };
});
