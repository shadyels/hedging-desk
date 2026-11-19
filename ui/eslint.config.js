import tseslint from "typescript-eslint";

export default tseslint.config(
  ...tseslint.configs.recommended,
  { files: ["src/**/*.{ts,tsx}"] },
  {
    // Wire strings (alert text, reject reasons, health detail) are untrusted: text nodes only.
    files: ["src/**/*.{ts,tsx}"],
    rules: {
      "no-restricted-syntax": [
        "error",
        {
          selector: "JSXAttribute[name.name='dangerouslySetInnerHTML']",
          message: "dangerouslySetInnerHTML is banned: bus strings render as text (ui/DESIGN.md Numbers).",
        },
      ],
    },
  },
  { ignores: ["dist/", "src/gen/"] }  // src/gen = ts-proto output (generated, not linted)
);
