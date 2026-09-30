/**
 * NovaLoc / 新译 — live job progress over WebSocket.
 *
 * Subscribes to `ws://<host>/ws/jobs/{job_id}`, which pushes
 * `{kind, stage, message, progress, severity, data, ts}` frames where
 * `kind ∈ log | progress | stage_start | stage_end | stage_error`.
 *
 * The socket auto-reconnects with capped exponential backoff and accumulates
 * every event it has seen. While the socket is unhealthy the hook falls back to
 * polling `GET /api/jobs/{job_id}` at a slow interval, so a proxied or
 * WebSocket-hostile deployment still shows progress.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { api, wsOrigin } from './api'
import type { Job, JobEvent, JobEventKind, JobSeverity } from './api'

export type SocketStatus = 'idle' | 'connecting' | 'open' | 'closed' | 'error'

export type UseJobSocketOptions = {
  /** Disable the subscription entirely (default: true). */
  enabled?: boolean
  /** Max retained events in memory (default: 2000, oldest dropped). */
  maxEvents?: number
  /** Poll `GET /api/jobs/{id}` while the socket is not open (default: true). */
  pollFallback?: boolean
  /** Poll interval in ms (default: 2500). */
  pollIntervalMs?: number
}

export type JobSocketState = {
  status: SocketStatus
  /** Events in arrival order, including polled ones. */
  events: JobEvent[]
  /** Latest known job snapshot (from polling), or null. */
  job: Job | null
  /** Latest event, for headline display. */
  lastEvent: JobEvent | null
  /** Latest reported overall progress in the range 0..1. */
  progress: number
  /** Human-readable reason for the most recent socket problem. */
  error: string | null
  /** Number of connection attempts since the last successful open. */
  attempts: number
  /** Force a reconnect now. */
  reconnect: () => void
  /** Drop accumulated events. */
  clear: () => void
}

const KIND_ALIASES: Record<string, JobEventKind> = {
  log: 'log',
  progress: 'progress',
  stage_start: 'stage_start',
  stage_end: 'stage_end',
  stage_error: 'stage_error',
}

const SEVERITIES: readonly string[] = ['debug', 'info', 'warning', 'error']

function asString(value: unknown, fallback = ''): string {
  return typeof value === 'string' ? value : value === null || value === undefined ? fallback : String(value)
}

function asNumber(value: unknown, fallback = 0): number {
  if (typeof value === 'number' && Number.isFinite(value)) return value
  if (typeof value === 'string') {
    const parsed = Number.parseFloat(value)
    if (Number.isFinite(parsed)) return parsed
  }
  return fallback
}

function coerceKind(value: unknown): JobEventKind {
  const key = asString(value, 'log').toLowerCase()
  return KIND_ALIASES[key] ?? 'log'
}

function coerceSeverity(value: unknown): JobSeverity {
  const key = asString(value, 'info').toLowerCase()
  return SEVERITIES.includes(key) ? key : 'info'
}

/** Normalize an arbitrary frame (socket payload or REST event) into a JobEvent. */
export function normalizeEvent(input: unknown): JobEvent | null {
  if (!input || typeof input !== 'object') return null
  const record = input as Record<string, unknown>
  const data = record['data']
  return {
    kind: coerceKind(record['kind']),
    stage: asString(record['stage']),
    message: asString(record['message'] ?? record['msg']),
    progress: asNumber(record['progress'], 0),
    severity: coerceSeverity(record['severity']),
    data: data && typeof data === 'object' ? (data as Record<string, unknown>) : null,
    ts: asString(record['ts'], new Date().toISOString()),
  }
}

function eventKey(event: JobEvent): string {
  return `${event.ts}|${event.kind}|${event.stage}|${event.progress}|${event.message}`
}

function clampProgress(value: number): number {
  if (!Number.isFinite(value)) return 0
  return Math.min(1, Math.max(0, value))
}

