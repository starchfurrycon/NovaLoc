/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** Backend origin for /api calls. Empty = same origin. */
  readonly VITE_API_BASE?: string
  /** WebSocket origin for /ws/jobs/{id}. Empty = same host as the page. */
  readonly VITE_WS_BASE?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
