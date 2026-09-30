/**
 * NovaLoc / 新译 — typed API client.
 *
 * Mirrors the frozen backend contract exactly (all JSON, UTF-8, base path `/api`).
 * The base origin can be overridden with `VITE_API_BASE` (default: same origin).
 */

/* ------------------------------------------------------------------ *
 * Configuration
 * ------------------------------------------------------------------ */

function normalizeBase(raw: string | undefined): string {
  const value = (raw ?? '').trim()
  if (!value) return ''
  return value.replace(/\/+$/, '')
}

/** API origin without the `/api` suffix, e.g. `` (same origin) or `http://127.0.0.1:8000`. */
export const API_ORIGIN = normalizeBase(import.meta.env.VITE_API_BASE)

/** Full base path of the REST API. */
export const API_BASE = `${API_ORIGIN}/api`

/** WebSocket origin, e.g. `ws://127.0.0.1:8000`. */
export function wsOrigin(): string {
  const explicit = normalizeBase(import.meta.env.VITE_WS_BASE)
  if (explicit) return explicit.replace(/^http/, 'ws')
  if (API_ORIGIN) return API_ORIGIN.replace(/^http/, 'ws')
  const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
  return `${proto}//${window.location.host}`
}

/** Resolve a server-relative path (e.g. an annotated PNG url) to an absolute URL. */
export function apiUrl(path: string): string {
  if (/^[a-z][a-z0-9+.-]*:\/\//i.test(path)) return path
  const suffix = path.startsWith('/') ? path : `/${path}`
  return `${API_ORIGIN}${suffix}`
}

/* ------------------------------------------------------------------ *
 * Types — frozen contract
 * ------------------------------------------------------------------ */

export type EngineOption = { id: string; display_name: string }

export type HealthOllama = { available: boolean; base_url: string; models: string[] }
export type HealthGpu = { directml: boolean; name: string }
export type Health = {
  ok: boolean
  version: string
  ollama: HealthOllama
  gpu: HealthGpu
  engines: EngineOption[]
}

export type ProjectProgress = Record<string, unknown>

export type Project = {
  id: string
  name: string
  game_dir: string
  engine: string | null
  engine_version: string | null
  created_at: string
  updated_at: string
  progress: ProjectProgress
}

export type CreateProjectBody = { name: string; game_dir: string; engine?: string }

export type DetectResult = {
  engine_id: string
  display_name: string
  confidence: number
  version: string | null
  evidence: string[]
}

export type JobRef = { job_id: string }

export type TextStatus = 'pending' | 'translated' | 'reviewed' | 'skipped' | 'failed' | string
export type TextKind = 'dialogue' | 'ui' | 'item' | 'quest' | 'system' | 'other' | string

export type TextEntry = {
  uid: string
  source: string
  target: string
  status: TextStatus
  kind: TextKind
  speaker: string | null
  warnings: string[]
}

export type TextPage = { total: number; entries: TextEntry[] }

export type TextPatchItem = { uid: string; target: string; status?: TextStatus }
export type TextPatchResult = { updated: number }

export type ImageBlock = {
  id: string
  source: string
  target: string
  status: TextStatus
  confidence: number
}

export type ImageAsset = {
  uid: string
  path: string
  width: number
  height: number
  blocks: ImageBlock[]
}

export type ImagePage = { total: number; images: ImageAsset[] }

export type ImagePatchItem = { uid: string; block_id: string; target: string }
export type ImagePatchResult = { updated: number }

export type FontCoverage = {
  font_id: string
  path: string
  family: string
  coverage_ratio: number
  missing_count: number
}

export type FontPatch = Record<string, unknown>

export type FontReport = { coverage: FontCoverage[]; patches: FontPatch[] }

export type QaSeverity = 'info' | 'warning' | 'error' | 'critical' | string
export type QaIssue = { severity: QaSeverity; stage: string; message: string; detail: string }
export type QaReport = { ok: boolean; issues: QaIssue[]; stats: Record<string, unknown> }

export type JobStatus = 'queued' | 'running' | 'done' | 'failed' | 'cancelled' | string

export type JobEvent = {
  kind: JobEventKind
  stage: string
  message: string
  progress: number
  severity: JobSeverity
  data: Record<string, unknown> | null
  ts: string
}

export type JobEventKind = 'log' | 'progress' | 'stage_start' | 'stage_end' | 'stage_error'
export type JobSeverity = 'debug' | 'info' | 'warning' | 'error' | string

export type Job = {
  id: string
  stage: string
  status: JobStatus
  progress: number
  message: string
  started_at: string | null
  finished_at: string | null
  error: string | null
  events: JobEvent[]
}

export type OcrTier = 'fast' | 'balanced' | 'accurate' | string
export type FontPolicy = 'subset' | 'repackage' | 'report-only' | string

export type Settings = {
  ollama_base_url: string
  model_translate: string
  model_vision: string
  target_language: string
  ocr_tier: OcrTier
  use_directml: boolean
  font_policy: FontPolicy
}

/** A settings payload may also carry advisory arrays for the UI to offer as choices. */
export type SettingsResponse = Settings & {
  models?: string[]
  target_languages?: string[]
  ocr_tiers?: string[]
  font_policies?: string[]
}

/* ------------------------------------------------------------------ *
 * Errors
 * ------------------------------------------------------------------ */

export class ApiError extends Error {
  readonly status: number
  readonly url: string
  readonly body: string

  constructor(message: string, status: number, url: string, body = '') {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.url = url
    this.body = body
  }
}

/* ------------------------------------------------------------------ *
 * Core request helper
 * ------------------------------------------------------------------ */

type RequestOptions = {
  method?: 'GET' | 'POST' | 'PATCH' | 'PUT' | 'DELETE'
  body?: unknown
  signal?: AbortSignal
}

function extractDetail(body: string): string {
  if (!body) return ''
  try {
    const parsed: unknown = JSON.parse(body)
    if (parsed && typeof parsed === 'object') {
      const record = parsed as Record<string, unknown>
      const detail = record['detail'] ?? record['message'] ?? record['error']
      if (typeof detail === 'string') return detail
      if (Array.isArray(detail)) {
        return detail
          .map((item) =>
            item && typeof item === 'object' && 'msg' in item
              ? String((item as Record<string, unknown>)['msg'])
              : String(item),
          )
          .join('; ')
      }
    }
  } catch {
    /* not JSON — fall through to the raw body */
  }
  return body.slice(0, 400)
}

export async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const url = apiUrl(path)
  const init: RequestInit = {
    method: options.method ?? 'GET',
    headers: { Accept: 'application/json' },
  }
  if (options.body !== undefined) {
    init.headers = { ...init.headers, 'Content-Type': 'application/json' }
    init.body = JSON.stringify(options.body)
  }
  if (options.signal) init.signal = options.signal

  let response: Response
  try {
    response = await fetch(url, init)
  } catch (cause) {
    throw new ApiError(
      `无法连接后端服务 (${url})。请确认后端已启动。`,
      0,
      url,
      cause instanceof Error ? cause.message : String(cause),
    )
  }

  const raw = await response.text()
  if (!response.ok) {
    const detail = extractDetail(raw)
    throw new ApiError(
      detail || `请求失败 ${response.status} ${response.statusText}`,
      response.status,
      url,
      raw,
    )
  }
  if (!raw) return undefined as T
  try {
    return JSON.parse(raw) as T
  } catch {
    throw new ApiError('后端返回了非法 JSON。', response.status, url, raw)
  }
}

