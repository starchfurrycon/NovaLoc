/** NovaLoc / 新译 — small shared presentational helpers. */

export function cx(...values: Array<string | false | null | undefined>): string {
  return values.filter(Boolean).join(' ')
}

/** Format a 0..1 ratio as a percentage string. */
export function pct(ratio: number | null | undefined, digits = 1): string {
  if (ratio === null || ratio === undefined || !Number.isFinite(ratio)) return '—'
  const value = ratio > 1 ? ratio : ratio * 100
  return `${value.toFixed(digits)}%`
}

/** Clamp a 0..1 progress value. */
export function clamp01(value: number | null | undefined): number {
  if (value === null || value === undefined || !Number.isFinite(value)) return 0
  const normalized = value > 1 ? value / 100 : value
  return Math.min(1, Math.max(0, normalized))
}

/** Format a byte count. */
export function bytes(value: number): string {
  if (!Number.isFinite(value) || value <= 0) return '0 B'
  const units = ['B', 'KB', 'MB', 'GB', 'TB']
  const exponent = Math.min(units.length - 1, Math.floor(Math.log(value) / Math.log(1024)))
  const scaled = value / 1024 ** exponent
  return `${scaled.toFixed(exponent === 0 ? 0 : 1)} ${units[exponent]}`
}

/** Compact timestamp: `06-14 09:31:07`. */
export function shortTime(value: string | null | undefined): string {
  if (!value) return '—'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(
    date.getMinutes(),
  )}:${pad(date.getSeconds())}`
}

/** Clock only: `09:31:07.412`. */
export function clockTime(value: string | null | undefined): string {
  if (!value) return '--:--:--'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  const pad = (n: number, w = 2) => String(n).padStart(w, '0')
  return `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}.${pad(
    date.getMilliseconds(),
    3,
  )}`
}

/** Relative "x 分钟前" description. */
export function relativeTime(value: string | null | undefined): string {
  if (!value) return '—'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  const delta = (Date.now() - date.getTime()) / 1000
  if (delta < 45) return '刚刚'
  if (delta < 3600) return `${Math.round(delta / 60)} 分钟前`
  if (delta < 86400) return `${Math.round(delta / 3600)} 小时前`
  if (delta < 86400 * 30) return `${Math.round(delta / 86400)} 天前`
  return shortTime(value)
}

export type Tone = 'neutral' | 'ok' | 'warn' | 'err' | 'info' | 'accent'

export const TONE_TEXT: Record<Tone, string> = {
  neutral: 'text-ink-dim',
  ok: 'text-ok',
  warn: 'text-warn',
  err: 'text-err',
  info: 'text-info',
  accent: 'text-cyan',
}

export const TONE_BORDER: Record<Tone, string> = {
  neutral: 'border-line',
  ok: 'border-ok/40',
  warn: 'border-warn/40',
  err: 'border-err/45',
  info: 'border-info/40',
  accent: 'border-cyan/45',
}

export const TONE_BG: Record<Tone, string> = {
  neutral: 'bg-line/25',
  ok: 'bg-ok/12',
  warn: 'bg-warn/12',
  err: 'bg-err/12',
  info: 'bg-info/12',
  accent: 'bg-cyan/12',
}

/** Map an entry / issue status onto a visual tone. */
export function statusTone(status: string | null | undefined): Tone {
  switch ((status ?? '').toLowerCase()) {
    case 'translated':
    case 'done':
    case 'ok':
    case 'applied':
    case 'reviewed':
      return 'ok'
    case 'pending':
    case 'queued':
    case 'running':
    case 'skipped':
      return 'warn'
    case 'failed':
    case 'error':
    case 'critical':
      return 'err'
    case 'warning':
      return 'warn'
    case 'info':
      return 'info'
    default:
      return 'neutral'
  }
}

const STATUS_LABELS: Record<string, string> = {
  pending: '待处理',
  translated: '已翻译',
  reviewed: '已审校',
  skipped: '跳过',
  failed: '失败',
  queued: '排队中',
  running: '进行中',
  done: '完成',
  ok: '通过',
  warning: '警告',
  error: '错误',
  critical: '严重',
  info: '提示',
}

export function statusLabel(status: string | null | undefined): string {
  if (!status) return '未知'
  return STATUS_LABELS[status.toLowerCase()] ?? status
}

const KIND_LABELS: Record<string, string> = {
  dialogue: '对话',
  ui: '界面',
  item: '道具',
  quest: '任务',
  system: '系统',
  other: '其他',
}

export function kindLabel(kind: string | null | undefined): string {
  if (!kind) return '未分类'
  return KIND_LABELS[kind.toLowerCase()] ?? kind
}

/** Truncate long strings for table cells. */
export function truncate(value: string, max = 120): string {
  if (value.length <= max) return value
  return `${value.slice(0, max - 1)}…`
}

/** Human chapter/stage names for the pipeline. */
const STAGE_LABELS: Record<string, string> = {
  // 键必须与后端 `novaloc.pipeline.stages.STAGES` 的阶段 id **逐字一致**。
  // 以前这里写的是 `images`，而后端发的是 `images_scan` / `images_localize`，
  // 于是任务页和质检页会直接显示英文 id；新增的 `unpack` 同理。
  // 对不上的时候 `stageLabel()` 会退回显示原始 id —— 不难看，但很露怯。
  unpack: '解包资源',
  detect: '引擎识别',
  extract: '文本提取',
  images_scan: '扫描贴图',
  translate: '机器翻译',
  fonts: '字体审计',
  images_localize: '贴图汉化',
  qa: '质检',
  apply: '写回资源',
  job: '任务',
  // 旧键保留：历史工作区的日志里可能还是这两个写法
  images: '贴图识别',
}

export function stageLabel(stage: string | null | undefined): string {
  if (!stage) return '任务'
  return STAGE_LABELS[stage.toLowerCase()] ?? stage
}

/** Narrow an unknown record value into a display string. */
export function asText(value: unknown): string {
  if (value === null || value === undefined) return ''
  if (typeof value === 'string') return value
  if (typeof value === 'number' || typeof value === 'boolean') return String(value)
  try {
    return JSON.stringify(value)
  } catch {
    return String(value)
  }
}
