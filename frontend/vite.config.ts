import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    port: 5173,
    // Proxy API calls to the FastAPI backend so there are no CORS issues in dev.
    //
    // 127.0.0.1, NOT localhost. uvicorn binds IPv4 only, while `localhost` on Windows
    // resolves to ::1 first — that connection is refused and the stack waits ~2s before
    // falling back to IPv4. Measured: 16ms via 127.0.0.1 vs 2030ms via localhost, on EVERY
    // request. It looked like a slow model; it was name resolution.
    proxy: {
      "/api": "http://127.0.0.1:8000",
    },
  },
});
