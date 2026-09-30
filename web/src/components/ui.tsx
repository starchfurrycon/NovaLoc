/**
 * NovaLoc / 新译 — shared UI kit.
 *
 * Everything here is built from Arwes (`@arwes/react`) frames / text effects,
 * Tailwind utility classes and lucide-react icons. No custom textures or
 * hand-drawn SVG art.
 */

import {
  forwardRef,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ButtonHTMLAttributes,
  type InputHTMLAttributes,
  type ReactNode,
  type SelectHTMLAttributes,
  type TextareaHTMLAttributes,
} from 'react'
import {
  Animator,
  FrameCorners,
  FrameLines,
  FrameNero,
  FrameOctagon,
  FrameUnderline,
  Text,
} from '@arwes/react'
import { AlertTriangle, Check, Info, Loader2, X, XCircle } from 'lucide-react'
import { TONE_BG, TONE_BORDER, TONE_TEXT, clamp01, cx, statusLabel, statusTone } from '../ui'
import type { Tone } from '../ui'

/* ------------------------------------------------------------------ *
 * Arwes text effects
 * ------------------------------------------------------------------ */

/** Animated text whose entrance restarts whenever `animKey` changes. */
export function EffText({
  children,
  className,
  as = 'span',
  fixed = false,
  blink = false,
}: {
  children: ReactNode
  className?: string
  as?: keyof HTMLElementTagNameMap
  fixed?: boolean
  blink?: boolean
}): ReactNode {
  const animKey = useMemo(() => `${String(children)}`, [children])
  return (
    <Animator key={animKey}>
      <Text
        as={as}
        className={cx('nl-text', className)}
        fixed={fixed}
        blink={blink}
        contentStyle={{ display: 'inline' }}
      >
        {children}
      </Text>
    </Animator>
  )
}

/* ------------------------------------------------------------------ *
 * Panel
 * ------------------------------------------------------------------ */

export function Panel({
  title,
  subtitle,
  icon,
  actions,
  children,
  className,
  bodyClassName,
  pad = true,
  frame = 'corners',
}: {
  title?: ReactNode
  subtitle?: ReactNode
  icon?: ReactNode
  actions?: ReactNode
  children?: ReactNode
  className?: string
  bodyClassName?: string
  pad?: boolean
  frame?: 'corners' | 'octagon' | 'nero' | 'none'
}): ReactNode {
  return (
    <section
      className={cx(
        'relative isolate bg-panel/85 shadow-[0_0_0_1px_rgba(0,0,0,0.4),0_18px_40px_-30px_rgba(46,230,214,0.5)]',
        className,
      )}
    >
      {frame === 'corners' && (
        <FrameCorners strokeWidth={1} cornerLength={12} className="pointer-events-none absolute inset-0" />
      )}
      {frame === 'octagon' && (
        <FrameOctagon strokeWidth={1} squareSize={11} className="pointer-events-none absolute inset-0" />
      )}
      {frame === 'nero' && (
        <FrameNero
          cornerLength={10}
          cornerWidth={1.2}
          className="pointer-events-none absolute inset-0"
        />
      )}

      {(title || actions) && (
        <header className="relative flex items-start justify-between gap-4 px-4 pt-3 pb-2">
          <div className="flex min-w-0 items-center gap-2">
            {icon && <span className="shrink-0 text-cyan/80">{icon}</span>}
            <div className="min-w-0">
              {title && (
                <h2 className="truncate text-[12px] font-medium tracking-[0.16em] text-cyan-soft uppercase">
                  {title}
                </h2>
              )}
              {subtitle && <p className="mt-0.5 truncate text-[11px] text-ink-faint">{subtitle}</p>}
            </div>
          </div>
          {actions && <div className="flex shrink-0 items-center gap-2">{actions}</div>}
        </header>
      )}

      <div className="relative px-4 pb-1">
        <FrameLines largeLineWidth={1} smallLineWidth={0.6} smallLineLength={6} />
      </div>

      <div className={cx('relative', pad && 'p-4', bodyClassName)}>{children}</div>
    </section>
  )
}

/* ------------------------------------------------------------------ *
 * Button
 * ------------------------------------------------------------------ */

type ButtonProps = ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: 'primary' | 'ghost' | 'danger' | 'subtle'
  size?: 'sm' | 'md'
  icon?: ReactNode
  busy?: boolean
}

