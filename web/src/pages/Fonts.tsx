/** 字体 — coverage audit and font patch plan/result. */

import { useCallback, useMemo, useState, type ReactNode } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import {
  AlertTriangle,
  BarChart3,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  FileType2,
  RefreshCw,
  ShieldCheck,
  Type,
} from 'lucide-react'
import { api } from '../api'
import type { FontCoverage, FontReport, Project } from '../api'
import { errorMessage, useAction, useResource, useToasts } from '../hooks'
import { asText, cx, pct } from '../ui'
import { PageHeader } from '../components/PageHeader'
import {
  Badge,
  Button,
  EmptyState,
  ErrorState,
  IconButton,
  LoadingState,
  Meter,
  Panel,
  ProgressBar,
  ToastStack,
} from '../components/ui'

function coverageTone(ratio: number): 'ok' | 'warn' | 'err' {
  if (ratio >= 0.995) return 'ok'
  if (ratio >= 0.9) return 'warn'
  return 'err'
}

function PatchCard({ patch, index }: { patch: Record<string, unknown>; index: number }): ReactNode {
  const [open, setOpen] = useState(false)
  const status = asText(patch['status'] ?? patch['result'] ?? '')
  const family = asText(patch['family'] ?? patch['font_id'] ?? patch['path'] ?? `补丁 ${index + 1}`)
  const added = patch['added_count'] ?? patch['added'] ?? patch['glyphs']
  const plan = patch['plan'] ?? patch['actions'] ?? patch['chars']
  const tone = status === 'applied' || status === 'done' || status === 'ok' ? 'ok' : status === 'failed' ? 'err' : 'warn'

  return (
    <div className="border border-line/60 bg-abyss/45">
      <button
        type="button"
        onClick={() => setOpen((value) => !value)}
        className="flex w-full items-center justify-between gap-3 px-3 py-2 text-left"
      >
        <span className="flex min-w-0 items-center gap-2">
          {open ? (
            <ChevronDown size={12} className="shrink-0 text-cyan/80" />
          ) : (
            <ChevronRight size={12} className="shrink-0 text-ink-faint" />
          )}
          <span className="min-w-0 truncate font-mono text-[11px] text-ink-dim">{family}</span>
        </span>
        <span className="flex shrink-0 items-center gap-1.5">
          {added !== undefined && <Badge tone="accent">+{asText(added)} 字形</Badge>}
          {status ? <Badge tone={tone}>{status}</Badge> : <Badge tone="neutral">已计划</Badge>}
        </span>
      </button>
      {open && (
        <div className="border-t border-line/50 px-3 py-2">
          {plan !== undefined && (
            <p className="mb-2 text-[11px] break-words text-ink-dim">
              {typeof plan === 'string' ? plan : asText(plan)}
            </p>
          )}
          <pre className="scroll-thin max-h-64 overflow-auto border border-line/50 bg-void/70 p-2 font-mono text-[10px] leading-relaxed text-ink-dim">
            {JSON.stringify(patch, null, 2)}
          </pre>
        </div>
      )}
    </div>
  )
}

function CoverageRow({ item }: { item: FontCoverage }): ReactNode {
  const tone = coverageTone(item.coverage_ratio)
  return (
    <tr className="border-b border-line/40 align-middle">
      <td className="px-3 py-2">
        <div className="flex items-center gap-2">
          <Type size={13} className="shrink-0 text-cyan/70" />
          <div className="min-w-0">
            <div className="truncate text-[11px] text-ink">{item.family || '（无 family 名）'}</div>
            <div className="truncate font-mono text-[10px] text-ink-faint" title={item.path}>
              {item.path}
            </div>
          </div>
        </div>
      </td>
      <td className="px-3 py-2 font-mono text-[10px] text-ink-faint">{item.font_id}</td>
      <td className="w-48 px-3 py-2">
        <div className="flex items-center gap-2">
          <ProgressBar value={item.coverage_ratio} tone={tone} className="flex-1" />
          <span
            className={cx(
              'w-12 shrink-0 text-right font-mono text-[11px]',
              tone === 'ok' && 'text-ok',
              tone === 'warn' && 'text-warn',
              tone === 'err' && 'text-err',
            )}
          >
            {pct(item.coverage_ratio)}
          </span>
        </div>
      </td>
      <td className="px-3 py-2 text-right">
        <span
          className={cx(
            'font-mono text-[11px]',
            item.missing_count === 0 ? 'text-ok' : item.missing_count > 200 ? 'text-err' : 'text-warn',
          )}
        >
          {item.missing_count}
        </span>
      </td>
      <td className="px-3 py-2">
        <Badge tone={tone}>
          {tone === 'ok' ? <CheckCircle2 size={10} /> : <AlertTriangle size={10} />}
          {tone === 'ok' ? '覆盖完整' : tone === 'warn' ? '存在缺字' : '缺字严重'}
        </Badge>
      </td>
    </tr>
  )
}

