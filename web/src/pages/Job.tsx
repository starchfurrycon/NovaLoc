/** 任务进度 — live per-stage progress and a scrolling event log over WebSocket. */

import { useCallback, useEffect, useMemo, useState, type ReactNode } from 'react'
import { Link, useParams } from 'react-router-dom'
import {
  Activity,
  AlertOctagon,
  Ban,
  CheckCircle2,
  Clock,
  Download,
  PlugZap,
  RadioTower,
  RefreshCw,
  ScanLine,
  Trash2,
  WifiOff,
} from 'lucide-react'
import { api } from '../api'
import type { JobEvent, JobStatus } from '../api'
import { deriveStages, useJobSocket } from '../useJobSocket'
import { clockTime, shortTime, stageLabel, statusLabel, statusTone } from '../ui'
import { PageHeader } from '../components/PageHeader'
import { LogStream } from '../components/LogStream'
import {
  Badge,
  Button,
  CopyButton,
  ErrorState,
  KV,
  LoadingState,
  Panel,
  ProgressBar,
  ToastStack,
} from '../components/ui'
import { useToasts } from '../hooks'

const STATUS_TONE: Record<string, 'ok' | 'warn' | 'err' | 'info' | 'neutral'> = {
  done: 'ok',
  running: 'info',
  queued: 'warn',
  failed: 'err',
  cancelled: 'neutral',
}

function useElapsed(startedAt: string | null, finishedAt: string | null): string {
  const [, setTick] = useState(0)

  useEffect(() => {
    if (finishedAt) return
    const interval = window.setInterval(() => setTick((value) => value + 1), 1000)
    return () => window.clearInterval(interval)
  }, [finishedAt])

  return useMemo(() => {
    if (!startedAt) return '—'
    const start = new Date(startedAt).getTime()
    if (Number.isNaN(start)) return '—'
    const end = finishedAt ? new Date(finishedAt).getTime() : Date.now()
    const seconds = Math.max(0, Math.round((end - start) / 1000))
    const minutes = Math.floor(seconds / 60)
    const hours = Math.floor(minutes / 60)
    if (hours > 0) return `${hours}h ${minutes % 60}m ${seconds % 60}s`
    if (minutes > 0) return `${minutes}m ${seconds % 60}s`
    return `${seconds}s`
  }, [startedAt, finishedAt])
}

/** Derive a status when the REST snapshot is unavailable. */
function effectiveStatus(restStatus: JobStatus | undefined, events: JobEvent[]): JobStatus {
  if (restStatus) return restStatus
  for (let index = events.length - 1; index >= 0; index -= 1) {
    const event = events[index]!
    if (event.kind === 'stage_error') return 'failed'
    if (event.kind === 'stage_end') return 'done'
    if (event.kind === 'stage_start' || event.kind === 'progress') return 'running'
  }
  return events.length > 0 ? 'running' : 'queued'
}

