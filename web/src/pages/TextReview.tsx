/** 文本审校 — filterable, virtualized source ↔ target table with inline editing. */

import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { Link, useParams } from 'react-router-dom'
import {
  AlertTriangle,
  CheckCheck,
  Eraser,
  FileText,
  Filter,
  Image as ImageIcon,
  RotateCcw,
  Save,
  Search,
  ShieldAlert,
  Type,
} from 'lucide-react'
import { api } from '../api'
import type { Project, TextEntry, TextPatchItem, TextPage } from '../api'
import { errorMessage, useAction, useResource, useToasts } from '../hooks'
import { cx, kindLabel, statusTone, truncate } from '../ui'
import { PageHeader } from '../components/PageHeader'
import { VirtualList } from '../components/VirtualList'
import {
  Badge,
  Button,
  EmptyState,
  ErrorState,
  Input,
  LoadingState,
  Panel,
  Select,
  StatusPill,
  TextArea,
  ToastStack,
} from '../components/ui'

const ROW_HEIGHT = 78

type StatusFilter = 'all' | 'pending' | 'translated' | 'reviewed' | 'failed'

const STATUS_FILTERS: Array<{ value: StatusFilter; label: string }> = [
  { value: 'all', label: '全部状态' },
  { value: 'pending', label: '待处理' },
  { value: 'translated', label: '已翻译' },
  { value: 'reviewed', label: '已审校' },
  { value: 'failed', label: '失败' },
]