export default function FontsPage(): ReactNode {
  const { id = '' } = useParams()
  const navigate = useNavigate()
  const toasts = useToasts()

  const project = useResource<Project>((signal) => api.getProject(id, signal), [id])
  const fonts = useResource<FontReport>((signal) => api.getFonts(id, signal), [id])
  const audit = useAction(api.fonts)

  const projectName = project.data?.name ?? null

  const coverage = useMemo(() => fonts.data?.coverage ?? [], [fonts.data])
  const patches = useMemo(() => fonts.data?.patches ?? [], [fonts.data])

  const summary = useMemo(() => {
    if (coverage.length === 0) return null
    const worst = coverage.reduce(
      (acc, item) => (item.coverage_ratio < acc.coverage_ratio ? item : acc),
      coverage[0]!,
    )
    const missing = coverage.reduce((total, item) => total + item.missing_count, 0)
    const average =
      coverage.reduce((total, item) => total + item.coverage_ratio, 0) / coverage.length
    const incomplete = coverage.filter((item) => item.coverage_ratio < 0.995).length
    return { worst, missing, average, incomplete }
  }, [coverage])

  const runAudit = useCallback(async () => {
    const result = await audit.run(id)
    if (!result?.job_id) {
      toasts.push('err', `字体审计启动失败：${errorMessage(audit.error)}`)
      return
    }
    toasts.push('ok', `字体审计已启动（job ${result.job_id.slice(0, 8)}）。`)
    navigate(`/jobs/${result.job_id}`)
  }, [audit, id, navigate, toasts])

  return (
    <div className="space-y-4">
      <PageHeader
        eyebrow={`FONTS / ${projectName ?? id.slice(0, 8)}`}
        title="字体"
        description="审计目标语言字符集在游戏字体中的覆盖情况，并生成字体补丁计划。缺字会导致游戏内显示为方块或问号，请在写回资源前处理。"
        meta={
          <>
            <Badge tone="accent">
              <FileType2 size={10} />
              字体 {coverage.length}
            </Badge>
            {summary && (
              <>
                <Badge tone={coverageTone(summary.average)}>平均覆盖 {pct(summary.average)}</Badge>
                <Badge tone={summary.missing === 0 ? 'ok' : 'warn'}>缺失字形 {summary.missing}</Badge>
                <Badge tone={patches.length > 0 ? 'info' : 'neutral'}>补丁 {patches.length}</Badge>
              </>
            )}
          </>
        }
        actions={
          <>
            <Button
              variant="primary"
              icon={<ShieldCheck size={12} />}
              busy={audit.pending}
              onClick={() => void runAudit()}
            >
              运行字体审计
            </Button>
            <Button icon={<RefreshCw size={12} />} busy={fonts.loading} onClick={() => void fonts.reload()}>
              刷新
            </Button>
          </>
        }
      />

      {fonts.error && <ErrorState error={fonts.error} onRetry={() => void fonts.reload()} />}
      {audit.error && <ErrorState error={audit.error} />}
      {fonts.loading && !fonts.data && <LoadingState label="审计字体覆盖" />}

      {summary && (
        <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
          <Panel title="平均覆盖" icon={<BarChart3 size={13} />}>
            <Meter value={summary.average} label={`${coverage.length} 个字体文件`} />
          </Panel>
          <Panel title="覆盖不完整" icon={<AlertTriangle size={13} />}>
            <div className="font-mono text-[22px] text-warn">{summary.incomplete}</div>
            <p className="mt-1 text-[10px] text-ink-faint">覆盖率低于 99.5% 的字体数量</p>
          </Panel>
          <Panel title="缺失字形" icon={<Type size={13} />}>
            <div className="font-mono text-[22px] text-err">{summary.missing}</div>
            <p className="mt-1 text-[10px] text-ink-faint">所有字体缺失字符数合计</p>
          </Panel>
          <Panel title="最差字体" icon={<FileType2 size={13} />}>
            <div className="truncate text-[12px] text-ink-dim" title={summary.worst.path}>
              {summary.worst.family || summary.worst.font_id}
            </div>
            <div className="mt-1">
              <ProgressBar value={summary.worst.coverage_ratio} tone={coverageTone(summary.worst.coverage_ratio)} />
            </div>
            <p className="mt-1 text-[10px] text-ink-faint">
              {pct(summary.worst.coverage_ratio)} · 缺 {summary.worst.missing_count} 字
            </p>
          </Panel>
        </div>
      )}

      {fonts.data && coverage.length === 0 && (
        <EmptyState
          icon={<Type size={20} />}
          title="尚未审计字体"
          description="点击「运行字体审计」，后端会扫描游戏字体并比对目标语言字符集。"
          action={
            <Button variant="primary" size="sm" busy={audit.pending} onClick={() => void runAudit()}>
              运行字体审计
            </Button>
          }
        />
      )}

      {coverage.length > 0 && (
        <Panel
          pad={false}
          title="覆盖审计"
          subtitle={`GET /api/projects/${id}/fonts · coverage`}
          icon={<ShieldCheck size={13} />}
          actions={
            <IconButton label="刷新" icon={<RefreshCw size={12} />} onClick={() => void fonts.reload()} />
          }
        >
          <div className="scroll-thin overflow-x-auto">
            <table className="w-full min-w-[720px] border-collapse">
              <thead>
                <tr className="border-b border-line/70 text-left">
                  <th className="nl-label px-3 py-2 font-normal">字体 / 路径</th>
                  <th className="nl-label px-3 py-2 font-normal">font_id</th>
                  <th className="nl-label px-3 py-2 font-normal">覆盖率</th>
                  <th className="nl-label px-3 py-2 text-right font-normal">缺字数</th>
                  <th className="nl-label px-3 py-2 font-normal">判定</th>
                </tr>
              </thead>
              <tbody>
                {coverage.map((item) => (
                  <CoverageRow key={`${item.font_id}-${item.path}`} item={item} />
                ))}
              </tbody>
            </table>
          </div>
          <p className="border-t border-line/50 px-3 py-2 text-[10px] text-ink-faint">
            缺字清单由后端在补丁计划中给出；下方展开任意补丁即可查看原始 JSON。
          </p>
        </Panel>
      )}

      <Panel
        title="字体补丁计划 / 结果"
        subtitle="POST /api/projects/{id}/fonts 产物"
        icon={<FileType2 size={13} />}
        actions={
          <Button size="sm" icon={<RefreshCw size={11} />} onClick={() => void fonts.reload()}>
            刷新
          </Button>
        }
      >
        {patches.length === 0 ? (
          <EmptyState
            icon={<ShieldCheck size={18} />}
            title="暂无补丁计划"
            description="若所有字体覆盖率均为 100%，后端可能不会生成补丁。否则请运行字体审计。"
          />
        ) : (
          <div className="space-y-2">
            {patches.map((patch, index) => (
              <PatchCard key={index} patch={patch} index={index} />
            ))}
          </div>
        )}
      </Panel>

      <ToastStack items={toasts.items} onDismiss={toasts.dismiss} />
    </div>
  )
}
