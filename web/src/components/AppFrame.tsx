/**
 * NovaLoc / 新译 — application shell.
 *
 * Restrained light-sci-fi chrome: Arwes animated background + frames, a fixed
 * navigation rail, a status header fed by `GET /api/health`, and a routed body.
 */

import { useCallback, useEffect, useMemo, useState, type ReactNode } from 'react'
import { NavLink, Route, Routes, useLocation, useNavigate } from 'react-router-dom'
import {
  Animator,
  AnimatorGeneralProvider,
  BleepsProvider,
  Dots,
  GridLines,
  Illuminator,
  MovingLines,
} from '@arwes/react'
import {
  Activity,
  AlertTriangle,
  ArrowLeft,
  Cpu,
  FileText,
  FolderTree,
  Gauge,
  Image as ImageIcon,
  Layers,
  LayoutGrid,
  Menu,
  RefreshCw,
  ScanLine,
  Settings as SettingsIcon,
  Type,
  Wifi,
  WifiOff,
  X,
} from 'lucide-react'
import { api } from '../api'
import type { Health } from '../api'
import { cx } from '../ui'
import { Badge, Button, EffText, IconButton } from './ui'

import ProjectsPage from '../pages/Projects'
import ScanPage from '../pages/Scan'
import TextPage from '../pages/TextReview'
import ImagesPage from '../pages/Images'
import FontsPage from '../pages/Fonts'
import JobPage from '../pages/Job'
import SettingsPage from '../pages/Settings'
import QaPage from '../pages/Qa'
import NotFoundPage from '../pages/NotFound'

/* ------------------------------------------------------------------ *
 * Bleeps — protocol-level UI feedback, synthesized at runtime so the app
 * ships no audio assets.
 * ------------------------------------------------------------------ */

type BleepName = 'click' | 'open' | 'error'

function encodeWav(samples: Float32Array, sampleRate: number): string {
  const buffer = new ArrayBuffer(44 + samples.length * 2)
  const view = new DataView(buffer)
  const writeAscii = (offset: number, text: string) => {
    for (let index = 0; index < text.length; index += 1) {
      view.setUint8(offset + index, text.charCodeAt(index))
    }
  }
  writeAscii(0, 'RIFF')
  view.setUint32(4, 36 + samples.length * 2, true)
  writeAscii(8, 'WAVE')
  writeAscii(12, 'fmt ')
  view.setUint32(16, 16, true)
  view.setUint16(20, 1, true)
  view.setUint16(22, 1, true)
  view.setUint32(24, sampleRate, true)
  view.setUint32(28, sampleRate * 2, true)
  view.setUint16(32, 2, true)
  view.setUint16(34, 16, true)
  writeAscii(36, 'data')
  view.setUint32(40, samples.length * 2, true)
  for (let index = 0; index < samples.length; index += 1) {
    const clamped = Math.max(-1, Math.min(1, samples[index] ?? 0))
    view.setInt16(44 + index * 2, clamped * 0x7fff, true)
  }
  let binary = ''
  const bytes = new Uint8Array(buffer)
  for (let index = 0; index < bytes.length; index += 1) {
    binary += String.fromCharCode(bytes[index]!)
  }
  return `data:audio/wav;base64,${btoa(binary)}`
}

function tone(frequency: number, duration: number, decay = 18): string {
  const sampleRate = 22050
  const total = Math.floor(sampleRate * duration)
  const samples = new Float32Array(total)
  for (let index = 0; index < total; index += 1) {
    const t = index / sampleRate
    const envelope = Math.exp(-decay * t)
    samples[index] = Math.sin(2 * Math.PI * frequency * t) * envelope * 0.28
  }
  return encodeWav(samples, sampleRate)
}

