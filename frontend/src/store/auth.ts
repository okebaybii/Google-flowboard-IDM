import { create } from "zustand";
import { 
  signInWithEmailAndPassword, 
  createUserWithEmailAndPassword, 
  signOut, 
  GoogleAuthProvider, 
  signInWithPopup,
  onAuthStateChanged 
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
  getFreshToken: () => Promise<string | null>;
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
      
      let errorMsg = "Đăng nhập thất bại.";
      try {
        const errData = await response.json();
        if (response.status === 403 && errData.detail === "account_not_approved") {
          errorMsg = "Tài khoản của bạn đang chờ Admin kích hoạt trên Firebase Console. Vui lòng liên hệ quản trị viên!";
        } else if (errData.detail === "session_conflict") {
          set({ sessionConflict: true });
          return null;
        } else {
          const detailStr = typeof errData.detail === 'string' ? errData.detail : JSON.stringify(errData.detail);
          errorMsg = `Lỗi (${response.status}): ${detailStr || "Đăng nhập thất bại."}`;
        }
      } catch (e) {
        errorMsg = `Lỗi kết nối (${response.status}): Không thể giải mã phản hồi từ máy chủ.`;
      }
      set({ error: errorMsg });
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
          if (err.code === "auth/user-disabled") {
            set({
              error: "Tài khoản của bạn đã được đăng ký nhưng chưa được kích hoạt hoặc đã bị khóa trên Firebase Console. Vui lòng liên hệ Admin!",
              loading: false
            });
          } else {
            set({ error: err.message, loading: false });
          }
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
    
    getFreshToken: async () => {
      const state = get();
      if (hasRealFirebase && firebaseAuth && firebaseAuth.currentUser) {
        try {
          const freshToken = await firebaseAuth.currentUser.getIdToken();
          set({ token: freshToken });
          return freshToken;
        } catch (err) {
          console.error("Failed to refresh Firebase token:", err);
          return state.token;
        }
      }
      return state.token;
    },
    
    triggerConflict: () => {
      set({ sessionConflict: true });
    },

    clearError: () => {
      set({ error: null });
    }
  };
});
