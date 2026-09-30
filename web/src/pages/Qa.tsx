/** 质检报告 — issues from GET /api/projects/{id}/qa. */

import { useCallback, useMemo, useState, type ReactNode } from 'react'
import { Link, useParams } from 'react-router-dom'
import {
  AlertOctagon,
  AlertTriangle,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  Gauge,
  Info,
  Play,
  RefreshCw,
  ShieldAlert,
} from 'lucide-react'
import { api } from '../api'
import type { Project, QaIssue, QaReport } from '../api'
import { errorMessage, useAction, useResource, useToasts } from '../hooks'
import { asText, cx, stageLabel, statusLabel, statusTone, truncate } from '../ui'
import { PageHeader } from '../components/PageHeader'
import {
  Badge,
  Button,
  EmptyState,
  ErrorState,
  LoadingState,
  Panel,
  Tabs,
  ToastStack,
} from '../components/ui'

type SeverityFilter = 'all' | 'error' | 'warning' | 'info'

const SEVERITY_ICON: Record<string, ReactNode> = {
  critical: <AlertOctagon size={12} />,
  error: <AlertOctagon size={12} />,
  warning: <AlertTriangle size={12} />,
  info: <Info size={12} />,
}

const SEVERITY_RANK: Record<string, number> = { critical: 0, error: 1, warning: 2, info: 3 }

