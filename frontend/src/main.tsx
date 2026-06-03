import React from "react";
import ReactDOM from "react-dom/client";
import { App } from "./App";
import { useAuthStore } from "./store/auth";
import "@xyflow/react/dist/style.css";
import "./styles.css";

// Global Fetch Interceptor to automatically attach authorization and session headers to all /api/ requests.
const originalFetch = window.fetch;
window.fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
  let url = "";
  if (typeof input === "string") {
    url = input;
  } else if (input instanceof URL) {
    url = input.href;
  } else if (input && typeof input === "object" && "url" in input) {
    url = input.url;
  }

  const isApi = url.startsWith("/api") || url.startsWith("api") || url.includes("/api/");
  if (isApi) {
    const { sessionId } = useAuthStore.getState();
    const token = await useAuthStore.getState().getFreshToken();
    if (token || sessionId) {
      if (input instanceof Request) {
        if (token && !input.headers.has("Authorization")) {
          input.headers.set("Authorization", `Bearer ${token}`);
        }
        if (sessionId && !input.headers.has("X-Session-ID")) {
          input.headers.set("X-Session-ID", sessionId);
        }
      } else {
        const headers = new Headers(init?.headers);
        if (token && !headers.has("Authorization")) {
          headers.set("Authorization", `Bearer ${token}`);
        }
        if (sessionId && !headers.has("X-Session-ID")) {
          headers.set("X-Session-ID", sessionId);
        }
        init = {
          ...init,
          headers,
        };
      }
    }
  }
  return originalFetch(input, init);
};

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);