const BUTTON_VARIANTS: Record<NonNullable<ButtonProps['variant']>, string> = {
  primary: 'border-cyan/60 bg-cyan/10 text-cyan-soft hover:bg-cyan/20 hover:border-cyan',
  ghost: 'border-line text-ink-dim hover:border-cyan/60 hover:text-cyan-soft hover:bg-cyan/6',
  danger: 'border-err/50 bg-err/8 text-err hover:bg-err/16 hover:border-err',
  subtle: 'border-transparent bg-line/20 text-ink-dim hover:text-ink hover:bg-line/35',
}

export const Button = forwardRef<HTMLButtonElement, ButtonProps>(function Button(
  { variant = 'ghost', size = 'md', icon, busy = false, className, children, disabled, ...rest },
  ref,
) {
  return (
    <button
      {...rest}
      ref={ref}
      disabled={disabled || busy}
      className={cx(
        'group relative inline-flex items-center justify-center gap-1.5 border font-mono tracking-[0.08em] uppercase transition-colors duration-150',
        size === 'sm' ? 'px-2 py-1 text-[10px]' : 'px-3 py-1.5 text-[11px]',
        BUTTON_VARIANTS[variant],
        (disabled || busy) && 'cursor-not-allowed opacity-45 hover:bg-transparent',
        className,
      )}
    >
      {busy ? (
        <Loader2 size={size === 'sm' ? 11 : 13} className="animate-spin" />
      ) : (
        icon && <span className="shrink-0 opacity-90">{icon}</span>
      )}
      <span className="truncate">{children}</span>
    </button>
  )
})

/** Icon-only square button. */
export function IconButton({
  label,
  icon,
  variant = 'ghost',
  className,
  ...rest
}: ButtonHTMLAttributes<HTMLButtonElement> & {
  label: string
  icon: ReactNode
  variant?: 'ghost' | 'danger'
}): ReactNode {
  return (
    <button
      {...rest}
      title={label}
      aria-label={label}
      className={cx(
        'inline-flex h-7 w-7 items-center justify-center border transition-colors duration-150',
        variant === 'danger'
          ? 'border-line text-ink-faint hover:border-err/60 hover:text-err'
          : 'border-line text-ink-faint hover:border-cyan/60 hover:text-cyan-soft',
        rest.disabled && 'cursor-not-allowed opacity-40',
        className,
      )}
    >
      {icon}
    </button>
  )
}

/* ------------------------------------------------------------------ *
 * Form fields
 * ------------------------------------------------------------------ */

const FIELD_CLASS =
  'w-full border border-line bg-abyss/80 px-2.5 py-1.5 font-mono text-[12px] text-ink placeholder:text-ink-faint/70 transition-colors focus:border-cyan/70 focus:outline-none'

export function Field({
  label,
  hint,
  error,
  required,
  children,
  className,
}: {
  label?: ReactNode
  hint?: ReactNode
  error?: ReactNode
  required?: boolean
  children: ReactNode
  className?: string
}): ReactNode {
  return (
    <label className={cx('block', className)}>
      {label && (
        <span className="nl-label mb-1 flex items-center gap-1">
          {label}
          {required && <span className="text-err">*</span>}
        </span>
      )}
      {children}
      {hint && !error && <span className="mt-1 block text-[10px] text-ink-faint">{hint}</span>}
      {error && <span className="mt-1 block text-[10px] text-err">{error}</span>}
    </label>
  )
}

export const Input = forwardRef<HTMLInputElement, InputHTMLAttributes<HTMLInputElement>>(
  function Input({ className, ...rest }, ref) {
    return <input {...rest} ref={ref} className={cx(FIELD_CLASS, className)} />
  },
)

export const TextArea = forwardRef<HTMLTextAreaElement, TextareaHTMLAttributes<HTMLTextAreaElement>>(
  function TextArea({ className, ...rest }, ref) {
    return (
      <textarea
        {...rest}
        ref={ref}
        className={cx(FIELD_CLASS, 'scroll-thin resize-y leading-relaxed', className)}
      />
    )
  },
)

export const Select = forwardRef<HTMLSelectElement, SelectHTMLAttributes<HTMLSelectElement>>(
  function Select({ className, children, ...rest }, ref) {
    return (
      <select {...rest} ref={ref} className={cx(FIELD_CLASS, 'cursor-pointer', className)}>
        {children}
      </select>
    )
  },
)