const BLEEPS: Record<BleepName, { sources: Array<{ src: string; type: string }> }> = {
  click: { sources: [{ src: tone(880, 0.05), type: 'audio/wav' }] },
  open: { sources: [{ src: tone(523.25, 0.12, 12), type: 'audio/wav' }] },
  error: { sources: [{ src: tone(174.61, 0.2, 9), type: 'audio/wav' }] },
}

/* ------------------------------------------------------------------ *
 * Navigation
 * ------------------------------------------------------------------ */

type NavItem = {
  to: string
  label: string
  hint: string
  icon: ReactNode
  /** Route params this entry needs when no project is selected. */
  needsProject?: 'scan' | 'text' | 'images' | 'fonts' | 'qa'
}

const NAV: NavItem[] = [
  { to: '/', label: '项目', hint: '项目管理', icon: <FolderTree size={14} /> },
  { to: '/scan/:id', label: '扫描', hint: '引擎与提取', icon: <ScanLine size={14} />, needsProject: 'scan' },
  { to: '/text/:id', label: '文本审校', hint: '译文校对', icon: <FileText size={14} />, needsProject: 'text' },
  { to: '/images/:id', label: '贴图审校', hint: 'OCR 图块', icon: <ImageIcon size={14} />, needsProject: 'images' },
  { to: '/fonts/:id', label: '字体', hint: '覆盖审计', icon: <Type size={14} />, needsProject: 'fonts' },
  { to: '/jobs/:id', label: '任务进度', hint: '实时日志', icon: <Activity size={14} /> },
  { to: '/qa/:id', label: '质检报告', hint: '问题清单', icon: <Gauge size={14} />, needsProject: 'qa' },
  { to: '/settings', label: '设置', hint: '模型与 OCR', icon: <SettingsIcon size={14} /> },
]

/** Extract a project id from any route so the nav rail can keep context. */
function useContextProjectId(): string | null {
  const { pathname } = useLocation()
  const match = /^\/(?:scan|text|images|fonts|qa|jobs)\/([^/]+)/.exec(pathname)
  return match?.[1] ? decodeURIComponent(match[1]) : null
}

function NavRail({ collapsed, onNavigate }: { collapsed: boolean; onNavigate: () => void }): ReactNode {
  const projectId = useContextProjectId()
  const { pathname } = useLocation()

  return (
    <nav className="flex flex-col gap-0.5 p-2">
      {NAV.map((item) => {
        const to = item.needsProject
          ? item.needsProject === 'scan'
            ? `/scan/${projectId ?? ''}`
            : item.to.replace(':id', projectId ?? '')
          : item.to.replace(':id', projectId ?? '')
        const disabled = item.needsProject !== undefined && !projectId
        const active =
          item.to === '/'
            ? pathname === '/'
            : pathname.startsWith(item.to.split('/:')[0] ?? item.to)

        if (disabled) {
          return (
            <span
              key={item.to}
              title="请先选择项目"
              className="flex cursor-not-allowed items-center gap-2.5 border border-transparent px-2.5 py-2 text-[11px] tracking-[0.06em] text-ink-faint/45"
            >
              <span className="opacity-50">{item.icon}</span>
              {!collapsed && <span className="truncate">{item.label}</span>}
            </span>
          )
        }

        return (
          <NavLink
            key={item.to}
            to={to}
            onClick={onNavigate}
            className={cx(
              'group relative flex items-center gap-2.5 border px-2.5 py-2 text-[11px] tracking-[0.06em] transition-colors',
              active
                ? 'border-cyan/50 bg-cyan/10 text-cyan-soft'
                : 'border-transparent text-ink-dim hover:border-line hover:bg-line/15 hover:text-ink',
            )}
          >
            <span className={cx('shrink-0', active ? 'text-cyan' : 'text-ink-faint group-hover:text-cyan/80')}>
              {item.icon}
            </span>
            {!collapsed && (
              <span className="flex min-w-0 flex-col">
                <span className="truncate">{item.label}</span>
                <span className="truncate text-[9px] tracking-[0.14em] text-ink-faint uppercase">
                  {item.hint}
                </span>
              </span>
            )}
            {active && <span className="absolute top-1.5 bottom-1.5 -left-[9px] w-[2px] bg-cyan" />}
          </NavLink>
        )
      })}
    </nav>
  )
}

