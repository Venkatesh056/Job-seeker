import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test/setup.ts"],
    include: ["src/**/*.test.{ts,tsx}"],
  },
  resolve: {
    // `import.meta.dirname` rather than `__dirname`: this file is ESM, and Vite warns
    // that the CommonJS global is going away in its next major.
    alias: { "@": new URL("./src", import.meta.url).pathname },
  },
});