export function useJobSocket(
  jobId: string | undefined,
  options: UseJobSocketOptions = {},
): JobSocketState {
  const {
    enabled = true,
    maxEvents = 2000,
    pollFallback = true,
    pollIntervalMs = 2500,
  } = options

  const [status, setStatus] = useState<SocketStatus>('idle')
  const [events, setEvents] = useState<JobEvent[]>([])
  const [job, setJob] = useState<Job | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [attempts, setAttempts] = useState(0)
  const [nonce, setNonce] = useState(0)

  const socketRef = useRef<WebSocket | null>(null)
  const retryRef = useRef(0)
  const timerRef = useRef<number | null>(null)
  const closedByUsRef = useRef(false)
  const seenRef = useRef<Set<string>>(new Set())
  const maxEventsRef = useRef(maxEvents)
  maxEventsRef.current = maxEvents

  const append = useCallback((incoming: JobEvent[]) => {
    if (incoming.length === 0) return
    const fresh: JobEvent[] = []
    for (const event of incoming) {
      const key = eventKey(event)
      if (seenRef.current.has(key)) continue
      seenRef.current.add(key)
      fresh.push(event)
    }
    if (fresh.length === 0) return
    if (seenRef.current.size > maxEventsRef.current * 2) {
      seenRef.current = new Set([...seenRef.current].slice(-maxEventsRef.current))
    }
    setEvents((previous) => {
      const merged = previous.concat(fresh)
      return merged.length > maxEventsRef.current
        ? merged.slice(merged.length - maxEventsRef.current)
        : merged
    })
  }, [])

  const reset = useCallback(() => {
    seenRef.current = new Set()
    setEvents([])
    setJob(null)
    setError(null)
    setAttempts(0)
  }, [])

  const reconnect = useCallback(() => {
    retryRef.current = 0
    setNonce((value) => value + 1)
  }, [])

  const clear = useCallback(() => {
    seenRef.current = new Set()
    setEvents([])
  }, [])

  /* -- reset state whenever the target job changes ------------------ */
  useEffect(() => {
    reset()
  }, [jobId, reset])

  /* -- websocket subscription -------------------------------------- */
  useEffect(() => {
    if (!enabled || !jobId) {
      setStatus('idle')
      return
    }
    if (typeof WebSocket === 'undefined') {
      setStatus('error')
      setError('当前环境不支持 WebSocket，已切换为轮询模式。')
      return
    }

    let disposed = false
    closedByUsRef.current = false

    const clearTimer = () => {
      if (timerRef.current !== null) {
        window.clearTimeout(timerRef.current)
        timerRef.current = null
      }
    }

    const scheduleRetry = () => {
      if (disposed || closedByUsRef.current) return
      retryRef.current += 1
      setAttempts(retryRef.current)
      const delay = Math.min(8000, 500 * 2 ** Math.min(retryRef.current - 1, 4))
      clearTimer()
      timerRef.current = window.setTimeout(connect, delay)
    }

    const connect = () => {
      if (disposed) return
      clearTimer()
      setStatus('connecting')
      let socket: WebSocket
      try {
        socket = new WebSocket(`${wsOrigin()}/ws/jobs/${encodeURIComponent(jobId)}`)
      } catch (cause) {
        setError(cause instanceof Error ? cause.message : String(cause))
        setStatus('error')
        scheduleRetry()
        return
      }
      socketRef.current = socket

      socket.onopen = () => {
        if (disposed) return
        retryRef.current = 0
        setAttempts(0)
        setError(null)
        setStatus('open')
      }

      socket.onmessage = (message) => {
        if (disposed) return
        const raw = typeof message.data === 'string' ? message.data : ''
        if (!raw) return
        let payload: unknown
        try {
          payload = JSON.parse(raw)
        } catch {
          append([
            {
              kind: 'log',
              stage: '',
              message: raw,
              progress: 0,
              severity: 'info',
              data: null,
              ts: new Date().toISOString(),
            },
          ])
          return
        }
        if (Array.isArray(payload)) {
          const list = payload
            .map((item) => normalizeEvent(item))
            .filter((item): item is JobEvent => item !== null)
          append(list)
          return
        }
        const record = payload as Record<string, unknown>
        // Tolerate a REST-shaped envelope: {id, stage, status, progress, events: []}
        if (Array.isArray(record['events'])) {
          const list = (record['events'] as unknown[])
            .map((item) => normalizeEvent(item))
            .filter((item): item is JobEvent => item !== null)
          append(list)
          setJob(record as unknown as Job)
        }
        const single = normalizeEvent(payload)
        if (single) append([single])
      }

      socket.onerror = () => {
        if (disposed) return
        setError('任务事件通道连接异常。')
        setStatus('error')
      }

      socket.onclose = (closeEvent) => {
        if (disposed) return
        socketRef.current = null
        if (closedByUsRef.current) return
        setStatus('closed')
        if (closeEvent.code !== 1000) {
          setError(`任务事件通道已断开 (code ${closeEvent.code})，正在重连…`)
        }
        scheduleRetry()
      }
    }

    connect()

    return () => {
      disposed = true
      closedByUsRef.current = true
      clearTimer()
      const socket = socketRef.current
      socketRef.current = null
      if (socket && (socket.readyState === WebSocket.OPEN || socket.readyState === WebSocket.CONNECTING)) {
        socket.close(1000, 'component unmounted')
      }
    }
  }, [enabled, jobId, nonce, append])

  /* -- polling fallback -------------------------------------------- */
  useEffect(() => {
    if (!enabled || !jobId || !pollFallback) return
    if (status === 'open') return

    let disposed = false
    let inFlight = false

    const tick = async () => {
      if (disposed || inFlight) return
      inFlight = true
      try {
        const snapshot = await api.getJob(jobId)
        if (disposed) return
        setJob(snapshot)
        const list = (snapshot.events ?? [])
          .map((item) => normalizeEvent(item))
          .filter((item): item is JobEvent => item !== null)
        append(list)
      } catch {
        /* transient — the socket effect owns user-facing error state */
      } finally {
        inFlight = false
      }
    }

    void tick()
    const interval = window.setInterval(() => void tick(), pollIntervalMs)
    return () => {
      disposed = true
      window.clearInterval(interval)
    }
  }, [enabled, jobId, pollFallback, pollIntervalMs, status, append])

  const lastEvent = events.length > 0 ? events[events.length - 1]! : null

  /** Progress from the newest event that reported one, else from the REST snapshot. */
  const progress = useMemo(() => {
    for (let index = events.length - 1; index >= 0; index -= 1) {
      const event = events[index]!
      if (event.kind === 'progress' || event.progress > 0) return clampProgress(event.progress)
    }
    if (job) return clampProgress(job.progress)
    return 0
  }, [events, job])

  return {
    status,
    events,
    job,
    lastEvent,
    progress,
    error,
    attempts,
    reconnect,
    clear,
  }
}

