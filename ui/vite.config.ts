import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5273,
    // The engine runs behind the same origin in production; in development the
    // API is proxied so the browser never has to think about two hosts.
    proxy: { "/api": "http://127.0.0.1:8777" },
  },
  build: { outDir: "dist", emptyOutDir: true },
});