/* ------------------------------------------------------------------ *
 * Health strip
 * ------------------------------------------------------------------ */

function HealthStrip({ health, error, onRefresh }: { health: Health | null; error: string | null; onRefresh: () => void }): ReactNode {
  if (error) {
    return (
      <div className="flex items-center gap-2 text-[10px] tracking-[0.1em] text-err uppercase">
        <WifiOff size={12} />
        <span className="truncate">后端离线</span>
        <IconButton label="重新检测" icon={<RefreshCw size={11} />} onClick={onRefresh} className="h-5 w-5" />
      </div>
    )
  }
  if (!health) {
    return (
      <div className="nl-pulse text-[10px] tracking-[0.14em] text-ink-faint uppercase">连接后端…</div>
    )
  }
  return (
    <div className="flex flex-wrap items-center gap-1.5">
      <Badge tone={health.ok ? 'ok' : 'warn'}>
        <Wifi size={10} />
        {health.ok ? '在线' : '异常'}
      </Badge>
      <Badge tone="neutral">v{health.version || '?'}</Badge>
      <Badge tone={health.ollama.available ? 'ok' : 'warn'}>
        <Layers size={10} />
        Ollama {health.ollama.available ? `${health.ollama.models.length} 模型` : '不可用'}
      </Badge>
      <Badge tone={health.gpu.directml ? 'accent' : 'neutral'}>
        <Cpu size={10} />
        {health.gpu.name || 'CPU'}
        {health.gpu.directml ? ' · DML' : ''}
      </Badge>
      <IconButton label="刷新状态" icon={<RefreshCw size={11} />} onClick={onRefresh} className="h-5 w-5" />
    </div>
  )
}

/* ------------------------------------------------------------------ *
 * Shell
 * ------------------------------------------------------------------ */

