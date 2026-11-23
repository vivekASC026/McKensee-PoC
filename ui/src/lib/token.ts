/**
 * Every call to SDM's API carries the token the server gave this page.
 *
 * The server makes a token when it starts and puts it in the page it serves
 * (`<meta name="sdm-token">`); a request without it is refused, so a page on
 * another site cannot use the API. Installed once, before anything fetches,
 * so every fetch -- JSON calls, uploads, downloads -- sends it. In development
 * (`npm run dev`) the page comes from Vite, which has no token: start the
 * server with SDG_API_TOKEN and Vite with the same value as VITE_SDM_TOKEN.
 */

const TOKEN =
  document.querySelector('meta[name="sdm-token"]')?.getAttribute("content") ??
  ((import.meta as unknown as { env?: Record<string, string> }).env?.VITE_SDM_TOKEN) ??
  "";

function isApi(input: RequestInfo | URL): boolean {
  const url = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
  const parsed = new URL(url, window.location.href);
  return parsed.origin === window.location.origin && parsed.pathname.startsWith("/api");
}

const plain = window.fetch.bind(window);

window.fetch = (input: RequestInfo | URL, init?: RequestInit) => {
  if (!isApi(input)) return plain(input, init);
  const headers = new Headers(init?.headers ?? (input instanceof Request ? input.headers : undefined));
  headers.set("X-SDM-Token", TOKEN);
  return plain(input, { ...init, headers });
};

export {};
