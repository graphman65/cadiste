import react from "@vitejs/plugin-react";
import { defineConfig, loadEnv } from "vite";

// The build lands in the Python package, which serves it; `npm run dev` proxies the API to `cadiste serve`.
export default defineConfig(({ mode }) => {
  const backend = loadEnv(mode, ".", "CADISTE_").CADISTE_BACKEND_URL || "http://127.0.0.1:8123";
  return {
    plugins: [react()],
    // three-cad-viewer bundles three.js: one ~1.7 MB chunk, served locally.
    build: { outDir: "../src/cadiste/static", emptyOutDir: true, chunkSizeWarningLimit: 2500 },
    server: { proxy: { "/api": backend } },
  };
});
