import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  // The registry test imports protocol/nats-subjects.md (?raw) so it can fail on drift.
  server: { fs: { allow: [".", "../protocol"] } },
  // Vitest blanks every .css import by default. testCss.ts reads the real stylesheets (?raw) so a test can
  // check what a rule declares; only that import form is let through.
  test: { environment: "jsdom", css: { include: [/\.css\?raw$/] } },
});