/** Derive per-stage progress from the accumulated event stream. */
export type StageProgress = {
  stage: string
  progress: number
  status: 'pending' | 'running' | 'done' | 'failed'
  message: string
  startedAt: string | null
  endedAt: string | null
}

export function deriveStages(events: JobEvent[]): StageProgress[] {
  const order: string[] = []
  const map = new Map<string, StageProgress>()

  const ensure = (stage: string): StageProgress => {
    const key = stage || 'job'
    let entry = map.get(key)
    if (!entry) {
      entry = {
        stage: key,
        progress: 0,
        status: 'pending',
        message: '',
        startedAt: null,
        endedAt: null,
      }
      map.set(key, entry)
      order.push(key)
    }
    return entry
  }

  for (const event of events) {
    const entry = ensure(event.stage)
    if (event.kind === 'stage_start') {
      entry.status = 'running'
      entry.startedAt = event.ts
      if (event.message) entry.message = event.message
    } else if (event.kind === 'progress') {
      entry.status = entry.status === 'pending' ? 'running' : entry.status
      entry.progress = clampProgress(event.progress)
      if (event.message) entry.message = event.message
    } else if (event.kind === 'log') {
      if (entry.status === 'pending') entry.status = 'running'
      if (event.message) entry.message = event.message
    } else if (event.kind === 'stage_end') {
      entry.status = 'done'
      entry.progress = 1
      entry.endedAt = event.ts
      if (event.message) entry.message = event.message
    } else if (event.kind === 'stage_error') {
      entry.status = 'failed'
      entry.endedAt = event.ts
      if (event.message) entry.message = event.message
    }
  }

  return order.map((key) => map.get(key)!)
}

/** Latest reported progress across the whole stream. */
export function overallProgress(events: JobEvent[]): number {
  for (let index = events.length - 1; index >= 0; index -= 1) {
    const event = events[index]!
    if (event.kind === 'stage_end') return 1
    if (event.kind === 'progress') return clampProgress(event.progress)
  }
  return 0
}