export function Toggle({
  checked,
  onChange,
  label,
  hint,
  disabled,
}: {
  checked: boolean
  onChange: (next: boolean) => void
  label: ReactNode
  hint?: ReactNode
  disabled?: boolean
}): ReactNode {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      disabled={disabled}
      onClick={() => onChange(!checked)}
      className={cx(
        'flex w-full items-center justify-between gap-4 border px-3 py-2 text-left transition-colors',
        checked ? 'border-cyan/50 bg-cyan/8' : 'border-line bg-abyss/60 hover:border-line-soft',
        disabled && 'cursor-not-allowed opacity-50',
      )}
    >
      <span className="min-w-0">
        <span className="block text-[11px] tracking-[0.08em] text-ink uppercase">{label}</span>
        {hint && <span className="mt-0.5 block text-[10px] text-ink-faint">{hint}</span>}
      </span>
      <span
        className={cx(
          'relative h-4 w-9 shrink-0 border transition-colors',
          checked ? 'border-cyan/70 bg-cyan/25' : 'border-line bg-line/20',
        )}
      >
        <span
          className={cx(
            'absolute top-0.5 h-2.5 w-2.5 transition-all',
            checked ? 'left-[20px] bg-cyan' : 'left-0.5 bg-ink-faint',
          )}
        />
      </span>
    </button>
  )
}

/* ------------------------------------------------------------------ *
 * Badges / pills
 * ------------------------------------------------------------------ */

export function Badge({
  children,
  tone = 'neutral',
  className,
}: {
  children: ReactNode
  tone?: Tone
  className?: string
}): ReactNode {
  return (
    <span
      className={cx(
        'inline-flex items-center gap-1 border px-1.5 py-[1px] font-mono text-[10px] tracking-[0.1em] uppercase',
        TONE_BORDER[tone],
        TONE_BG[tone],
        TONE_TEXT[tone],
        className,
      )}
    >
      {children}
    </span>
  )
}

export function StatusPill({ status, className }: { status: string; className?: string }): ReactNode {
  const tone = statusTone(status)
  return (
    <Badge tone={tone} className={className}>
      <span className={cx('h-1.5 w-1.5', tone === 'neutral' ? 'bg-ink-faint' : 'bg-current')} />
      {statusLabel(status)}
    </Badge>
  )
}

/* ------------------------------------------------------------------ *
 * Progress
 * ------------------------------------------------------------------ */

export function ProgressBar({
  value,
  tone = 'accent',
  height = 4,
  showValue = false,
  className,
}: {
  value: number
  tone?: Tone
  height?: number
  showValue?: boolean
  className?: string
}): ReactNode {
  const ratio = clamp01(value)
  return (
    <div className={cx('flex items-center gap-2', className)}>
      <div
        className="relative flex-1 overflow-hidden bg-line/30"
        style={{ height }}
        role="progressbar"
        aria-valuenow={Math.round(ratio * 100)}
        aria-valuemin={0}
        aria-valuemax={100}
      >
        <div
          className={cx(
            'h-full transition-[width] duration-500 ease-out',
            tone === 'ok' && 'bg-ok',
            tone === 'warn' && 'bg-warn',
            tone === 'err' && 'bg-err',
            tone === 'info' && 'bg-info',
            (tone === 'accent' || tone === 'neutral') && 'bg-cyan',
          )}
          style={{ width: `${ratio * 100}%` }}
        />
      </div>
      {showValue && (
        <span className="w-10 shrink-0 text-right font-mono text-[10px] text-ink-faint">
          {Math.round(ratio * 100)}%
        </span>
      )}
    </div>
  )
}