export default function JobPage(): ReactNode {
  const { id: jobId = '' } = useParams()
  const toasts = useToasts()

  const {
    status: socketStatus,
    events,
    job,
    lastEvent,
    progress,
    error: socketError,
    attempts,
    reconnect,
    clear,
  } = useJobSocket(jobId, { maxEvents: 3000 })

  const [snapshotError, setSnapshotError] = useState<Error | null>(null)
  const [snapshotLoading, setSnapshotLoading] = useState(true)

  useEffect(() => {
    const controller = new AbortController()
    setSnapshotLoading(true)
    api.getJob(jobId, controller.signal).then(
      () => {
        if (controller.signal.aborted) return
        setSnapshotError(null)
        setSnapshotLoading(false)
      },
      (cause: unknown) => {
        if (controller.signal.aborted) return
        setSnapshotError(cause instanceof Error ? cause : new Error(String(cause)))
        setSnapshotLoading(false)
      },
    )
    return () => controller.abort()
  }, [jobId])

  const stages = useMemo(() => deriveStages(events), [events])
  const status = effectiveStatus(job?.status, events)
  const elapsed = useElapsed(job?.started_at ?? null, job?.finished_at ?? null)
  const tone = STATUS_TONE[status] ?? 'neutral'
  const statusIcon =
    status === 'failed' ? (
      <AlertOctagon size={13} />
    ) : status === 'done' ? (
      <CheckCircle2 size={13} />
    ) : status === 'cancelled' ? (
      <Ban size={13} />
    ) : (
      <Activity size={13} className={status === 'running' ? 'nl-pulse' : undefined} />
    )

  const headline = lastEvent?.message ?? job?.message ?? '等待事件…'
  const currentStage = lastEvent?.stage ?? job?.stage ?? ''
  const finishedAt = job?.finished_at ?? null

  const exportLog = useCallback(() => {
    const text = events
      .map((event) => `[${event.ts}] ${event.kind} ${event.stage} ${event.progress} ${event.message}`)
      .join('\n')
    const blob = new Blob([text], { type: 'text/plain;charset=utf-8' })
    const url = URL.createObjectURL(blob)
    const anchor = document.createElement('a')
    anchor.href = url
    anchor.download = `novaloc-job-${jobId.slice(0, 8)}.log`
    anchor.click()
    URL.revokeObjectURL(url)
    toasts.push('ok', '日志已导出。')
  }, [events, jobId, toasts])

  return (
    <div className="space-y-4">
      <PageHeader
        eyebrow={`JOB / ${jobId.slice(0, 8)}`}
        title="任务进度"
        description="实时事件来自 WebSocket ws://<host>/ws/jobs/{job_id}；断线会自动重连，并回退到 GET /api/jobs/{job_id} 轮询。"
        meta={
          <>
            <Badge tone={tone}>
              {statusIcon}
              {statusLabel(status)}
            </Badge>
            <Badge tone={socketStatus === 'open' ? 'ok' : socketStatus === 'connecting' ? 'warn' : 'err'}>
              <RadioTower size={10} />
              {socketStatus === 'open'
                ? '事件通道已连接'
                : socketStatus === 'connecting'
                  ? '连接中…'
                  : socketStatus === 'idle'
                    ? '未订阅'
                    : '通道断开'}
            </Badge>
            <Badge tone="neutral">
              <Activity size={10} />
              事件 {events.length}
            </Badge>
            <span className="font-mono text-[10px] text-ink-faint">job_id {jobId}</span>
          </>
        }
        actions={
          <>
            <Button icon={<PlugZap size={12} />} onClick={reconnect}>
              重新连接
            </Button>
            <Button icon={<RefreshCw size={12} />} onClick={() => window.location.reload()}>
              重新载入
            </Button>
            <Button icon={<Download size={12} />} disabled={events.length === 0} onClick={exportLog}>
              导出日志
            </Button>
            <Button icon={<Trash2 size={12} />} disabled={events.length === 0} onClick={clear}>
              清空事件
            </Button>
          </>
        }
      />

      {(socketError || snapshotError) && (
        <ErrorState
          error={socketError ?? snapshotError ?? new Error('未知错误')}
          onRetry={reconnect}
        />
      )}
      {snapshotLoading && !job && !socketError && <LoadingState label="读取任务快照" />}

      <div className="grid gap-3 xl:grid-cols-[minmax(0,1.35fr)_minmax(0,1fr)]">
        <Panel
          title="总体进度"
          subtitle={currentStage ? `当前阶段 ${stageLabel(currentStage)}` : undefined}
          icon={<Activity size={13} />}
          frame="nero"
        >
          <div className="space-y-3">
            <div className="flex items-end justify-between gap-3">
              <div className="min-w-0">
                <div className="font-mono text-[26px] leading-none text-cyan-soft">
                  {Math.round(progress * 100)}
                  <span className="text-[14px] text-ink-faint">%</span>
                </div>
                <p className="mt-1.5 truncate text-[11px] text-ink-dim" title={headline}>
                  {headline}
                </p>
              </div>
              <div className="shrink-0 text-right text-[10px] text-ink-faint">
                <div className="flex items-center justify-end gap-1">
                  <Clock size={10} />
                  已运行 {elapsed}
                </div>
                {job?.started_at && <div className="mt-0.5">开始 {shortTime(job.started_at)}</div>}
              </div>
            </div>
            <ProgressBar
              value={progress}
              tone={status === 'failed' ? 'err' : status === 'done' ? 'ok' : 'accent'}
              height={6}
              showValue
            />
            {job?.error && (
              <div className="border border-err/45 bg-err/8 px-2.5 py-2 text-[11px] text-err">
                {job.error}
              </div>
            )}
            <div className="grid gap-x-6 sm:grid-cols-2">
              <KV label="状态">{statusLabel(status)}</KV>
              <KV label="阶段">{stageLabel(currentStage) || '—'}</KV>
              <KV label="开始时间">{shortTime(job?.started_at ?? null)}</KV>
              <KV label="结束时间">{shortTime(finishedAt)}</KV>
              <KV label="最后事件">{clockTime(lastEvent?.ts ?? null)}</KV>
              <KV label="重连次数">{attempts}</KV>
            </div>
            <div className="flex items-center gap-2 border-t border-line/50 pt-2">
              <span className="nl-label">复制 job_id</span>
              <CopyButton value={jobId} label="复制" />
            </div>
          </div>
        </Panel>

        <Panel
          title="阶段进度"
          subtitle={`${stages.length} 个阶段已出现`}
          icon={<ScanLine size={13} />}
        >
          {stages.length === 0 ? (
            <p className="nl-pulse py-6 text-center text-[10px] tracking-[0.16em] text-ink-faint uppercase">
              等待阶段事件…
            </p>
          ) : (
            <ol className="space-y-2.5">
              {stages.map((stage, index) => (
                <li key={stage.stage} className="space-y-1">
                  <div className="flex items-center justify-between gap-2">
                    <div className="flex min-w-0 items-center gap-2">
                      <span className="font-mono text-[10px] text-ink-faint/70">
                        {String(index + 1).padStart(2, '0')}
                      </span>
                      <span className="truncate text-[11px] text-ink-dim">{stageLabel(stage.stage)}</span>
                      <Badge tone={statusTone(stage.status)}>
                        {stage.status === 'done'
                          ? '完成'
                          : stage.status === 'failed'
                            ? '失败'
                            : stage.status === 'running'
                              ? '进行中'
                              : '等待'}
                      </Badge>
                    </div>
                    <span className="shrink-0 font-mono text-[10px] text-ink-faint">
                      {Math.round(stage.progress * 100)}%
                    </span>
                  </div>
                  <ProgressBar
                    value={stage.progress}
                    tone={
                      stage.status === 'failed'
                        ? 'err'
                        : stage.status === 'done'
                          ? 'ok'
                          : stage.status === 'running'
                            ? 'accent'
                            : 'neutral'
                    }
                    height={3}
                  />
                  {stage.message && (
                    <p className="truncate text-[10px] text-ink-faint" title={stage.message}>
                      {stage.message}
                    </p>
                  )}
                </li>
              ))}
            </ol>
          )}
        </Panel>
      </div>

      <Panel
        title="事件日志"
        subtitle="kind ∈ log | progress | stage_start | stage_end | stage_error"
        icon={<WifiOff size={13} />}
      >
        <LogStream events={events} height={380} onClear={clear} />
      </Panel>

      <div className="flex flex-wrap items-center gap-2 text-[10px] text-ink-faint">
        <Link
          to="/"
          className="inline-flex items-center gap-1 border border-line px-2 py-0.5 uppercase hover:border-cyan/50 hover:text-cyan-soft"
        >
          返回项目列表
        </Link>
        {currentStage && (
          <span>
            当前阶段 <code className="text-cyan-dim">{currentStage}</code> · 事件通道{' '}
            <code className="text-cyan-dim">{socketStatus}</code>
          </span>
        )}
      </div>

      <ToastStack items={toasts.items} onDismiss={toasts.dismiss} />
    </div>
  )
}
