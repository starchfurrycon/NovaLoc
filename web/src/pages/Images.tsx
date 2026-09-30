/** 贴图审校 — image asset grid with OCR blocks and annotated PNG preview. */

import { useCallback, useEffect, useMemo, useState, type ReactNode } from 'react'
import { useParams } from 'react-router-dom'
import {
  Crosshair,
  Image as ImageIcon,
  Images,
  Maximize2,
  RefreshCw,
  RotateCcw,
  Save,
  ScanSearch,
  Search,
} from 'lucide-react'
import { api } from '../api'
import type { ImageAsset, ImageBlock, ImagePage, ImagePatchItem, Project } from '../api'
import { errorMessage, useAction, useResource, useToasts } from '../hooks'
import { cx, truncate } from '../ui'
import { PageHeader } from '../components/PageHeader'
import {
  Badge,
  Button,
  EmptyState,
  ErrorState,
  IconButton,
  Input,
  LoadingState,
  Modal,
  Panel,
  StatusPill,
  TextArea,
  ToastStack,
} from '../components/ui'

export default function ImagesPage(): ReactNode {
  const { id = '' } = useParams()
  const toasts = useToasts()

  const [query, setQuery] = useState('')
  const [limit, setLimit] = useState(60)
  const [active, setActive] = useState<ImageAsset | null>(null)
  /** `${uid}:${blockId}` → edited target */
  const [drafts, setDrafts] = useState<Record<string, string>>({})
  const [showAnnotated, setShowAnnotated] = useState(true)

  const project = useResource<Project>((signal) => api.getProject(id, signal), [id])
  const images = useResource<ImagePage>(
    (signal) => api.getImages(id, { limit }, signal),
    [id, limit],
  )
  const scan = useAction(api.scanImages)
  const save = useAction(api.patchImages)

  const assets = useMemo(() => images.data?.images ?? [], [images.data])

  const filtered = useMemo(() => {
    const needle = query.trim().toLowerCase()
    if (!needle) return assets
    return assets.filter((asset) => {
      if (asset.path.toLowerCase().includes(needle) || asset.uid.toLowerCase().includes(needle)) {
        return true
      }
      return asset.blocks.some(
        (block) =>
          block.source.toLowerCase().includes(needle) ||
          block.target.toLowerCase().includes(needle),
      )
    })
  }, [assets, query])

  const dirtyEntries = useMemo(() => {
    const list: ImagePatchItem[] = []
    for (const [key, value] of Object.entries(drafts)) {
      const [uid, blockId] = key.split(':', 2)
      if (!uid || !blockId) continue
      const asset = assets.find((item) => item.uid === uid)
      const block = asset?.blocks.find((item) => item.id === blockId)
      if (block && block.target !== value) list.push({ uid, block_id: blockId, target: value })
    }
    return list
  }, [drafts, assets])

  const blockDraftKey = (uid: string, blockId: string) => `${uid}:${blockId}`

  const blockValue = useCallback(
    (asset: ImageAsset, block: ImageBlock) => drafts[blockDraftKey(asset.uid, block.id)] ?? block.target,
    [drafts],
  )

  const totalBlocks = useMemo(
    () => assets.reduce((total, asset) => total + asset.blocks.length, 0),
    [assets],
  )

  const commit = useCallback(
    async (items: ImagePatchItem[]) => {
      if (items.length === 0) return
      const result = await save.run(id, items)
      if (!result) {
        toasts.push('err', `保存失败：${errorMessage(save.error)}`)
        return
      }
      setDrafts((previous) => {
        const next = { ...previous }
        for (const item of items) delete next[blockDraftKey(item.uid, item.block_id)]
        return next
      })
      toasts.push('ok', `已更新 ${result.updated} 个图块。`)
      await images.reload({ quiet: true })
    },
    [id, save, toasts, images],
  )

  // Keep the open modal in sync with refreshed data.
  useEffect(() => {
    if (!active) return
    const updated = assets.find((asset) => asset.uid === active.uid)
    if (updated && updated !== active) setActive(updated)
  }, [assets, active])

  const runScan = useCallback(async () => {
    const result = await scan.run(id)
    if (!result?.job_id) {
      toasts.push('err', `贴图扫描启动失败：${errorMessage(scan.error)}`)
      return
    }
    toasts.push('ok', `贴图扫描已启动（job ${result.job_id.slice(0, 8)}）。`)
  }, [scan, toasts, id])

  return (
    <div className="space-y-4">
      <PageHeader
        eyebrow={`IMAGES / ${id.slice(0, 8)}`}
        title="贴图审校"
        description="逐张贴图核对 OCR 结果。标注图中的方框编号与下方图块列表一一对应；修改后可直接写回，不会覆盖原文件。"
        meta={
          <>
            <Badge tone="accent">
              <Images size={10} />
              贴图 {images.data?.total ?? assets.length}
            </Badge>
            <Badge tone="neutral">
              <ScanSearch size={10} />
              OCR 图块 {totalBlocks}
            </Badge>
            {dirtyEntries.length > 0 && (
              <Badge tone="info">
                <Save size={10} />
                未保存 {dirtyEntries.length}
              </Badge>
            )}
          </>
        }
        actions={
          <>
            <Button
              variant="primary"
              icon={<ScanSearch size={12} />}
              busy={scan.pending}
              onClick={() => void runScan()}
            >
              运行贴图扫描
            </Button>
            <Button icon={<RefreshCw size={12} />} busy={images.loading} onClick={() => void images.reload()}>
              刷新
            </Button>
            <Button
              variant={dirtyEntries.length > 0 ? 'primary' : 'ghost'}
              icon={<Save size={12} />}
              disabled={dirtyEntries.length === 0}
              busy={save.pending}
              onClick={() => void commit(dirtyEntries)}
            >
              保存全部{dirtyEntries.length > 0 ? ` (${dirtyEntries.length})` : ''}
            </Button>
          </>
        }
      />

      <Panel pad={false} bodyClassName="p-3">
        <div className="flex flex-wrap items-center gap-2">
          <div className="flex min-w-[240px] flex-1 items-center gap-2 border border-line bg-abyss/80 px-2">
            <Search size={12} className="shrink-0 text-ink-faint" />
            <input
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder="搜索贴图路径 / UID / 源文 / 译文"
              className="w-full bg-transparent py-1.5 font-mono text-[12px] text-ink placeholder:text-ink-faint/70 focus:outline-none"
            />
          </div>
          <Button
            icon={<Crosshair size={12} />}
            variant={showAnnotated ? 'primary' : 'ghost'}
            onClick={() => setShowAnnotated((value) => !value)}
          >
            标注图覆盖
          </Button>
          <Button
            variant="ghost"
            icon={<RotateCcw size={12} />}
            disabled={dirtyEntries.length === 0}
            onClick={() => setDrafts({})}
          >
            放弃修改
          </Button>
          <div className="ml-auto flex items-center gap-2">
            <span className="nl-label">渲染上限</span>
            <div className="w-24">
              <Input
                type="number"
                min={12}
                step={12}
                value={limit}
                onChange={(event) => {
                  const next = Number.parseInt(event.target.value, 10)
                  if (Number.isFinite(next) && next >= 12) setLimit(next)
                }}
              />
            </div>
          </div>
        </div>
      </Panel>

      {images.error && <ErrorState error={images.error} onRetry={() => void images.reload()} />}
      {scan.error && <ErrorState error={scan.error} />}
      {save.error && <ErrorState error={save.error} />}
      {images.loading && !images.data && <LoadingState label="读取贴图资源" />}

      {images.data && filtered.length === 0 && (
        <EmptyState
          icon={<ImageIcon size={20} />}
          title="没有贴图资源"
          description="请在「扫描」页运行贴图扫描，识别出的贴图与 OCR 图块会显示在这里。"
        />
      )}

      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
        {filtered.map((asset) => {
          const assetDirty = dirtyEntries.filter((item) => item.uid === asset.uid).length
          return (
            <Panel key={asset.uid} frame="corners" pad={false} bodyClassName="p-0">
              <button
                type="button"
                onClick={() => setActive(asset)}
                className="relative block w-full overflow-hidden border-b border-line/60 bg-void/70"
                style={{ aspectRatio: '16 / 9' }}
                title="点击查看标注图与图块"
              >
                {showAnnotated ? (
                  <img
                    src={api.annotatedUrl(id, asset.uid)}
                    alt={asset.path}
                    loading="lazy"
                    className="h-full w-full object-contain"
                  />
                ) : (
                  <span className="nl-mono-grid flex h-full w-full items-center justify-center text-ink-faint/60">
                    <ImageIcon size={22} />
                  </span>
                )}
                <span className="absolute top-1.5 right-1.5 inline-flex items-center gap-1 border border-line bg-void/85 px-1.5 py-[1px] text-[9px] tracking-[0.1em] text-ink-dim uppercase">
                  <Maximize2 size={9} /> 详情
                </span>
              </button>

              <div className="space-y-2 p-2.5">
                <p className="truncate font-mono text-[10px] text-ink-dim" title={asset.path}>
                  {asset.path}
                </p>
                <div className="flex flex-wrap items-center gap-1.5">
                  <Badge tone="neutral">
                    {asset.width}×{asset.height}
                  </Badge>
                  <Badge tone={asset.blocks.length > 0 ? 'accent' : 'neutral'}>
                    图块 {asset.blocks.length}
                  </Badge>
                  {assetDirty > 0 && <Badge tone="info">待保存 {assetDirty}</Badge>}
                  <span className="font-mono text-[9px] text-ink-faint/70">{asset.uid.slice(0, 8)}</span>
                </div>

                {asset.blocks.length > 0 ? (
                  <ul className="space-y-1">
                    {asset.blocks.slice(0, 3).map((block) => (
                      <li
                        key={block.id}
                        className="flex items-start gap-2 border-l border-cyan/30 pl-2 text-[10px] leading-snug"
                      >
                        <span className="min-w-0 flex-1">
                          <span className="block truncate text-ink-dim" title={block.source}>
                            {truncate(block.source || '（空）', 40)}
                          </span>
                          <span className="block truncate text-cyan-soft/90" title={blockValue(asset, block)}>
                            {truncate(blockValue(asset, block) || '（未翻译）', 40)}
                          </span>
                        </span>
                        <StatusPill status={block.status} />
                      </li>
                    ))}
                    {asset.blocks.length > 3 && (
                      <li className="pl-2 text-[10px] text-ink-faint">
                        还有 {asset.blocks.length - 3} 个图块…
                      </li>
                    )}
                  </ul>
                ) : (
                  <p className="text-[10px] text-ink-faint">未识别到文字图块。</p>
                )}
              </div>
            </Panel>
          )
        })}
      </div>

      <Modal
        open={active !== null}
        wide
        onClose={() => setActive(null)}
        title={active ? `贴图 ${active.uid.slice(0, 8)} · ${active.width}×${active.height}` : '贴图'}
      >
        {active && (
          <div className="grid gap-4 lg:grid-cols-[minmax(0,1.15fr)_minmax(0,1fr)]">
            <div className="space-y-2">
              <div className="border border-line/70 bg-void/80 p-1">
                <img
                  src={api.annotatedUrl(id, active.uid)}
                  alt={`${active.path} 标注图`}
                  className="mx-auto max-h-[60vh] w-auto object-contain"
                />
              </div>
              <div className="space-y-1">
                <p className="font-mono text-[10px] break-all text-ink-dim">{active.path}</p>
                <div className="flex flex-wrap items-center gap-2">
                  <Badge tone="neutral">
                    {active.width}×{active.height}
                  </Badge>
                  <Badge tone="accent">图块 {active.blocks.length}</Badge>
                  <a
                    href={api.annotatedUrl(id, active.uid)}
                    target="_blank"
                    rel="noreferrer"
                    className="inline-flex items-center gap-1 border border-line px-2 py-0.5 text-[10px] tracking-[0.1em] uppercase hover:border-cyan/50 hover:text-cyan-soft"
                  >
                    <Maximize2 size={10} /> 打开标注 PNG
                  </a>
                </div>
              </div>
            </div>

            <div className="space-y-2">
              <div className="flex items-center justify-between">
                <span className="nl-label">OCR 图块 · 源文 ↔ 译文</span>
                <div className="flex items-center gap-1.5">
                  <IconButton
                    label="放弃该贴图修改"
                    icon={<RotateCcw size={12} />}
                    onClick={() =>
                      setDrafts((previous) => {
                        const next = { ...previous }
                        for (const block of active.blocks) delete next[blockDraftKey(active.uid, block.id)]
                        return next
                      })
                    }
                  />
                  <Button
                    size="sm"
                    variant="primary"
                    icon={<Save size={11} />}
                    busy={save.pending}
                    onClick={() =>
                      void commit(dirtyEntries.filter((item) => item.uid === active.uid))
                    }
                  >
                    保存该贴图
                  </Button>
                </div>
              </div>

              <div className="scroll-thin max-h-[58vh] space-y-2 overflow-y-auto pr-1">
                {active.blocks.length === 0 && (
                  <p className="border border-dashed border-line/70 p-4 text-center text-[11px] text-ink-faint">
                    该贴图没有识别到文字图块。
                  </p>
                )}
                {active.blocks.map((block, index) => {
                  const key = blockDraftKey(active.uid, block.id)
                  const value = blockValue(active, block)
                  const dirty = value !== block.target
                  return (
                    <div
                      key={block.id}
                      className={cx(
                        'border border-line/60 bg-abyss/50 p-2',
                        dirty && 'border-cyan/50 bg-cyan/6',
                      )}
                    >
                      <div className="mb-1.5 flex items-center justify-between gap-2">
                        <div className="flex items-center gap-1.5">
                          <Badge tone="accent">#{index + 1}</Badge>
                          <StatusPill status={block.status} />
                          <span className="font-mono text-[9px] text-ink-faint">
                            置信度 {(block.confidence * 100).toFixed(0)}%
                          </span>
                        </div>
                        <span className="font-mono text-[9px] text-ink-faint/70">
                          {block.id.slice(0, 10)}
                        </span>
                      </div>
                      <div className="grid gap-1.5">
                        <div className="text-[11px] leading-snug text-ink-dim whitespace-pre-wrap">
                          {block.source || <span className="text-ink-faint/60">（空）</span>}
                        </div>
                        <TextArea
                          rows={2}
                          value={value}
                          spellCheck={false}
                          className="text-[11px]"
                          onChange={(event) =>
                            setDrafts((previous) => ({ ...previous, [key]: event.target.value }))
                          }
                        />
                      </div>
                    </div>
                  )
                })}
              </div>

              <p className="text-[10px] text-ink-faint">
                提交格式：<code className="text-cyan-dim">PATCH /api/projects/{id}/images</code> body{' '}
                <code className="text-cyan-dim">{'{"items":[{"uid","block_id","target"}]}'}</code>
              </p>
            </div>
          </div>
        )}
      </Modal>

      <ToastStack items={toasts.items} onDismiss={toasts.dismiss} />
    </div>
  )
}