export default function AppFrame(): ReactNode {
  const [health, setHealth] = useState<Health | null>(null)
  const [healthError, setHealthError] = useState<string | null>(null)
  const [railOpen, setRailOpen] = useState(false)
  const [poll, setPoll] = useState(0)
  const navigate = useNavigate()
  const location = useLocation()

  const loadHealth = useCallback((signal?: AbortSignal) => {
    return api.health(signal).then(
      (result) => {
        setHealth(result)
        setHealthError(null)
      },
      (cause: unknown) => {
        if (signal?.aborted) return
        setHealthError(cause instanceof Error ? cause.message : String(cause))
      },
    )
  }, [])

  useEffect(() => {
    const controller = new AbortController()
    void loadHealth(controller.signal)
    const interval = window.setInterval(() => void loadHealth(controller.signal), 15000)
    return () => {
      controller.abort()
      window.clearInterval(interval)
    }
  }, [loadHealth, poll])

  // Close the mobile rail whenever the route changes.
  useEffect(() => {
    setRailOpen(false)
  }, [location.pathname])

  const canGoBack = location.pathname !== '/'

  const creator = useMemo(
    () => (
      <AnimatorGeneralProvider duration={{ enter: 0.35, exit: 0.22 }}>
        <BleepsProvider bleeps={BLEEPS} common={{ volume: 0.35 }}>
          <div className="relative flex h-full min-h-0 flex-col bg-void">
            <div className="pointer-events-none absolute inset-0 overflow-hidden opacity-70">
              <GridLines lineColor="rgba(46,230,214,0.045)" distance={56} />
              <MovingLines lineColor="rgba(46,230,214,0.10)" distance={28} sets={5} />
              <Dots color="rgba(46,230,214,0.14)" distance={36} size={1.4} type="cross" />
              <Illuminator color="rgba(46,230,214,0.05)" size={480} />
            </div>

            <header className="relative z-20 flex h-12 shrink-0 items-center justify-between gap-3 border-b border-line/70 bg-abyss/85 px-3 backdrop-blur">
              <div className="flex min-w-0 items-center gap-3">
                <IconButton
                  label="切换导航"
                  icon={railOpen ? <X size={13} /> : <Menu size={13} />}
                  className="lg:hidden"
                  onClick={() => setRailOpen((open) => !open)}
                />
                <button
                  type="button"
                  onClick={() => navigate('/')}
                  className="flex items-baseline gap-2 whitespace-nowrap"
                >
                  <span className="text-[13px] font-medium tracking-[0.3em] text-cyan-soft">
                    <EffText>NOVALOC</EffText>
                  </span>
                  <span className="text-[11px] tracking-[0.3em] text-ink-faint">新译</span>
                </button>
                {canGoBack && (
                  <Button
                    size="sm"
                    variant="subtle"
                    icon={<ArrowLeft size={11} />}
                    onClick={() => navigate(-1)}
                    className="hidden sm:inline-flex"
                  >
                    返回
                  </Button>
                )}
              </div>
              <HealthStrip health={health} error={healthError} onRefresh={() => setPoll((n) => n + 1)} />
            </header>

            <div className="relative z-10 flex min-h-0 flex-1">
              <aside
                className={cx(
                  'scroll-thin absolute inset-y-0 left-0 z-30 w-52 shrink-0 overflow-y-auto border-r border-line/70 bg-abyss/95 backdrop-blur transition-transform lg:static lg:translate-x-0',
                  railOpen ? 'translate-x-0' : '-translate-x-full',
                )}
              >
                <div className="border-b border-line/50 px-3 py-2">
                  <span className="nl-label">导航 / NAV</span>
                </div>
                <NavRail collapsed={false} onNavigate={() => setRailOpen(false)} />
                <div className="mt-2 border-t border-line/50 px-3 py-3 text-[9px] leading-relaxed tracking-[0.1em] text-ink-faint/80 uppercase">
                  <div className="mb-1 flex items-center gap-1.5">
                    <AlertTriangle size={10} />
                    本地离线流水线
                  </div>
                  <div>文本 · 贴图 · 字体 · 质检</div>
                </div>
              </aside>

              {railOpen && (
                <div
                  className="absolute inset-0 z-20 bg-void/70 lg:hidden"
                  onClick={() => setRailOpen(false)}
                  role="presentation"
                />
              )}

              <main className="scroll-thin min-w-0 flex-1 overflow-y-auto">
                <div className="mx-auto w-full max-w-[1400px] p-4">
                  <Animator>
                    <Routes>
                      <Route path="/" element={<ProjectsPage />} />
                      <Route path="/scan/:id" element={<ScanPage />} />
                      <Route path="/text/:id" element={<TextPage />} />
                      <Route path="/images/:id" element={<ImagesPage />} />
                      <Route path="/fonts/:id" element={<FontsPage />} />
                      <Route path="/jobs/:id" element={<JobPage />} />
                      <Route path="/settings" element={<SettingsPage />} />
                      <Route path="/qa/:id" element={<QaPage />} />
                      <Route path="*" element={<NotFoundPage />} />
                    </Routes>
                  </Animator>
                </div>
              </main>
            </div>

            <footer className="relative z-10 flex shrink-0 items-center justify-between gap-3 border-t border-line/60 bg-abyss/80 px-3 py-1 text-[9px] tracking-[0.16em] text-ink-faint uppercase">
              <span>NovaLoc / 新译 · 游戏本地化工作台</span>
              <span className="truncate">
                {health?.engines?.length ? `引擎支持 ${health.engines.length}` : '引擎列表未就绪'}
              </span>
            </footer>
          </div>
        </BleepsProvider>
      </AnimatorGeneralProvider>
    ),
    [health, healthError, navigate, railOpen, canGoBack, poll],
  )

  return creator
}
