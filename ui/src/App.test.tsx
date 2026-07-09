import { render, screen } from "@testing-library/react";
import { expect, test } from "vitest";
import { App } from "./App";

test("scaffold shell renders", () => {
  render(<App />);
  expect(screen.getByText(/UI scaffold/)).toBeDefined();
});