export default function TextReviewPage(): ReactNode {
  const { id = '' } = useParams()
  const toasts = useToasts()

  const [status, setStatus] = useState<StatusFilter>('all')
  const [kind, setKind] = useState<string>('all')
  const [query, setQuery] = useState('')
  const [onlyWarnings, setOnlyWarnings] = useState(false)
  const [limit, setLimit] = useState(300)

  const [drafts, setDrafts] = useState<Record<string, string>>({})
  const [focused, setFocused] = useState<string | null>(null)

  const project = useResource<Project>((signal) => api.getProject(id, signal), [id])
  const text = useResource<TextPage>(
    (signal) => api.getText(id, { limit }, signal),
    [id, limit],
  )
  const save = useAction(api.patchText)

  const entries = useMemo(() => text.data?.entries ?? [], [text.data])

  const kinds = useMemo(() => {
    const set = new Set<string>()
    for (const entry of entries) set.add(entry.kind)
    return [...set].sort()
  }, [entries])

  const filtered = useMemo(() => {
    const needle = query.trim().toLowerCase()
    return entries.filter((entry) => {
      if (status !== 'all' && entry.status !== status) return false
      if (kind !== 'all' && entry.kind !== kind) return false
      if (onlyWarnings && entry.warnings.length === 0) return false
      if (!needle) return true
      return (
        entry.source.toLowerCase().includes(needle) ||
        entry.target.toLowerCase().includes(needle) ||
        entry.uid.toLowerCase().includes(needle) ||
        (entry.speaker ?? '').toLowerCase().includes(needle)
      )
    })
  }, [entries, status, kind, onlyWarnings, query])

  const visible = useMemo(() => filtered.slice(0, limit), [filtered, limit])

  const dirtyUids = useMemo(() => {
    const list: string[] = []
    for (const [uid, value] of Object.entries(drafts)) {
      const entry = entries.find((item) => item.uid === uid)
      if (!entry) continue
      if (entry.target !== value) list.push(uid)
    }
    return list
  }, [drafts, entries])

  const warningCount = useMemo(
    () => entries.filter((entry) => entry.warnings.length > 0).length,
    [entries],
  )

  const unsaved = dirtyUids.length > 0

  useEffect(() => {
    if (!unsaved) return
    const handler = (event: BeforeUnloadEvent) => {
      event.preventDefault()
    }
    window.addEventListener('beforeunload', handler)
    return () => window.removeEventListener('beforeunload', handler)
  }, [unsaved])

  const targetValue = useCallback(
    (entry: TextEntry) => drafts[entry.uid] ?? entry.target,
    [drafts],
  )

  const setDraft = useCallback((uid: string, value: string) => {
    setDrafts((previous) => ({ ...previous, [uid]: value }))
  }, [])

  const revert = useCallback((uid: string) => {
    setDrafts((previous) => {
      const next = { ...previous }
      delete next[uid]
      return next
    })
  }, [])

  const commit = useCallback(
    async (items: TextPatchItem[], label: string) => {
      if (items.length === 0) return
      const result = await save.run(id, items)
      if (!result) {
        toasts.push('err', `保存失败：${errorMessage(save.error)}`)
        return
      }
      setDrafts((previous) => {
        const next = { ...previous }
        for (const item of items) delete next[item.uid]
        return next
      })
      toasts.push('ok', `${label}：已更新 ${result.updated} 条。`)
      await text.reload({ quiet: true })
    },
    [id, save, toasts, text],
  )

  const saveAll = useCallback(() => {
    const items: TextPatchItem[] = dirtyUids.map((uid) => ({ uid, target: drafts[uid] ?? '' }))
    void commit(items, '批量保存')
  }, [dirtyUids, drafts, commit])

  const saveOne = useCallback(
    (entry: TextEntry, extra?: { status?: string }) => {
      const target = targetValue(entry)
      const item: TextPatchItem = { uid: entry.uid, target }
      if (extra?.status) item.status = extra.status
      void commit([item], '已保存')
    },
    [targetValue, commit],
  )

  // Scroll back to the first row whenever the filter set changes.
  const listTop = useRef<HTMLDivElement | null>(null)
  useEffect(() => {
    listTop.current?.scrollIntoView({ block: 'start' })
  }, [status, kind, query, onlyWarnings])

  // Ctrl/Cmd + Enter commits every dirty row.
  useEffect(() => {
    const handler = (event: KeyboardEvent) => {
      if ((event.ctrlKey || event.metaKey) && event.key === 'Enter') {
        event.preventDefault()
        const items: TextPatchItem[] = []
        for (const [uid, value] of Object.entries(drafts)) {
          const entry = entries.find((item) => item.uid === uid)
          if (entry && entry.target !== value) items.push({ uid, target: value })
        }
        if (items.length > 0) void commit(items, '批量保存')
      }
    }
    window.addEventListener('keydown', handler)
    return () => window.removeEventListener('keydown', handler)
  }, [drafts, entries, commit])

  return (
    <div className="space-y-4">
      <PageHeader
        eyebrow={`TEXT / ${id.slice(0, 8)}`}
        title="文本审校"
        description="逐条核对源文与译文。修改后按 Ctrl+Enter 保存全部，或在单行按 Enter 保存该行。占位符或格式异常的条目标红，务必优先处理。"
        meta={
          <>
            <Badge tone="accent">
              <FileText size={10} />
              共 {text.data?.total ?? entries.length} 条
            </Badge>
            <Badge tone={warningCount > 0 ? 'warn' : 'ok'}>
              <ShieldAlert size={10} />
              警告 {warningCount}
            </Badge>
            {unsaved && (
              <Badge tone="info">
                <Save size={10} />
                未保存 {dirtyUids.length}
              </Badge>
            )}
            <span className="text-[10px] text-ink-faint">
              当前筛选 {filtered.length} 条 · 已渲染 {visible.length}
            </span>
          </>
        }
        actions={
          <>
            <Button
              variant={unsaved ? 'primary' : 'ghost'}
              icon={<Save size={12} />}
              disabled={!unsaved}
              busy={save.pending}
              onClick={saveAll}
            >
              保存全部{unsaved ? ` (${dirtyUids.length})` : ''}
            </Button>
            <Button
              icon={<RotateCcw size={12} />}
              disabled={!unsaved}
              onClick={() => setDrafts({})}
            >
              放弃修改
            </Button>
            <Button icon={<RotateCcw size={12} />} onClick={() => void text.reload()} busy={text.loading}>
              刷新
            </Button>
          </>
        }
      />

      <Panel pad={false} bodyClassName="p-3">
        <div className="flex flex-wrap items-end gap-2">
          <div className="flex min-w-[220px] flex-1 items-center gap-2 border border-line bg-abyss/80 px-2">
            <Search size={12} className="shrink-0 text-ink-faint" />
            <input
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder="搜索源文 / 译文 / UID / 说话人"
              className="w-full bg-transparent py-1.5 font-mono text-[12px] text-ink placeholder:text-ink-faint/70 focus:outline-none"
            />
            {query && (
              <button
                type="button"
                onClick={() => setQuery('')}
                className="shrink-0 text-[10px] text-ink-faint uppercase hover:text-cyan-soft"
              >
                清除
              </button>
            )}
          </div>

          <div className="w-36">
            <Select value={status} onChange={(event) => setStatus(event.target.value as StatusFilter)}>
              {STATUS_FILTERS.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </Select>
          </div>

          <div className="w-36">
            <Select value={kind} onChange={(event) => setKind(event.target.value)}>
              <option value="all">全部类型</option>
              {kinds.map((value) => (
                <option key={value} value={value}>
                  {kindLabel(value)}
                </option>
              ))}
            </Select>
          </div>

          <Button
            icon={<Filter size={12} />}
            variant={onlyWarnings ? 'primary' : 'ghost'}
            onClick={() => setOnlyWarnings((value) => !value)}
          >
            仅看警告
          </Button>

          <div className="ml-auto flex items-center gap-2">
            <span className="nl-label">渲染上限</span>
            <div className="w-24">
              <Input
                type="number"
                min={50}
                step={50}
                value={limit}
                onChange={(event) => {
                  const next = Number.parseInt(event.target.value, 10)
                  if (Number.isFinite(next) && next >= 50) setLimit(next)
                }}
              />
            </div>
          </div>
        </div>

        <p className="mt-2 text-[10px] leading-relaxed text-ink-faint">
          客户端按 <code className="text-cyan-dim">limit={limit}</code> 分批渲染以避免大表格卡顿；筛选条件同时会作为查询参数发给
          <code className="mx-1 text-cyan-dim">GET /api/projects/{'{id}'}/text</code>
          ，后端可忽略未知参数。
        </p>
      </Panel>

      {text.error && <ErrorState error={text.error} onRetry={() => void text.reload()} />}
      {save.error && <ErrorState error={save.error} />}
      {text.loading && !text.data && <LoadingState label="读取文本条目" />}

      {text.data && filtered.length === 0 && (
        <EmptyState
          icon={<FileText size={20} />}
          title="没有符合条件的条目"
          description="试试清空搜索条件，或先在「扫描」页运行文本提取。"
        />
      )}

      {visible.length > 0 && (
        <div ref={listTop} className="scroll-mt-4">
        <Panel
          pad={false}
          title="源文 ↔ 译文"
          subtitle={`${project.data?.name ?? id} · ${visible.length} / ${filtered.length}`}
          icon={<Type size={13} />}
          className="scroll-mt-4"
          actions={
            <div className="hidden items-center gap-2 text-[10px] text-ink-faint md:flex">
              <span>
                <kbd className="border border-line px-1">Ctrl</kbd>+
                <kbd className="border border-line px-1">Enter</kbd> 保存全部
              </span>
              <span>
                <kbd className="border border-line px-1">Enter</kbd> 保存该行
              </span>
            </div>
          }
        >
          <div className="grid grid-cols-[110px_minmax(0,1fr)_minmax(0,1fr)_110px] gap-2 border-b border-line/70 px-3 py-1.5">
            <span className="nl-label">条目</span>
            <span className="nl-label">源文 SOURCE</span>
            <span className="nl-label">译文 TARGET</span>
            <span className="nl-label text-right">操作</span>
          </div>

          <VirtualList
            count={visible.length}
            rowHeight={ROW_HEIGHT}
            className="h-[calc(100vh-380px)] min-h-[380px]"
            onEndReached={() => {
              if (visible.length < filtered.length) setLimit((value) => value + 300)
            }}
            empty={<div className="p-6 text-center text-[11px] text-ink-faint">无条目</div>}
            renderRow={(index) => {
              const entry = visible[index]
              if (!entry) return null
              const value = targetValue(entry)
              const dirty = value !== entry.target
              const tone = statusTone(entry.status)
              return (
                <div
                  key={`${entry.uid}-${index}`}
                  style={{ height: ROW_HEIGHT }}
                  className={cx(
                    'grid grid-cols-[110px_minmax(0,1fr)_minmax(0,1fr)_110px] items-start gap-2 border-b border-line/30 px-3 py-2',
                    dirty && 'bg-cyan/6',
                    focused === entry.uid && 'bg-cyan/10',
                  )}
                  onMouseDown={() => setFocused(entry.uid)}
                >
                  <div className="min-w-0 space-y-1">
                    <div className="truncate font-mono text-[10px] text-ink-faint" title={entry.uid}>
                      {entry.uid.slice(0, 12)}
                    </div>
                    <StatusPill status={entry.status} />
                    <div className="flex flex-wrap items-center gap-1">
                      <Badge tone="neutral">{kindLabel(entry.kind)}</Badge>
                      {entry.speaker && (
                        <span className="truncate text-[9px] text-ink-faint" title={entry.speaker}>
                          {truncate(entry.speaker, 10)}
                        </span>
                      )}
                    </div>
                  </div>

                  <div className="scroll-thin h-[58px] overflow-auto pr-1 text-[11px] leading-[1.5] text-ink-dim whitespace-pre-wrap">
                    {entry.source || <span className="text-ink-faint/60">（空）</span>}
                  </div>

                  <div className="space-y-1">
                    <TextArea
                      value={value}
                      spellCheck={false}
                      rows={2}
                      className="h-[42px] min-h-[42px] py-1 text-[11px]"
                      onChange={(event) => setDraft(entry.uid, event.target.value)}
                      onKeyDown={(event) => {
                        if (event.key === 'Enter' && !event.shiftKey) {
                          event.preventDefault()
                          saveOne(entry)
                        }
                        if (event.key === 'Escape') {
                          event.preventDefault()
                          revert(entry.uid)
                          event.currentTarget.blur()
                        }
                      }}
                    />
                    {entry.warnings.length > 0 && (
                      <div className="flex flex-wrap items-start gap-1">
                        <Badge tone="warn">
                          <AlertTriangle size={10} />
                          警告 {entry.warnings.length}
                        </Badge>
                        <span className="min-w-0 flex-1 truncate text-[10px] text-warn/90" title={entry.warnings.join('\n')}>
                          {entry.warnings.join('；')}
                        </span>
                      </div>
                    )}
                  </div>

                  <div className="flex flex-col items-end gap-1">
                    <div className="flex items-center gap-1">
                      <Button
                        size="sm"
                        variant={dirty ? 'primary' : 'ghost'}
                        icon={<Save size={11} />}
                        disabled={!dirty}
                        busy={save.pending}
                        onClick={() => saveOne(entry)}
                      >
                        保存
                      </Button>
                      <Button
                        size="sm"
                        variant="subtle"
                        icon={<Eraser size={11} />}
                        disabled={!dirty}
                        onClick={() => revert(entry.uid)}
                      >
                        撤销
                      </Button>
                    </div>
                    <Button
                      size="sm"
                      variant="ghost"
                      icon={<CheckCheck size={11} />}
                      disabled={tone === 'ok' && !dirty}
                      onClick={() => saveOne(entry, { status: 'reviewed' })}
                    >
                      标记审校
                    </Button>
                  </div>
                </div>
              )
            }}
          />

          <div className="flex flex-wrap items-center justify-between gap-2 border-t border-line/60 px-3 py-2 text-[10px] text-ink-faint">
            <span>
              显示 {visible.length} / {filtered.length} 条（后端共 {text.data?.total ?? 0} 条）
            </span>
            <div className="flex items-center gap-2">
              <Link
                to={`/images/${id}`}
                className="inline-flex items-center gap-1 border border-line px-2 py-0.5 uppercase hover:border-cyan/50 hover:text-cyan-soft"
              >
                <ImageIcon size={10} /> 贴图审校
              </Link>
              <Link
                to={`/qa/${id}`}
                className="inline-flex items-center gap-1 border border-line px-2 py-0.5 uppercase hover:border-cyan/50 hover:text-cyan-soft"
              >
                <ShieldAlert size={10} /> 质检报告
              </Link>
              {visible.length < filtered.length && (
                <Button size="sm" onClick={() => setLimit((value) => value + 300)}>
                  加载更多
                </Button>
              )}
            </div>
          </div>
        </Panel>
        </div>
      )}

      <ToastStack items={toasts.items} onDismiss={toasts.dismiss} />
    </div>
  )
}

/** Re-exported for readability in tooling. */
export type { TextEntry }
