/** NovaLoc / 新译 — data loading / mutations / toasts. */

import { useCallback, useEffect, useRef, useState, type DependencyList } from 'react'
import type { ToastItem, ToastTone } from './components/ui'

export type ResourceState<T> = {
  data: T | null
  error: Error | null
  loading: boolean
  /** Milliseconds since the last successful load, or null. */
  updatedAt: number | null
  reload: (options?: { quiet?: boolean }) => Promise<void>
  /** Patch the cached value locally (after a PATCH round-trip). */
  set: (updater: T | ((previous: T | null) => T | null)) => void
}

function toError(cause: unknown): Error {
  if (cause instanceof Error) return cause
  return new Error(String(cause))
}

/**
 * Poll a resource. `deps` controls when the request is re-issued; `reload()`
 * forces one on demand. Aborts in-flight requests on unmount / dep change.
 */
export function useResource<T>(
  loader: (signal: AbortSignal) => Promise<T>,
  deps: DependencyList = [],
  options: { intervalMs?: number; enabled?: boolean } = {},
): ResourceState<T> {
  const { intervalMs = 0, enabled = true } = options

  const [data, setData] = useState<T | null>(null)
  const [error, setError] = useState<Error | null>(null)
  const [loading, setLoading] = useState(enabled)
  const [updatedAt, setUpdatedAt] = useState<number | null>(null)

  const loaderRef = useRef(loader)
  loaderRef.current = loader

  const abortRef = useRef<AbortController | null>(null)
  const mountedRef = useRef(true)
  const [nonce, setNonce] = useState(0)

  useEffect(() => {
    mountedRef.current = true
    return () => {
      mountedRef.current = false
      abortRef.current?.abort()
    }
  }, [])

  useEffect(() => {
    if (!enabled) {
      setLoading(false)
      return
    }
    const controller = new AbortController()
    abortRef.current?.abort()
    abortRef.current = controller
    setLoading(true)

    loaderRef.current(controller.signal).then(
      (result) => {
        if (controller.signal.aborted) return
        setData(result)
        setError(null)
        setLoading(false)
        setUpdatedAt(Date.now())
      },
      (cause: unknown) => {
        if (controller.signal.aborted) return
        setError(toError(cause))
        setLoading(false)
      },
    )

    return () => controller.abort()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [enabled, nonce, ...deps])

  useEffect(() => {
    if (!enabled || intervalMs <= 0) return
    const interval = window.setInterval(() => setNonce((value) => value + 1), intervalMs)
    return () => window.clearInterval(interval)
  }, [enabled, intervalMs])

  const reload = useCallback(async (reloadOptions: { quiet?: boolean } = {}) => {
    if (!reloadOptions.quiet) setLoading(true)
    setNonce((value) => value + 1)
  }, [])

  const set = useCallback((updater: T | ((previous: T | null) => T | null)) => {
    setData((previous) =>
      typeof updater === 'function' ? (updater as (p: T | null) => T | null)(previous) : updater,
    )
  }, [])

  return { data, error, loading, updatedAt, reload, set }
}

export type ActionState<A extends unknown[], R> = {
  run: (...args: A) => Promise<R | null>
  pending: boolean
  error: Error | null
  reset: () => void
}

/** Wrap a mutating call with pending / error bookkeeping. */
export function useAction<A extends unknown[], R>(
  action: (...args: A) => Promise<R>,
): ActionState<A, R> {
  const [pending, setPending] = useState(false)
  const [error, setError] = useState<Error | null>(null)
  const actionRef = useRef(action)
  actionRef.current = action
  const mountedRef = useRef(true)

  useEffect(() => {
    mountedRef.current = true
    return () => {
      mountedRef.current = false
    }
  }, [])

  const run = useCallback(async (...args: A): Promise<R | null> => {
    setPending(true)
    setError(null)
    try {
      const result = await actionRef.current(...args)
      if (mountedRef.current) setPending(false)
      return result
    } catch (cause) {
      if (mountedRef.current) {
        setError(toError(cause))
        setPending(false)
      }
      return null
    }
  }, [])

  const reset = useCallback(() => setError(null), [])

  return { run, pending, error, reset }
}

/* ------------------------------------------------------------------ *
 * Toasts
 * ------------------------------------------------------------------ */

export type ToastApi = {
  items: ToastItem[]
  push: (tone: ToastTone, message: string) => void
  dismiss: (id: number) => void
}

export function useToasts(): ToastApi {
  const [items, setItems] = useState<ToastItem[]>([])
  const nextId = useRef(1)
  const timers = useRef<number[]>([])

  useEffect(
    () => () => {
      for (const timer of timers.current) window.clearTimeout(timer)
      timers.current = []
    },
    [],
  )

  const dismiss = useCallback((id: number) => {
    setItems((previous) => previous.filter((item) => item.id !== id))
  }, [])

  const push = useCallback(
    (tone: ToastTone, message: string) => {
      const id = nextId.current
      nextId.current += 1
      setItems((previous) => [...previous.slice(-4), { id, tone, message }])
      const timer = window.setTimeout(() => dismiss(id), tone === 'err' ? 7000 : 3800)
      timers.current.push(timer)
    },
    [dismiss],
  )

  return { items, push, dismiss }
}

/** Human-readable message from any thrown value. */
export function errorMessage(cause: unknown): string {
  return cause instanceof Error ? cause.message : String(cause)
}
