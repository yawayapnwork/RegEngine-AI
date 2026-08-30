import React from "react";
import ReactDOM from "react-dom/client";
import { Auth0Provider } from "@auth0/auth0-react";
import App from "./App.jsx";
import "./index.css";

const domain = import.meta.env?.VITE_AUTH0_DOMAIN;
const clientId = import.meta.env?.VITE_AUTH0_CLIENT_ID;
const audience = import.meta.env?.VITE_AUTH0_AUDIENCE;

const root = ReactDOM.createRoot(document.getElementById("root"));

// Auth0Provider requires domain/clientId to render at all -- rather than
// let it throw on a misconfigured deployment (missing env vars at build
// time), render the app unauthenticated-but-functional (every read-only
// view still works; only the upload action needs a real token) with a
// console warning, so a broken env var config degrades instead of white-
// screening the whole dashboard.
if (!domain || !clientId) {
  console.warn(
    "VITE_AUTH0_DOMAIN / VITE_AUTH0_CLIENT_ID are not set -- login is disabled; " +
      "circular upload will not work until these are configured (see frontend/.env.example).",
  );
  root.render(
    <React.StrictMode>
      <App auth0Configured={false} />
    </React.StrictMode>,
  );
} else {
  root.render(
    <React.StrictMode>
      <Auth0Provider
        domain={domain}
        clientId={clientId}
        authorizationParams={{
          redirect_uri: window.location.origin,
          ...(audience ? { audience } : {}),
        }}
        // Persists the session across a page reload (default is
        // in-memory, which logs the user out on every refresh) --
        // acceptable here since this is a first-party SPA reading its
        // own token, not a third-party embed.
        cacheLocation="localstorage"
        useRefreshTokens
      >
        <App auth0Configured={true} />
      </Auth0Provider>
    </React.StrictMode>,
  );
}
