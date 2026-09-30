/** Aggregated log/event stream with severity colouring and auto-scroll. */

import { useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { ArrowDownToLine, Eraser, Filter } from 'lucide-react'
import type { JobEvent } from '../api'
import { clockTime, cx, stageLabel } from '../ui'
import { Badge, Button } from './ui'
import type { Tone } from '../ui'

const SEVERITY_TONE: Record<string, Tone> = {
  debug: 'neutral',
  info: 'info',
  warning: 'warn',
  error: 'err',
}

export function LogStream({
  events,
  height = 360,
  onClear,
  emptyLabel = '等待事件…',
}: {
  events: JobEvent[]
  height?: number
  onClear?: () => void
  emptyLabel?: string
}): ReactNode {
  const scroller = useRef<HTMLDivElement | null>(null)
  const [follow, setFollow] = useState(true)
  const [level, setLevel] = useState<'all' | 'warning' | 'error'>('all')

  const visible = useMemo(() => {
    if (level === 'all') return events
    if (level === 'warning') {
      return events.filter((event) => event.severity === 'warning' || event.severity === 'error')
    }
    return events.filter((event) => event.severity === 'error')
  }, [events, level])

  useEffect(() => {
    if (!follow) return
    const node = scroller.current
    if (!node) return
    node.scrollTop = node.scrollHeight
  }, [visible, follow])

  return (
    <div className="relative">
      <div className="mb-2 flex items-center justify-between gap-2">
        <div className="flex items-center gap-1.5">
          <Filter size={11} className="text-ink-faint" />
          {(['all', 'warning', 'error'] as const).map((option) => (
            <button
              key={option}
              type="button"
              onClick={() => setLevel(option)}
              className={cx(
                'border px-1.5 py-[1px] text-[10px] tracking-[0.1em] uppercase transition-colors',
                level === option
                  ? 'border-cyan/50 bg-cyan/10 text-cyan-soft'
                  : 'border-line text-ink-faint hover:text-ink-dim',
              )}
            >
              {option === 'all' ? '全部' : option === 'warning' ? '警告+' : '仅错误'}
            </button>
          ))}
          <span className="ml-1 text-[10px] text-ink-faint">{visible.length} 条</span>
        </div>
        <div className="flex items-center gap-1.5">
          <Button
            size="sm"
            variant={follow ? 'primary' : 'ghost'}
            icon={<ArrowDownToLine size={11} />}
            onClick={() => setFollow((value) => !value)}
          >
            {follow ? '跟随' : '暂停跟随'}
          </Button>
          {onClear && (
            <Button size="sm" variant="subtle" icon={<Eraser size={11} />} onClick={onClear}>
              清空
            </Button>
          )}
        </div>
      </div>

      <div
        ref={scroller}
        style={{ height }}
        className="scroll-thin overflow-y-auto border border-line/70 bg-void/80 p-2 font-mono text-[11px] leading-[1.7]"
      >
        {visible.length === 0 ? (
          <div className="nl-pulse px-1 py-4 text-center text-[10px] tracking-[0.18em] text-ink-faint uppercase">
            {emptyLabel}
          </div>
        ) : (
          visible.map((event, index) => (
            <div
              key={`${event.ts}-${index}`}
              className={cx(
                'flex gap-2 px-1 py-[1px] whitespace-pre-wrap',
                event.severity === 'error' && 'text-err',
                event.severity === 'warning' && 'text-warn',
                event.severity !== 'error' && event.severity !== 'warning' && 'text-ink-dim',
              )}
            >
              <span className="shrink-0 text-ink-faint/70">{clockTime(event.ts)}</span>
              <span className="w-16 shrink-0 truncate text-cyan-dim/80">
                {stageLabel(event.stage)}
              </span>
              <span className="w-12 shrink-0">
                <Badge tone={SEVERITY_TONE[event.severity] ?? 'neutral'}>{event.kind}</Badge>
              </span>
              <span className="min-w-0 flex-1 break-words">{event.message}</span>
            </div>
          ))
        )}
      </div>
    </div>
  )
}