/** Small circular meter used for coverage ratios. */
export function Meter({ value, label }: { value: number; label?: string }): ReactNode {
  const ratio = clamp01(value)
  const tone: Tone = ratio >= 0.995 ? 'ok' : ratio >= 0.9 ? 'warn' : 'err'
  const size = 44
  const radius = (size - 5) / 2
  const circumference = 2 * Math.PI * radius
  return (
    <div className="flex items-center gap-2.5">
      <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`} className="shrink-0 -rotate-90">
        <circle
          cx={size / 2}
          cy={size / 2}
          r={radius}
          fill="none"
          stroke="currentColor"
          strokeWidth={2}
          className="text-line/50"
        />
        <circle
          cx={size / 2}
          cy={size / 2}
          r={radius}
          fill="none"
          stroke="currentColor"
          strokeWidth={2}
          strokeDasharray={circumference}
          strokeDashoffset={circumference * (1 - ratio)}
          className={TONE_TEXT[tone]}
        />
      </svg>
      <div className="min-w-0">
        <div className={cx('font-mono text-[13px]', TONE_TEXT[tone])}>
          {(ratio * 100).toFixed(1)}%
        </div>
        {label && <div className="text-[10px] text-ink-faint">{label}</div>}
      </div>
    </div>
  )
}

/* ------------------------------------------------------------------ *
 * States
 * ------------------------------------------------------------------ */

export function EmptyState({
  title,
  description,
  icon,
  action,
  className,
}: {
  title: ReactNode
  description?: ReactNode
  icon?: ReactNode
  action?: ReactNode
  className?: string
}): ReactNode {
  return (
    <div
      className={cx(
        'flex flex-col items-center justify-center gap-2 border border-dashed border-line/70 px-6 py-10 text-center',
        className,
      )}
    >
      {icon && <span className="text-ink-faint/70">{icon}</span>}
      <p className="text-[12px] tracking-[0.1em] text-ink-dim uppercase">{title}</p>
      {description && <p className="max-w-md text-[11px] leading-relaxed text-ink-faint">{description}</p>}
      {action && <div className="mt-1">{action}</div>}
    </div>
  )
}

export function ErrorState({
  error,
  onRetry,
  className,
}: {
  error: unknown
  onRetry?: () => void
  className?: string
}): ReactNode {
  const message = error instanceof Error ? error.message : String(error ?? '未知错误')
  return (
    <div
      className={cx(
        'flex items-start gap-3 border border-err/40 bg-err/8 px-3 py-2.5 text-[11px] text-err',
        className,
      )}
    >
      <XCircle size={14} className="mt-0.5 shrink-0" />
      <div className="min-w-0 flex-1">
        <p className="leading-relaxed break-words">{message}</p>
        {onRetry && (
          <button
            type="button"
            onClick={onRetry}
            className="mt-1.5 border border-err/50 px-2 py-0.5 text-[10px] tracking-[0.1em] uppercase hover:bg-err/15"
          >
            重试
          </button>
        )}
      </div>
    </div>
  )
}

export function LoadingState({ label = '读取中' }: { label?: string }): ReactNode {
  return (
    <div className="flex items-center justify-center gap-2 py-10 text-[11px] tracking-[0.16em] text-ink-faint uppercase">
      <Loader2 size={13} className="animate-spin text-cyan/70" />
      {label}
      <span className="nl-pulse">_</span>
    </div>
  )
}

/* ------------------------------------------------------------------ *
 * Toast
 * ------------------------------------------------------------------ */

export type ToastTone = 'ok' | 'err' | 'info'
type ToastItem = { id: number; tone: ToastTone; message: string }

const TOAST_ICON: Record<ToastTone, ReactNode> = {
  ok: <Check size={13} />,
  err: <AlertTriangle size={13} />,
  info: <Info size={13} />,
}

export function ToastStack({
  items,
  onDismiss,
}: {
  items: ToastItem[]
  onDismiss: (id: number) => void
}): ReactNode {
  return (
    <div className="pointer-events-none fixed right-4 bottom-4 z-50 flex w-80 flex-col gap-2">
      {items.map((item) => (
        <button
          key={item.id}
          type="button"
          onClick={() => onDismiss(item.id)}
          className={cx(
            'pointer-events-auto flex items-start gap-2 border bg-abyss/95 px-3 py-2 text-left text-[11px] backdrop-blur',
            item.tone === 'ok' && 'border-ok/45 text-ok',
            item.tone === 'err' && 'border-err/50 text-err',
            item.tone === 'info' && 'border-cyan/45 text-cyan-soft',
          )}
        >
          <span className="mt-0.5 shrink-0">{TOAST_ICON[item.tone]}</span>
          <span className="min-w-0 flex-1 break-words">{item.message}</span>
          <X size={11} className="mt-0.5 shrink-0 opacity-60" />
        </button>
      ))}
    </div>
  )
}

export type { ToastItem }

/* ------------------------------------------------------------------ *
 * Tabs
 * ------------------------------------------------------------------ */

export function Tabs<T extends string>({
  value,
  onChange,
  items,
  className,
}: {
  value: T
  onChange: (next: T) => void
  items: Array<{ value: T; label: ReactNode; count?: number }>
  className?: string
}): ReactNode {
  return (
    <div className={cx('relative flex flex-wrap items-center gap-1', className)}>
      {items.map((item) => {
        const active = item.value === value
        return (
          <button
            key={item.value}
            type="button"
            onClick={() => onChange(item.value)}
            className={cx(
              'relative border px-3 py-1 text-[11px] tracking-[0.12em] uppercase transition-colors',
              active
                ? 'border-cyan/60 bg-cyan/12 text-cyan-soft'
                : 'border-line text-ink-faint hover:border-line-soft hover:text-ink-dim',
            )}
          >
            {item.label}
            {item.count !== undefined && (
              <span className="ml-1.5 text-[10px] text-ink-faint">{item.count}</span>
            )}
            {active && (
              <span className="pointer-events-none absolute inset-x-0 -bottom-[1px] h-[1px] bg-cyan" />
            )}
          </button>
        )
      })}
    </div>
  )
}

/* ------------------------------------------------------------------ *
 * Key/value readout
 * ------------------------------------------------------------------ */

export function KV({
  label,
  children,
  mono = true,
  className,
}: {
  label: ReactNode
  children: ReactNode
  mono?: boolean
  className?: string
}): ReactNode {
  return (
    <div className={cx('flex items-baseline justify-between gap-3 border-b border-line/40 py-1.5', className)}>
      <span className="nl-label shrink-0">{label}</span>
      <span className={cx('min-w-0 text-right text-[11px] break-words text-ink-dim', mono && 'font-mono')}>
        {children}
      </span>
    </div>
  )
}

/* ------------------------------------------------------------------ *
 * Decorative frame underline / header rule
 * ------------------------------------------------------------------ */

export function Rule({ className }: { className?: string }): ReactNode {
  return (
    <div className={cx('relative h-2 w-full', className)}>
      <FrameUnderline strokeWidth={1} squareSize={5} className="absolute inset-0" />
    </div>
  )
}

/* ------------------------------------------------------------------ *
 * Generic modal (used for the annotated PNG preview)
 * ------------------------------------------------------------------ */

export function Modal({
  open,
  title,
  onClose,
  children,
  wide = false,
}: {
  open: boolean
  title: ReactNode
  onClose: () => void
  children: ReactNode
  wide?: boolean
}): ReactNode {
  useEffect(() => {
    if (!open) return
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [open, onClose])

  if (!open) return null

  return (
    <div
      className="fixed inset-0 z-40 flex items-center justify-center bg-void/85 p-6 backdrop-blur-sm"
      onClick={onClose}
      role="presentation"
    >
      <div
        className={cx('relative w-full', wide ? 'max-w-6xl' : 'max-w-3xl')}
        onClick={(event) => event.stopPropagation()}
        role="dialog"
        aria-modal="true"
      >
        <Panel
          frame="nero"
          title={title}
          actions={<IconButton label="关闭" icon={<X size={13} />} onClick={onClose} />}
          bodyClassName="max-h-[78vh] overflow-auto scroll-thin"
        >
          {children}
        </Panel>
      </div>
    </div>
  )
}

/* ------------------------------------------------------------------ *
 * Copy-to-clipboard inline helper
 * ------------------------------------------------------------------ */

export function CopyButton({ value, label = '复制' }: { value: string; label?: string }): ReactNode {
  const [copied, setCopied] = useState(false)
  const timer = useRef<number | null>(null)

  useEffect(
    () => () => {
      if (timer.current !== null) window.clearTimeout(timer.current)
    },
    [],
  )

  return (
    <button
      type="button"
      title={label}
      onClick={() => {
        void navigator.clipboard?.writeText(value).then(
          () => {
            setCopied(true)
            if (timer.current !== null) window.clearTimeout(timer.current)
            timer.current = window.setTimeout(() => setCopied(false), 1400)
          },
          () => setCopied(false),
        )
      }}
      className="text-[10px] tracking-[0.1em] text-ink-faint uppercase hover:text-cyan-soft"
    >
      {copied ? '已复制' : label}
    </button>
  )
}
