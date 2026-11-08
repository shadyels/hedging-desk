import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { App } from "./App";
import { natsConnector, startConnection } from "./bus/connection";
import { busSink } from "./store";
import "./styles/tokens.css";
import "./styles/shell.css";

// ui/CLAUDE.md "Stack": NATS WebSocket listener from deploy/nats.conf (no_tls, demo only).
startConnection({
  connect: natsConnector("ws://localhost:9222"),
  sink: busSink,
});

const el = document.getElementById("root");
if (el)
  createRoot(el).render(
    <StrictMode>
      <App />
    </StrictMode>,
  );
