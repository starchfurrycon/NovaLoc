/** 鎵弿 鈥?engine detection and extraction overview. */

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

  const refreshAll = useCallback(() => {
    void project.reload({ quiet: true })
    void text.reload({ quiet: true })
    void images.reload({ quiet: true })
    void fonts.reload({ quiet: true })
  }, [project, text, images, fonts])

  /** Trigger a pipeline endpoint that returns `{job_id}` and jump to its progress page. */
  const launchJob = useCallback(
    async (
      action: { run: (id: string) => Promise<{ job_id: string } | null>; error: Error | null },
      label: string,
    ): Promise<void> => {
      const result = await action.run(id)
      if (!result?.job_id) {
        toasts.push('err', `${label}鍚姩澶辫触锛?{errorMessage(action.error ?? '鍚庣鏈繑鍥?job_id')}`)
        return
      }
      toasts.push('ok', `${label}宸插惎鍔ㄣ€俙)
      navigate(`/jobs/${result.job_id}`)
    },
    [id, navigate, toasts],
  )

  const runDetect = useCallback(async () => {
    const result = await detect.run(id)
    if (!result) {
      toasts.push('err', `寮曟搸璇嗗埆澶辫触锛?{errorMessage(detect.error)}`)
      return
    }
    setDetection(result)
    toasts.push('ok', `璇嗗埆涓?${result.display_name}銆俙)
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
    return list.reduce((worst, item) => (item.coverage_ratio < worst.coverage_ratio ? item : worst), list[0]!)
  }, [fonts.data])

  const busy =
    extract.pending || translate.pending || scanImages.pending || auditFonts.pending || runAll.pending

  return (
    <div className="space-y-4">
      <PageHeader
        eyebrow={`SCAN / ${id.slice(0, 8)}`}
        title={project.data?.name ?? '鎵弿'}
        description="鍏堢‘璁ゅ紩鎿庤瘑鍒粨鏋滐紝鍐嶆寜闇€瑙﹀彂鍚勯樁娈点€傛墍鏈夐樁娈甸兘浼氳繑鍥?job_id锛屽彲鍦ㄣ€屼换鍔¤繘搴︺€嶉〉瀹炴椂瑙傚療銆?
        meta={
          <>
            <Badge tone="accent">
              <Cpu size={10} />
              {detection?.display_name ?? project.data?.engine ?? '鏈瘑鍒?}
            </Badge>
            <Badge tone="neutral">鏂囨湰 {text.data?.total ?? 0}</Badge>
            <Badge tone="neutral">璐村浘 {images.data?.total ?? 0}</Badge>
            <Badge tone="neutral">OCR 鍥惧潡 {blockCount}</Badge>
            {coverageWorst && (
              <Badge tone={coverageWorst.coverage_ratio >= 0.995 ? 'ok' : 'warn'}>
                鏈€浣庡瓧浣撹鐩?{(coverageWorst.coverage_ratio * 100).toFixed(1)}%
              </Badge>
            )}
          </>
        }
        actions={
          <>
            <Button icon={<RefreshCw size={12} />} onClick={refreshAll} busy={project.loading}>
              鍒锋柊
            </Button>
            <Button
              variant="primary"
              icon={<Play size={12} />}
              busy={runAll.pending}
              onClick={() => void launchJob(runAll, '瀹屾暣娴佹按绾?)}
            >
              杩愯瀹屾暣娴佹按绾?            </Button>
          </>
        }
      />

      {project.error && <ErrorState error={project.error} onRetry={() => void project.reload()} />}
      {project.loading && !project.data && <LoadingState label="璇诲彇椤圭洰" />}

      <div className="grid gap-3 lg:grid-cols-[minmax(0,1fr)_minmax(0,1fr)]">
        <Panel
          title="寮曟搸璇嗗埆"
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
              閲嶆柊璇嗗埆
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
                    <span>缃俊搴?/span>
                    <span>{(detection.confidence * 100).toFixed(1)}%</span>
                  </div>
                  <ProgressBar
                    value={detection.confidence}
                    tone={detection.confidence >= 0.8 ? 'ok' : detection.confidence >= 0.5 ? 'warn' : 'err'}
                  />
                </div>
              </div>
              <KV label="鐗堟湰">{detection.version ?? '鏈煡'}</KV>
              <div>
                <div className="nl-label mb-1">鍒ゅ畾渚濇嵁</div>
                {detection.evidence.length === 0 ? (
                  <p className="text-[11px] text-ink-faint">鍚庣鏈彁渚涘垽瀹氫緷鎹€?/p>
                ) : (
                  <ul className="space-y-1">
                    {detection.evidence.map((item, index) => (
                      <li key={`${item}-${index}`} className="flex gap-2 font-mono text-[11px] text-ink-dim">
                        <span className="text-cyan-dim">鈥?/span>
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
              title="灏氭湭璇嗗埆寮曟搸"
              description={
                project.data?.engine
                  ? `椤圭洰璁板綍涓殑寮曟搸涓?${project.data.engine}銆傜偣鍑汇€岄噸鏂拌瘑鍒€嶄互鑾峰彇缃俊搴︿笌鍒ゅ畾渚濇嵁銆俙
                  : '鐐瑰嚮銆岄噸鏂拌瘑鍒€嶏紝鍚庣浼氭壂鎻忔父鎴忕洰褰曚腑鐨勭壒寰佹枃浠舵帹鏂紩鎿庣被鍨嬨€?
              }
              action={
                <Button variant="primary" size="sm" busy={detect.pending} onClick={() => void runDetect()}>
                  寮€濮嬭瘑鍒?                </Button>
              }
            />
          )}
          {detect.error && <ErrorState error={detect.error} className="mt-3" />}
        </Panel>

        <Panel
          title="鎻愬彇鎽樿"
          subtitle="鏂囨湰 / 璐村浘 / 瀛椾綋缁熻"
          icon={<Gauge size={13} />}
          frame="nero"
          actions={
            <Button size="sm" icon={<RefreshCw size={11} />} onClick={refreshAll} busy={project.loading}>
              閲嶆柊缁熻
            </Button>
          }
        >
          <div className="grid gap-4 sm:grid-cols-2">
            <div>
              <div className="nl-label mb-1.5 flex items-center gap-1.5">
                <FileText size={10} /> 鏂囨湰鐘舵€?              </div>
              {textStats.byStatus.size === 0 ? (
                <p className="text-[11px] text-ink-faint">灏氭棤鏂囨湰鏉＄洰銆?/p>
              ) : (
                <div className="space-y-1">
                  {[...textStats.byStatus.entries()].map(([status, count]) => (
                    <div key={status} className="flex items-center justify-between gap-2">
                      <StatusPill status={status} />
                      <span className="font-mono text-[11px] text-ink-dim">{count}</span>
                    </div>
                  ))}
                </div>
              )}
            </div>
            <div>
              <div className="nl-label mb-1.5 flex items-center gap-1.5">
                <FileText size={10} /> 鏂囨湰绫诲瀷
              </div>
              {textStats.byKind.size === 0 ? (
                <p className="text-[11px] text-ink-faint">灏氭棤鏂囨湰鏉＄洰銆?/p>
              ) : (
                <div className="space-y-1">
                  {[...textStats.byKind.entries()].map(([kind, count]) => (
                    <div key={kind} className="flex items-center justify-between gap-2">
                      <span className="text-[11px] text-ink-dim">{kindLabel(kind)}</span>
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
              <div className="nl-label">鏂囨湰鏉＄洰</div>
              <div className="font-mono text-[15px] text-cyan-soft">{text.data?.total ?? 0}</div>
              <div className="text-[10px] text-ink-faint">鍚鍛?{textStats.warnings}</div>
            </button>
            <button
              type="button"
              onClick={() => navigate(`/images/${id}`)}
              className="border border-line/70 px-2 py-2 text-left transition-colors hover:border-cyan/50"
            >
              <div className="nl-label">璐村浘璧勬簮</div>
              <div className="font-mono text-[15px] text-cyan-soft">{images.data?.total ?? 0}</div>
              <div className="text-[10px] text-ink-faint">OCR 鍥惧潡 {blockCount}</div>
            </button>
            <button
              type="button"
              onClick={() => navigate(`/fonts/${id}`)}
              className="border border-line/70 px-2 py-2 text-left transition-colors hover:border-cyan/50"
            >
              <div className="nl-label">瀛椾綋鏂囦欢</div>
              <div className="font-mono text-[15px] text-cyan-soft">
                {fonts.data?.coverage.length ?? 0}
              </div>
              <div className="text-[10px] text-ink-faint">琛ヤ竵 {fonts.data?.patches.length ?? 0}</div>
            </button>
          </div>

          {textStats.warnings > 0 && (
            <div className="mt-3 border border-warn/40 bg-warn/8 px-2.5 py-2 text-[11px] text-warn">
              鏈?{textStats.warnings} 鏉¤瘧鏂囧甫鍗犱綅绗︽垨鏍煎紡璀﹀憡锛岃鍦ㄣ€屾枃鏈鏍°€嶄腑閫愭潯纭銆?            </div>
          )}
        </Panel>
      </div>

      <Panel title="闃舵鎺у埗" subtitle="姣忎釜闃舵鐙珛杩愯锛屼篃鍙覆鑱斾负瀹屾暣娴佹按绾? icon={<Wand2 size={13} />}>
        <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-3">
          {[
            {
              key: 'extract',
              label: '鎻愬彇鏂囨湰',
              hint: '瑙ｆ瀽寮曟搸璧勬簮锛屽鍑哄彲缈昏瘧瀛楃涓?,
              icon: <FileText size={13} />,
              action: extract,
              run: () => launchJob(extract, '鏂囨湰鎻愬彇'),
            },
            {
              key: 'translate',
              label: '鏈哄櫒缈昏瘧',
              hint: '璋冪敤鏈湴 Ollama 妯″瀷鎵归噺缈昏瘧',
              icon: <Wand2 size={13} />,
              action: translate,
              run: () => launchJob(translate, '鏈哄櫒缈昏瘧'),
            },
            {
              key: 'images',
              label: '璐村浘鎵弿',
              hint: 'OCR 璇嗗埆璐村浘鏂囧瓧骞剁敓鎴愭爣娉ㄥ浘',
              icon: <ImageIcon size={13} />,
              action: scanImages,
              run: () => launchJob(scanImages, '璐村浘鎵弿'),
            },
            {
              key: 'fonts',
              label: '瀛椾綋瀹¤',
              hint: '妫€鏌ュ瓧浣撹鐩栧苟鐢熸垚琛ヤ竵璁″垝',
              icon: <Type size={13} />,
              action: auditFonts,
              run: () => launchJob(auditFonts, '瀛椾綋瀹¤'),
            },
            {
              key: 'qa',
              label: '璐ㄦ',
              hint: '鏌ョ湅璐ㄦ鎶ュ憡涓殑闂娓呭崟',
              icon: <Gauge size={13} />,
              action: null,
              run: async () => navigate(`/qa/${id}`),
            },
            {
              key: 'apply',
              label: '鍐欏洖璧勬簮',
              hint: '鎶婅瘧鏂囦笌璐村浘鍐欏洖娓告垙鐩綍',
              icon: <Upload size={13} />,
              action: runAll,
              run: () => launchJob(runAll, '瀹屾暣娴佹按绾?),
            },
          ].map((item) => (
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
                variant={item.key === 'qa' ? 'ghost' : 'primary'}
                busy={item.action ? Boolean(item.action.pending) : false}
                disabled={busy && item.key !== 'qa'}
                onClick={() => void item.run()}
              >
                杩愯
              </Button>
            </div>
          ))}
        </div>
        {(extract.error || translate.error || scanImages.error || auditFonts.error || runAll.error) && (
          <ErrorState
            error={
              (extract.error ??
                translate.error ??
                scanImages.error ??
                auditFonts.error ??
                runAll.error) as Error
            }
            className="mt-3"
          />
        )}
      </Panel>

      <Panel title="椤圭洰鍏冩暟鎹? icon={<Cpu size={13} />}>
        <div className="grid gap-x-6 sm:grid-cols-2">
          <KV label="椤圭洰 ID">{id}</KV>
          <KV label="寮曟搸">{project.data?.engine ?? '鏈瘑鍒?}</KV>
          <KV label="寮曟搸鐗堟湰">{project.data?.engine_version ?? '鏈煡'}</KV>
          <KV label="娓告垙鐩綍">{project.data?.game_dir ?? '鈥?}</KV>
          <KV label="鍒涘缓鏃堕棿">{shortTime(project.data?.created_at)}</KV>
          <KV label="鏇存柊鏃堕棿">{relativeTime(project.data?.updated_at)}</KV>
        </div>
        {project.data && <ProgressRow progress={project.data.progress} />}
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
          {stage || '浠诲姟'} 路 {statusLabel(status)}
        </span>
        <span className="font-mono text-ink-faint">{Math.round(ratio * 100)}%</span>
      </div>
      <ProgressBar value={ratio} tone={tone === 'neutral' ? 'accent' : tone} />
    </div>
  )
}