/** Build a querystring, skipping empty values. */
function qs(params: Record<string, string | number | undefined | null>): string {
  const search = new URLSearchParams()
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null || value === '') continue
    search.set(key, String(value))
  }
  const out = search.toString()
  return out ? `?${out}` : ''
}

/* ------------------------------------------------------------------ *
 * Endpoints
 * ------------------------------------------------------------------ */

export type TextQuery = {
  offset?: number
  limit?: number
  status?: string
  kind?: string
  q?: string
}

export type ImageQuery = { offset?: number; limit?: number; q?: string }

export const api = {
  /* -- health ------------------------------------------------------ */
  health: (signal?: AbortSignal) => request<Health>('/api/health', { signal }),

  /* -- projects ---------------------------------------------------- */
  listProjects: (signal?: AbortSignal) => request<Project[]>('/api/projects', { signal }),

  createProject: (body: CreateProjectBody) =>
    request<Project>('/api/projects', { method: 'POST', body }),

  getProject: (id: string, signal?: AbortSignal) =>
    request<Project>(`/api/projects/${encodeURIComponent(id)}`, { signal }),

  deleteProject: (id: string) =>
    request<{ ok: boolean }>(`/api/projects/${encodeURIComponent(id)}`, { method: 'DELETE' }),

  /* -- pipeline stages --------------------------------------------- */
  detect: (id: string) =>
    request<DetectResult>(`/api/projects/${encodeURIComponent(id)}/detect`, { method: 'POST' }),

  extract: (id: string) =>
    request<JobRef>(`/api/projects/${encodeURIComponent(id)}/extract`, { method: 'POST' }),

  translate: (id: string) =>
    request<JobRef>(`/api/projects/${encodeURIComponent(id)}/translate`, { method: 'POST' }),

  fonts: (id: string) =>
    request<JobRef>(`/api/projects/${encodeURIComponent(id)}/fonts`, { method: 'POST' }),

  scanImages: (id: string) =>
    request<JobRef>(`/api/projects/${encodeURIComponent(id)}/images`, { method: 'POST' }),

  runAll: (id: string) =>
    request<JobRef>(`/api/projects/${encodeURIComponent(id)}/run`, { method: 'POST' }),

  apply: (id: string) =>
    request<JobRef>(`/api/projects/${encodeURIComponent(id)}/apply`, { method: 'POST' }),

  /* -- text -------------------------------------------------------- */
  // NOTE: `query` is a forward-compatible convenience; the frozen contract
  // declares this endpoint as a plain full dump and backends may ignore it.
  getText: (id: string, query: TextQuery = {}, signal?: AbortSignal) =>
    request<TextPage>(`/api/projects/${encodeURIComponent(id)}/text${qs(query)}`, { signal }),

  patchText: (id: string, items: TextPatchItem[]) =>
    request<TextPatchResult>(`/api/projects/${encodeURIComponent(id)}/text`, {
      method: 'PATCH',
      body: { items },
    }),

  /* -- images ------------------------------------------------------ */
  getImages: (id: string, query: ImageQuery = {}, signal?: AbortSignal) =>
    request<ImagePage>(`/api/projects/${encodeURIComponent(id)}/images${qs(query)}`, { signal }),

  patchImages: (id: string, items: ImagePatchItem[]) =>
    request<ImagePatchResult>(`/api/projects/${encodeURIComponent(id)}/images`, {
      method: 'PATCH',
      body: { items },
    }),

  annotatedUrl: (id: string, uid: string) =>
    apiUrl(
      `/api/projects/${encodeURIComponent(id)}/images/${encodeURIComponent(uid)}/annotated`,
    ),

  /* -- fonts ------------------------------------------------------- */
  getFonts: (id: string, signal?: AbortSignal) =>
    request<FontReport>(`/api/projects/${encodeURIComponent(id)}/fonts`, { signal }),

  /* -- qa ---------------------------------------------------------- */
  getQa: (id: string, signal?: AbortSignal) =>
    request<QaReport>(`/api/projects/${encodeURIComponent(id)}/qa`, { signal }),

  /* -- jobs -------------------------------------------------------- */
  getJob: (jobId: string, signal?: AbortSignal) =>
    request<Job>(`/api/jobs/${encodeURIComponent(jobId)}`, { signal }),

  /* -- settings ---------------------------------------------------- */
  getSettings: (signal?: AbortSignal) => request<SettingsResponse>('/api/settings', { signal }),

  putSettings: (body: Settings) =>
    request<SettingsResponse>('/api/settings', { method: 'PUT', body }),
}

export type ApiClient = typeof api
