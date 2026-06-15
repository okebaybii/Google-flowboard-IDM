import { defineConfig, createLogger } from "vite";
import react from "@vitejs/plugin-react";
import path from "node:path";

// Quiet the noisy "[vite] http proxy error: ... AggregateError [ECONNREFUSED]"
// spam that fires whenever the backend (agent on :8101) is briefly unreachable
// — e.g. while uvicorn --reload restarts, or during health/activity polling.
// The system still works; we just collapse the repeated stack traces into a
// single throttled one-liner so the console stays readable.
const logger = createLogger();
const origError = logger.error.bind(logger);
let lastProxyWarn = 0;
logger.error = (msg, options) => {
  if (typeof msg === "string" && msg.includes("proxy error")) {
    const now = Date.now();
    if (now - lastProxyWarn > 10000) {
      lastProxyWarn = now;
      logger.warn(
        "[proxy] backend on :8101 unreachable (agent restarting or down) — hiding repeats for 10s",
      );
    }
    return;
  }
  origError(msg, options);
};

export default defineConfig({
  customLogger: logger,
  plugins: [react()],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "src"),
    },
  },
  server: {
    port: 1234,
    host: "0.0.0.0",
    allowedHosts: ["flow.hieupro.io.vn"],
    proxy: {
      "/api": "http://localhost:8101",
      "/media": "http://localhost:8101",
      "/ws": {
        target: "ws://localhost:8101",
        ws: true,
      },
    },
  },
});
