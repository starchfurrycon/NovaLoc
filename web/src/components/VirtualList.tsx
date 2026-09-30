/**
 * Minimal windowed list — avoids a virtualization dependency while keeping the
 * text review table responsive with tens of thousands of entries.
 */

import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react'
import { cx } from '../ui'

export type VirtualListProps = {
  count: number
  rowHeight: number
  renderRow: (index: number) => ReactNode
  /** Extra rows rendered above/below the viewport. */
  overscan?: number
  className?: string
  /** Called when the user scrolls near the end (used for incremental loading). */
  onEndReached?: () => void
  endThreshold?: number
  empty?: ReactNode
}

export function VirtualList({
  count,
  rowHeight,
  renderRow,
  overscan = 6,
  className,
  onEndReached,
  endThreshold = 24,
  empty,
}: VirtualListProps): ReactNode {
  const scroller = useRef<HTMLDivElement | null>(null)
  const [viewport, setViewport] = useState(0)
  const [scrollTop, setScrollTop] = useState(0)
  const endNotified = useRef(false)

  useEffect(() => {
    const node = scroller.current
    if (!node) return
    const measure = () => setViewport(node.clientHeight)
    measure()
    const observer = new ResizeObserver(measure)
    observer.observe(node)
    return () => observer.disconnect()
  }, [])

  useEffect(() => {
    endNotified.current = false
  }, [count])

  const handleScroll = useCallback(
    (event: React.UIEvent<HTMLDivElement>) => {
      const node = event.currentTarget
      setScrollTop(node.scrollTop)
      if (!onEndReached) return
      const remaining = node.scrollHeight - node.scrollTop - node.clientHeight
      if (remaining < rowHeight * endThreshold) {
        if (!endNotified.current) {
          endNotified.current = true
          onEndReached()
        }
      } else {
        endNotified.current = false
      }
    },
    [onEndReached, rowHeight, endThreshold],
  )

  if (count === 0) {
    return (
      <div className={cx('scroll-thin overflow-auto', className)} ref={scroller}>
        {empty}
      </div>
    )
  }

  const totalHeight = count * rowHeight
  const visibleCount = Math.ceil((viewport || 600) / rowHeight)
  const start = Math.max(0, Math.floor(scrollTop / rowHeight) - overscan)
  const end = Math.min(count, start + visibleCount + overscan * 2)

  const rows: ReactNode[] = []
  for (let index = start; index < end; index += 1) {
    rows.push(renderRow(index))
  }

  return (
    <div
      ref={scroller}
      onScroll={handleScroll}
      className={cx('scroll-thin relative overflow-auto', className)}
    >
      <div style={{ height: totalHeight, position: 'relative' }}>
        <div style={{ transform: `translateY(${start * rowHeight}px)` }}>{rows}</div>
      </div>
    </div>
  )
}
