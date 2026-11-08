import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  // The registry test imports protocol/nats-subjects.md (?raw) so it can fail on drift.
  server: { fs: { allow: [".", "../protocol"] } },
  test: { environment: "jsdom" },
});