function IssueRow({ issue }: { issue: QaIssue }): ReactNode {
  const [open, setOpen] = useState(false)
  const tone = statusTone(issue.severity)
  return (
    <div className={cx('border border-line/50 bg-abyss/40', tone === 'err' && 'border-err/35')}>
      <button
        type="button"
        onClick={() => setOpen((value) => !value)}
        className="flex w-full items-start gap-2 px-3 py-2 text-left"
      >
        <span className={cx('mt-0.5 shrink-0', tone === 'err' ? 'text-err' : tone === 'warn' ? 'text-warn' : 'text-info')}>
          {open ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
        </span>
        <span className="min-w-0 flex-1">
          <span className="flex flex-wrap items-center gap-1.5">
            <Badge tone={tone}>
              {SEVERITY_ICON[issue.severity] ?? <Info size={10} />}
              {statusLabel(issue.severity)}
            </Badge>
            <Badge tone="neutral">{stageLabel(issue.stage)}</Badge>
          </span>
          <span className="mt-1 block text-[11px] leading-relaxed text-ink-dim">{issue.message}</span>
        </span>
      </button>
      {open && issue.detail && (
        <pre className="scroll-thin max-h-64 overflow-auto border-t border-line/50 bg-void/70 px-3 py-2 font-mono text-[10px] leading-relaxed whitespace-pre-wrap text-ink-dim">
          {issue.detail}
        </pre>
      )}
    </div>
  )
}

export default function QaPage(): ReactNode {
  const { id = '' } = useParams()
  const toasts = useToasts()
  const [filter, setFilter] = useState<SeverityFilter>('all')
  const [stageFilter, setStageFilter] = useState<string>('all')

  const project = useResource<Project>((signal) => api.getProject(id, signal), [id])
  const qa = useResource<QaReport>((signal) => api.getQa(id, signal), [id])
  const runAll = useAction(api.runAll)

  const issues = useMemo(() => qa.data?.issues ?? [], [qa.data])

  const counts = useMemo(() => {
    const result = { critical: 0, error: 0, warning: 0, info: 0 }
    for (const issue of issues) {
      if (issue.severity === 'critical') result.critical += 1
      else if (issue.severity === 'error') result.error += 1
      else if (issue.severity === 'warning') result.warning += 1
      else result.info += 1
    }
    return result
  }, [issues])

  const stages = useMemo(() => [...new Set(issues.map((issue) => issue.stage))], [issues])

  const filtered = useMemo(() => {
    const list = issues.filter((issue) => {
      if (stageFilter !== 'all' && issue.stage !== stageFilter) return false
      if (filter === 'all') return true
      if (filter === 'error') return issue.severity === 'error' || issue.severity === 'critical'
      return issue.severity === filter
    })
    return [...list].sort(
      (a, b) => (SEVERITY_RANK[a.severity] ?? 9) - (SEVERITY_RANK[b.severity] ?? 9),
    )
  }, [issues, filter, stageFilter])

  const stats = useMemo(() => Object.entries(qa.data?.stats ?? {}), [qa.data])

  const rerun = useCallback(async () => {
    const result = await runAll.run(id)
    if (!result?.job_id) {
      toasts.push('err', `启动失败：${errorMessage(runAll.error)}`)
      return
    }
    toasts.push('ok', '完整流水线已启动，可前往「任务进度」查看。')
  }, [runAll, id, toasts])

  return (
    <div className="space-y-4">
      <PageHeader
        eyebrow={`QA / ${id.slice(0, 8)}`}
        title="质检报告"
        description="汇总各阶段的检查结果：占位符一致性、译文长度、字体覆盖、贴图残留等。严重问题会阻塞写回资源。"
        meta={
          <>
            <Badge tone={qa.data?.ok ? 'ok' : 'err'}>
              {qa.data?.ok ? <CheckCircle2 size={10} /> : <ShieldAlert size={10} />}
              {qa.data ? (qa.data.ok ? '通过' : '存在问题') : '未生成'}
            </Badge>
            <Badge tone="err">严重 {counts.critical + counts.error}</Badge>
            <Badge tone="warn">警告 {counts.warning}</Badge>
            <Badge tone="info">提示 {counts.info}</Badge>
            <Badge tone="neutral">
              <Gauge size={10} />
              {project.data?.name ?? id}
            </Badge>
          </>
        }
        actions={
          <>
            <Button icon={<RefreshCw size={12} />} busy={qa.loading} onClick={() => void qa.reload()}>
              重新生成报告
            </Button>
            <Button variant="primary" icon={<Play size={12} />} busy={runAll.pending} onClick={() => void rerun()}>
              运行完整流水线
            </Button>
          </>
        }
      />

      {qa.error && <ErrorState error={qa.error} onRetry={() => void qa.reload()} />}
      {runAll.error && <ErrorState error={runAll.error} />}
      {qa.loading && !qa.data && <LoadingState label="生成质检报告" />}

      {stats.length > 0 && (
        <Panel title="统计" subtitle="GET /api/projects/{id}/qa · stats" icon={<Gauge size={13} />}>
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
            {stats.map(([key, value]) => (
              <div key={key} className="border border-line/60 bg-abyss/45 px-3 py-2">
                <div className="nl-label truncate" title={key}>
                  {key}
                </div>
                <div className="mt-1 font-mono text-[16px] break-words text-cyan-soft">
                  {typeof value === 'object' && value !== null
                    ? truncate(asText(value), 60)
                    : asText(value) || '—'}
                </div>
              </div>
            ))}
          </div>
        </Panel>
      )}

      {qa.data && issues.length === 0 && (
        <EmptyState
          icon={<CheckCircle2 size={22} />}
          title="未发现问题"
          description="所有阶段检查均通过。可以在「扫描」页运行完整流水线以应用译文。"
          action={
            <Link
              to={`/scan/${id}`}
              className="border border-cyan/50 px-3 py-1 text-[11px] tracking-[0.1em] text-cyan-soft uppercase hover:bg-cyan/12"
            >
              返回扫描
            </Link>
          }
        />
      )}

      {issues.length > 0 && (
        <Panel
          pad={false}
          title="问题清单"
          subtitle={`${filtered.length} / ${issues.length}`}
          icon={<ShieldAlert size={13} />}
          bodyClassName="p-0"
        >
          <div className="flex flex-wrap items-center justify-between gap-2 border-b border-line/60 px-3 py-2">
            <Tabs
              value={filter}
              onChange={setFilter}
              items={[
                { value: 'all', label: '全部', count: issues.length },
                { value: 'error', label: '严重', count: counts.error + counts.critical },
                { value: 'warning', label: '警告', count: counts.warning },
                { value: 'info', label: '提示', count: counts.info },
              ]}
            />
            {stages.length > 1 && (
              <div className="flex flex-wrap items-center gap-1">
                <span className="nl-label mr-1">阶段</span>
                <button
                  type="button"
                  onClick={() => setStageFilter('all')}
                  className={cx(
                    'border px-1.5 py-[1px] text-[10px] uppercase',
                    stageFilter === 'all' ? 'border-cyan/50 text-cyan-soft' : 'border-line text-ink-faint',
                  )}
                >
                  全部
                </button>
                {stages.map((stage) => (
                  <button
                    key={stage}
                    type="button"
                    onClick={() => setStageFilter(stage)}
                    className={cx(
                      'border px-1.5 py-[1px] text-[10px] uppercase',
                      stageFilter === stage
                        ? 'border-cyan/50 text-cyan-soft'
                        : 'border-line text-ink-faint hover:text-ink-dim',
                    )}
                  >
                    {stageLabel(stage)}
                  </button>
                ))}
              </div>
            )}
          </div>

          <div className="space-y-2 p-3">
            {filtered.length === 0 ? (
              <p className="py-6 text-center text-[11px] text-ink-faint">当前筛选条件下没有问题。</p>
            ) : (
              filtered.map((issue, index) => (
                <IssueRow key={`${issue.stage}-${issue.message}-${index}`} issue={issue} />
              ))
            )}
          </div>
        </Panel>
      )}

      <ToastStack items={toasts.items} onDismiss={toasts.dismiss} />
    </div>
  )
}
