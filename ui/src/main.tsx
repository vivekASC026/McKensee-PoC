import "./lib/token";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import { loadPack } from "./lib/pack";
import "./styles/tokens.css";

const root = createRoot(document.getElementById("root")!);

// The screens say what the business pack calls its records, so the pack is
// read before the first one is drawn.
loadPack().then(
  () =>
    root.render(
      <StrictMode>
        <App />
      </StrictMode>,
    ),
  (e: unknown) =>
    root.render(
      <p className="banner banner--alarm" role="alert" style={{ margin: "var(--space-6)" }}>
        Couldn't read the business pack from the server: {e instanceof Error ? e.message : String(e)}.
        Check that SDM is running, then reload the page.
      </p>,
    ),
);
