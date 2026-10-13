import { defineConfig } from "tsup";

export default defineConfig([
  {
    entry: ["src/index.ts"],
    format: ["esm"],
    dts: true,
    sourcemap: true,
    clean: true,
    target: "node20",
  },
  {
    entry: ["src/serve/cli.ts"],
    format: ["esm"],
    sourcemap: false,
    clean: false,
    target: "node20",
    dts: false,
    outDir: "dist",
    outExtension: () => ({ js: ".js" }),
  },
]);
