/** 扫描 — engine detection and extraction overview. */

import { useCallback, useMemo, useState, type ReactNode } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import {
  Cpu,
  FileText,
  Gauge,
  Image as ImageIcon,
  Play,
  RefreshCw,
  ScanLine,
  Type,
  Upload,
  Wand2,
} from 'lucide-react'
import { api } from '../api'
import type { DetectResult, FontReport, ImagePage, Project, TextPage } from '../api'
import { errorMessage, useAction, useResource, useToasts } from '../hooks'
import { cx, kindLabel, relativeTime, shortTime, statusLabel, statusTone } from '../ui'
import { PageHeader } from '../components/PageHeader'
import {
  Badge,
  Button,
  EmptyState,
  ErrorState,
  KV,
  LoadingState,
  Panel,
  ProgressBar,
  StatusPill,
  ToastStack,
} from '../components/ui'

/** Minimum shape needed to launch a pipeline stage and follow its job. */
type JobAction = {
  run: (id: string) => Promise<{ job_id: string } | null>
  error: Error | null
  pending: boolean
}

const STAGE_CONTROLS: Array<{
  key: string
  label: string
  hint: string
  icon: ReactNode
  variant: 'primary' | 'ghost'
}> = [
  {
    key: 'extract',
    label: '提取文本',
    hint: '解析引擎资源，导出可翻译字符串',
    icon: <FileText size={13} />,
    variant: 'primary',
  },
  {
    key: 'translate',
    label: '机器翻译',
    hint: '调用本地 Ollama 模型批量翻译',
    icon: <Wand2 size={13} />,
    variant: 'primary',
  },
  {
    key: 'images',
    label: '贴图扫描',
    hint: 'OCR 识别贴图文字并生成标注图',
    icon: <ImageIcon size={13} />,
    variant: 'primary',
  },
  {
    key: 'fonts',
    label: '字体审计',
    hint: '检查字体覆盖并生成补丁计划',
    icon: <Type size={13} />,
    variant: 'primary',
  },
  {
    key: 'qa',
    label: '质检',
    hint: '查看质检报告中的问题清单',
    icon: <Gauge size={13} />,
    variant: 'ghost',
  },
  {
    key: 'apply',
    label: '写回资源',
    hint: '把译文与贴图写回游戏目录',
    icon: <Upload size={13} />,
    variant: 'primary',
  },
]

