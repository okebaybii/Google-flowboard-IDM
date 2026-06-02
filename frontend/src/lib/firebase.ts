import { initializeApp, getApps, getApp } from "firebase/app";
import { getAuth } from "firebase/auth";

const metaEnv = (import.meta as any).env || {};

const firebaseConfig = {
  apiKey: metaEnv.VITE_FIREBASE_API_KEY || "",
  authDomain: metaEnv.VITE_FIREBASE_AUTH_DOMAIN || "",
  projectId: metaEnv.VITE_FIREBASE_PROJECT_ID || "",
  storageBucket: metaEnv.VITE_FIREBASE_STORAGE_BUCKET || "",
  messagingSenderId: metaEnv.VITE_FIREBASE_MESSAGING_SENDER_ID || "",
  appId: metaEnv.VITE_FIREBASE_APP_ID || ""
};

export const hasRealFirebase = Boolean(firebaseConfig.apiKey);

let firebaseApp;
let firebaseAuth: any = null;

if (hasRealFirebase) {
  try {
    firebaseApp = getApps().length === 0 ? initializeApp(firebaseConfig) : getApp();
    firebaseAuth = getAuth(firebaseApp);
  } catch (err) {
    console.error("❌ Failed to initialize Firebase App:", err);
  }
} else {
  console.warn("⚠️ Firebase configuration missing (VITE_FIREBASE_API_KEY is empty). Falling back to Mock Auth engine.");
}

export { firebaseAuth };
