/** Small page-level header used by every route. */

import type { ReactNode } from 'react'
import { EffText } from './ui'
import { cx } from '../ui'

export function PageHeader({
  title,
  eyebrow,
  description,
  actions,
  meta,
  className,
}: {
  title: ReactNode
  eyebrow?: ReactNode
  description?: ReactNode
  actions?: ReactNode
  meta?: ReactNode
  className?: string
}): ReactNode {
  return (
    <div className={cx('mb-4 flex flex-wrap items-end justify-between gap-3', className)}>
      <div className="min-w-0">
        {eyebrow && <div className="nl-label mb-1">{eyebrow}</div>}
        <h1 className="text-[17px] leading-tight font-medium tracking-[0.16em] text-cyan-soft uppercase">
          <EffText>{title}</EffText>
        </h1>
        {description && (
          <p className="mt-1.5 max-w-3xl text-[11px] leading-relaxed text-ink-faint">{description}</p>
        )}
        {meta && <div className="mt-2 flex flex-wrap items-center gap-2">{meta}</div>}
      </div>
      {actions && <div className="flex flex-wrap items-center gap-2">{actions}</div>}
    </div>
  )
}