export default function ScanPage(): ReactNode {
  const { id = '' } = useParams()
  const navigate = useNavigate()
  const toasts = useToasts()
  const [detection, setDetection] = useState<DetectResult | null>(null)

  const project = useResource<Project>((signal) => api.getProject(id, signal), [id])
  const text = useResource<TextPage>((signal) => api.getText(id, {}, signal), [id])
  const images = useResource<ImagePage>((signal) => api.getImages(id, {}, signal), [id])
  const fonts = useResource<FontReport>((signal) => api.getFonts(id, signal), [id])

  const detect = useAction(api.detect)
  const extract = useAction(api.extract)
  const translate = useAction(api.translate)
  const scanImages = useAction(api.scanImages)
  const auditFonts = useAction(api.fonts)
  const runAll = useAction(api.runAll)

  const actions: Record<string, JobAction | null> = useMemo(
    () => ({
      extract,
      translate,
      images: scanImages,
      fonts: auditFonts,
      qa: null,
      apply: runAll,
    }),
    [extract, translate, scanImages, auditFonts, runAll],
  )

  const refreshAll = useCallback(() => {
    void project.reload({ quiet: true })
    void text.reload({ quiet: true })
    void images.reload({ quiet: true })
    void fonts.reload({ quiet: true })
  }, [project, text, images, fonts])

  /** Trigger a pipeline endpoint that returns `{job_id}` and jump to its progress page. */
  const launchJob = useCallback(
    async (action: JobAction, label: string): Promise<void> => {
      const result = await action.run(id)
      if (!result?.job_id) {
        toasts.push('err', `${label}启动失败：${errorMessage(action.error ?? '后端未返回 job_id')}`)
        return
      }
      toasts.push('ok', `${label}已启动。`)
      navigate(`/jobs/${result.job_id}`)
    },
    [id, navigate, toasts],
  )

  const runDetect = useCallback(async () => {
    const result = await detect.run(id)
    if (!result) {
      toasts.push('err', `引擎识别失败：${errorMessage(detect.error)}`)
      return
    }
    setDetection(result)
    toasts.push('ok', `识别为 ${result.display_name}。`)
  }, [detect, id, toasts])

  const textStats = useMemo(() => {
    const entries = text.data?.entries ?? []
    const byStatus = new Map<string, number>()
    const byKind = new Map<string, number>()
    let warnings = 0
    for (const entry of entries) {
      byStatus.set(entry.status, (byStatus.get(entry.status) ?? 0) + 1)
      byKind.set(entry.kind, (byKind.get(entry.kind) ?? 0) + 1)
      if (entry.warnings.length > 0) warnings += 1
    }
    return { byStatus, byKind, warnings }
  }, [text.data])

  const blockCount = useMemo(
    () => (images.data?.images ?? []).reduce((total, image) => total + image.blocks.length, 0),
    [images.data],
  )

  const coverageWorst = useMemo(() => {
    const list = fonts.data?.coverage ?? []
    if (list.length === 0) return null
    return list.reduce(
      (worst, item) => (item.coverage_ratio < worst.coverage_ratio ? item : worst),
      list[0]!,
    )
  }, [fonts.data])

  const busy =
    extract.pending || translate.pending || scanImages.pending || auditFonts.pending || runAll.pending

  const stageError =
    extract.error ?? translate.error ?? scanImages.error ?? auditFonts.error ?? runAll.error

  return (
    <div className="space-y-4">
      <PageHeader
        eyebrow={`SCAN / ${id.slice(0, 8)}`}
        title={project.data?.name ?? '扫描'}
        description="先确认引擎识别结果，再按需触发各阶段。所有阶段都会返回 job_id，可在「任务进度」页实时观察。"
        meta={
          <>
            <Badge tone="accent">
              <Cpu size={10} />
              {detection?.display_name ?? project.data?.engine ?? '未识别'}
            </Badge>
            <Badge tone="neutral">文本 {text.data?.total ?? 0}</Badge>
            <Badge tone="neutral">贴图 {images.data?.total ?? 0}</Badge>
            <Badge tone="neutral">OCR 图块 {blockCount}</Badge>
            {coverageWorst && (
              <Badge tone={coverageWorst.coverage_ratio >= 0.995 ? 'ok' : 'warn'}>
                最低字体覆盖 {(coverageWorst.coverage_ratio * 100).toFixed(1)}%
              </Badge>
            )}
          </>
        }
        actions={
          <>
            <Button icon={<RefreshCw size={12} />} onClick={refreshAll} busy={project.loading}>
              刷新
            </Button>
            <Button
              variant="primary"
              icon={<Play size={12} />}
              busy={runAll.pending}
              onClick={() => void launchJob(runAll, '完整流水线')}
            >
              运行完整流水线
            </Button>
          </>
        }
      />

      {project.error && <ErrorState error={project.error} onRetry={() => void project.reload()} />}
      {project.loading && !project.data && <LoadingState label="读取项目" />}

      <div className="grid gap-3 lg:grid-cols-[minmax(0,1fr)_minmax(0,1fr)]">
        <Panel
          title="引擎识别"
          subtitle="POST /api/projects/{id}/detect"
          icon={<Cpu size={13} />}
          frame="nero"
          actions={
            <Button
              size="sm"
              variant="primary"
              icon={<ScanLine size={11} />}
              busy={detect.pending}
              onClick={() => void runDetect()}
            >
              重新识别
            </Button>
          }
        >
          {detection ? (
            <div className="space-y-3">
              <div className="flex items-center justify-between gap-3">
                <div>
                  <div className="text-[14px] tracking-[0.1em] text-cyan-soft">
                    {detection.display_name}
                  </div>
                  <div className="font-mono text-[10px] text-ink-faint">{detection.engine_id}</div>
                </div>
                <div className="w-40">
                  <div className="mb-1 flex justify-between text-[10px] text-ink-faint">
                    <span>置信度</span>
                    <span>{(detection.confidence * 100).toFixed(1)}%</span>
                  </div>
                  <ProgressBar
                    value={detection.confidence}
                    tone={
                      detection.confidence >= 0.8 ? 'ok' : detection.confidence >= 0.5 ? 'warn' : 'err'
                    }
                  />
                </div>
              </div>
              <KV label="版本">{detection.version ?? '未知'}</KV>
              <div>
                <div className="nl-label mb-1">判定依据</div>
                {detection.evidence.length === 0 ? (
                  <p className="text-[11px] text-ink-faint">后端未提供判定依据。</p>
                ) : (
                  <ul className="space-y-1">
                    {detection.evidence.map((item, index) => (
                      <li
                        key={`${item}-${index}`}
                        className="flex gap-2 font-mono text-[11px] text-ink-dim"
                      >
                        <span className="text-cyan-dim">›</span>
                        <span className="min-w-0 break-words">{item}</span>
                      </li>
                    ))}
                  </ul>
                )}
              </div>
            </div>
          ) : (
            <EmptyState
              icon={<ScanLine size={20} />}
              title="尚未识别引擎"
              description={
                project.data?.engine
                  ? `项目记录中的引擎为 ${project.data.engine}。点击「重新识别」以获取置信度与判定依据。`
                  : '点击「重新识别」，后端会扫描游戏目录中的特征文件推断引擎类型。'
              }
              action={
                <Button variant="primary" size="sm" busy={detect.pending} onClick={() => void runDetect()}>
                  开始识别
                </Button>
              }
            />
          )}
          {detect.error && <ErrorState error={detect.error} className="mt-3" />}
        </Panel>

        <Panel
          title="提取摘要"
          subtitle="文本 / 贴图 / 字体统计"
          icon={<Gauge size={13} />}
          frame="nero"
          actions={
            <Button size="sm" icon={<RefreshCw size={11} />} onClick={refreshAll} busy={project.loading}>
              重新统计
            </Button>
          }
        >
          <div className="grid gap-4 sm:grid-cols-2">
            <div>
              <div className="nl-label mb-1.5 flex items-center gap-1.5">
                <FileText size={10} /> 文本状态
              </div>
              {textStats.byStatus.size === 0 ? (
                <p className="text-[11px] text-ink-faint">尚无文本条目。</p>
              ) : (
                <div className="space-y-1">
                  {[...textStats.byStatus.entries()].map(([entryStatus, count]) => (
                    <div key={entryStatus} className="flex items-center justify-between gap-2">
                      <StatusPill status={entryStatus} />
                      <span className="font-mono text-[11px] text-ink-dim">{count}</span>
                    </div>
                  ))}
                </div>
              )}
            </div>
            <div>
              <div className="nl-label mb-1.5 flex items-center gap-1.5">
                <FileText size={10} /> 文本类型
              </div>
              {textStats.byKind.size === 0 ? (
                <p className="text-[11px] text-ink-faint">尚无文本条目。</p>
              ) : (
                <div className="space-y-1">
                  {[...textStats.byKind.entries()].map(([entryKind, count]) => (
                    <div key={entryKind} className="flex items-center justify-between gap-2">
                      <span className="text-[11px] text-ink-dim">{kindLabel(entryKind)}</span>
                      <span className="font-mono text-[11px] text-ink-faint">{count}</span>
                    </div>
                  ))}
                </div>
              )}
            </div>
          </div>

          <div className="mt-3 grid grid-cols-3 gap-2 border-t border-line/50 pt-3">
            <button
              type="button"
              onClick={() => navigate(`/text/${id}`)}
              className="border border-line/70 px-2 py-2 text-left transition-colors hover:border-cyan/50"
            >
              <div className="nl-label">文本条目</div>
              <div className="font-mono text-[15px] text-cyan-soft">{text.data?.total ?? 0}</div>
              <div className="text-[10px] text-ink-faint">含警告 {textStats.warnings}</div>
            </button>
            <button
              type="button"
              onClick={() => navigate(`/images/${id}`)}
              className="border border-line/70 px-2 py-2 text-left transition-colors hover:border-cyan/50"
            >
              <div className="nl-label">贴图资源</div>
              <div className="font-mono text-[15px] text-cyan-soft">{images.data?.total ?? 0}</div>
              <div className="text-[10px] text-ink-faint">OCR 图块 {blockCount}</div>
            </button>
            <button
              type="button"
              onClick={() => navigate(`/fonts/${id}`)}
              className="border border-line/70 px-2 py-2 text-left transition-colors hover:border-cyan/50"
            >
              <div className="nl-label">字体文件</div>
              <div className="font-mono text-[15px] text-cyan-soft">
                {fonts.data?.coverage.length ?? 0}
              </div>
              <div className="text-[10px] text-ink-faint">补丁 {fonts.data?.patches.length ?? 0}</div>
            </button>
          </div>

          {textStats.warnings > 0 && (
            <div className="mt-3 border border-warn/40 bg-warn/8 px-2.5 py-2 text-[11px] text-warn">
              有 {textStats.warnings} 条译文带占位符或格式警告，请在「文本审校」中逐条确认。
            </div>
          )}
        </Panel>
      </div>

      <Panel
        title="阶段控制"
        subtitle="每个阶段独立运行，也可串联为完整流水线"
        icon={<Wand2 size={13} />}
      >
        <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-3">
          {STAGE_CONTROLS.map((item) => {
            const action = actions[item.key] ?? null
            return (
              <div
                key={item.key}
                className="flex items-start justify-between gap-3 border border-line/60 bg-abyss/40 px-3 py-2.5"
              >
                <div className="min-w-0">
                  <div className="flex items-center gap-2 text-[12px] tracking-[0.08em] text-ink">
                    <span className="text-cyan/80">{item.icon}</span>
                    {item.label}
                  </div>
                  <p className="mt-0.5 text-[10px] leading-relaxed text-ink-faint">{item.hint}</p>
                </div>
                <Button
                  size="sm"
                  variant={item.variant}
                  busy={action ? action.pending : false}
                  disabled={busy && item.key !== 'qa'}
                  onClick={() => {
                    if (item.key === 'qa') {
                      navigate(`/qa/${id}`)
                      return
                    }
                    if (action) void launchJob(action, item.label)
                  }}
                >
                  运行
                </Button>
              </div>
            )
          })}
        </div>
        {stageError && <ErrorState error={stageError} className="mt-3" />}
      </Panel>

      <Panel title="项目元数据" icon={<Cpu size={13} />}>
        <div className="grid gap-x-6 sm:grid-cols-2">
          <KV label="项目 ID">{id}</KV>
          <KV label="引擎">{project.data?.engine ?? '未识别'}</KV>
          <KV label="引擎版本">{project.data?.engine_version ?? '未知'}</KV>
          <KV label="游戏目录">{project.data?.game_dir ?? '—'}</KV>
          <KV label="创建时间">{shortTime(project.data?.created_at)}</KV>
          <KV label="更新时间">{relativeTime(project.data?.updated_at)}</KV>
        </div>
        {project.data && <ProgressRow progress={project.data.progress ?? {}} />}
      </Panel>

      <ToastStack items={toasts.items} onDismiss={toasts.dismiss} />
    </div>
  )
}

function ProgressRow({ progress }: { progress: Record<string, unknown> }): ReactNode {
  const stage = typeof progress['stage'] === 'string' ? progress['stage'] : ''
  const ratio = Number(progress['progress'] ?? progress['ratio'] ?? 0)
  const status = typeof progress['status'] === 'string' ? progress['status'] : ''
  if (!stage && !status) return null
  const tone = statusTone(status)
  return (
    <div className={cx('mt-3 border-t border-line/50 pt-3')}>
      <div className="mb-1 flex items-center justify-between gap-2 text-[11px]">
        <span className="text-ink-dim">
          {stage || '任务'} · {statusLabel(status)}
        </span>
        <span className="font-mono text-ink-faint">{Math.round(ratio * 100)}%</span>
      </div>
      <ProgressBar value={ratio} tone={tone === 'neutral' ? 'accent' : tone} />
    </div>
  )
}
