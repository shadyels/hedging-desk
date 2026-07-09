import tseslint from "typescript-eslint";

export default tseslint.config(
  ...tseslint.configs.recommended,
  { files: ["src/**/*.{ts,tsx}"] },
  { ignores: ["dist/", "src/gen/"] }  // src/gen = ts-proto output (generated, not linted)
);
