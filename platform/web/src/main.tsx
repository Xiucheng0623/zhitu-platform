import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import "./design/tokens.css";
import "./design/components.css";
import "./design/pages.css";
import "./shell/shell.css";
import App from "./App";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>
);
