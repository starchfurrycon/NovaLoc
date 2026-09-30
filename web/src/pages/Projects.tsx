/** 项目 — project list, creation and deletion. */

import { useCallback, useEffect, useMemo, useState, type ReactNode } from 'react'
import { useNavigate } from 'react-router-dom'
import {
  Activity,
  Cpu,
  FileText,
  FolderPlus,
  FolderTree,
  Gauge,
  Image as ImageIcon,
  Plus,
  RefreshCw,
  ScanLine,
  Trash2,
  Type,
  X,
} from 'lucide-react'
import { api } from '../api'
import type { CreateProjectBody, Health, Project } from '../api'
import { useAction, useResource, useToasts } from '../hooks'
import { asText, clamp01, cx, relativeTime, shortTime, stageLabel } from '../ui'
import { PageHeader } from '../components/PageHeader'
import {
  Badge,
  Button,
  EffText,
  EmptyState,
  ErrorState,
  Field,
  IconButton,
  Input,
  LoadingState,
  Panel,
  ProgressBar,
  Select,
  ToastStack,
} from '../components/ui'

function ProjectProgressSummary({ progress }: { progress: Record<string, unknown> }): ReactNode {
  const stage = asText(progress['stage'] ?? progress['current_stage'])
  const ratio = clamp01(Number(progress['progress'] ?? progress['ratio'] ?? 0))
  const status = asText(progress['status'])
  const message = asText(progress['message'])
  if (!stage && !status && ratio <= 0) {
    return <span className="text-[10px] text-ink-faint">暂无流水线记录</span>
  }
  return (
    <div className="space-y-1">
      <div className="flex items-center justify-between gap-2">
        <span className="text-[10px] tracking-[0.1em] text-ink-dim uppercase">
          {stageLabel(stage) || '任务'}
          {status ? ` · ${status}` : ''}
        </span>
        <span className="font-mono text-[10px] text-ink-faint">{Math.round(ratio * 100)}%</span>
      </div>
      <ProgressBar value={ratio} height={3} />
      {message && <p className="truncate text-[10px] text-ink-faint">{message}</p>}
    </div>
  )
}

export default function ProjectsPage(): ReactNode {
  const navigate = useNavigate()
  const toasts = useToasts()
  const [showForm, setShowForm] = useState(false)
  const [confirming, setConfirming] = useState<string | null>(null)

  const [name, setName] = useState('')
  const [gameDir, setGameDir] = useState('')
  const [engine, setEngine] = useState('')
  const [engineCustom, setEngineCustom] = useState('')
  const [formError, setFormError] = useState<string | null>(null)

  const health = useResource<Health>((signal) => api.health(signal), [], { intervalMs: 30000 })
  const projects = useResource<Project[]>((signal) => api.listProjects(signal), [], {
    intervalMs: 15000,
  })

  const create = useAction(api.createProject)
  const remove = useAction(api.deleteProject)

  const engines = useMemo(() => health.data?.engines ?? [], [health.data])

  useEffect(() => {
    if (!confirming) return
    const timer = window.setTimeout(() => setConfirming(null), 4000)
    return () => window.clearTimeout(timer)
  }, [confirming])

  const resetForm = useCallback(() => {
    setName('')
    setGameDir('')
    setEngine('')
    setEngineCustom('')
    setFormError(null)
  }, [])

  const submit = useCallback(async () => {
    const trimmedName = name.trim()
    const trimmedDir = gameDir.trim()
    if (!trimmedName) {
      setFormError('请填写项目名称。')
      return
    }
    if (!trimmedDir) {
      setFormError('请填写游戏目录（绝对路径）。')
      return
    }
    const chosenEngine = engine === '__custom__' ? engineCustom.trim() : engine.trim()
    const body: CreateProjectBody = { name: trimmedName, game_dir: trimmedDir }
    if (chosenEngine) body.engine = chosenEngine

    const created = await create.run(body)
    if (!created) {
      setFormError(create.error?.message ?? '创建失败。')
      return
    }
    toasts.push('ok', `项目「${created.name}」已创建。`)
    resetForm()
    setShowForm(false)
    await projects.reload({ quiet: true })
  }, [name, gameDir, engine, engineCustom, create, toasts, resetForm, projects])

  const destroy = useCallback(
    async (project: Project) => {
      if (confirming !== project.id) {
        setConfirming(project.id)
        return
      }
      setConfirming(null)
      const result = await remove.run(project.id)
      if (!result?.ok) {
        toasts.push('err', remove.error?.message ?? '删除失败。')
        return
      }
      toasts.push('ok', `项目「${project.name}」已删除。`)
      await projects.reload({ quiet: true })
    },
    [confirming, remove, toasts, projects],
  )

  const list = projects.data ?? []

  return (
    <div className="space-y-4">
      <PageHeader
        eyebrow="PROJECTS / 本地化项目"
        title="项目"
        description="管理本地游戏项目。每个项目对应一个游戏目录，流水线会识别引擎、提取文本与贴图、审计字体覆盖，并把译文写回资源。"
        meta={
          <>
            <Badge tone="accent">
              <FolderTree size={10} />
              {list.length} 个项目
            </Badge>
            <Badge tone={health.data?.ollama.available ? 'ok' : 'warn'}>
              Ollama {health.data?.ollama.available ? '就绪' : '未就绪'}
            </Badge>
            {health.data?.gpu.directml && <Badge tone="info">DirectML 加速</Badge>}
          </>
        }
        actions={
          <>
            <Button
              icon={<RefreshCw size={12} />}
              onClick={() => void projects.reload()}
              busy={projects.loading}
            >
              刷新
            </Button>
            <Button
              variant="primary"
              icon={showForm ? <X size={12} /> : <Plus size={12} />}
              onClick={() => {
                setShowForm((open) => !open)
                setFormError(null)
              }}
            >
              {showForm ? '取消' : '新建项目'}
            </Button>
          </>
        }
      />

      {showForm && (
        <Panel title="新建项目" icon={<FolderPlus size={13} />} frame="nero">
          <div className="grid gap-3 md:grid-cols-3">
            <Field label="项目名称" required>
              <Input
                value={name}
                onChange={(event) => setName(event.target.value)}
                placeholder="例：星海旅人 中文汉化"
                autoFocus
              />
            </Field>
            <Field label="游戏目录" required hint="支持绝对路径，后端会校验目录是否存在。">
              <Input
                value={gameDir}
                onChange={(event) => setGameDir(event.target.value)}
                placeholder={'D:\\Games\\SomeGame'}
                spellCheck={false}
              />
            </Field>
            <Field label="引擎" hint="留空则由流水线自动识别。">
              <Select value={engine} onChange={(event) => setEngine(event.target.value)}>
                <option value="">自动识别</option>
                {engines.map((option) => (
                  <option key={option.id} value={option.id}>
                    {option.display_name} ({option.id})
                  </option>
                ))}
                <option value="__custom__">自定义…</option>
              </Select>
            </Field>
          </div>

          {engine === '__custom__' && (
            <div className="mt-3 max-w-xs">
              <Field label="自定义引擎 ID">
                <Input
                  value={engineCustom}
                  onChange={(event) => setEngineCustom(event.target.value)}
                  placeholder="unity / unreal / renpy …"
                  spellCheck={false}
                />
              </Field>
            </div>
          )}

          {formError && <ErrorState error={new Error(formError)} className="mt-3" />}
          {create.error && <ErrorState error={create.error} className="mt-3" />}

          <div className="mt-4 flex items-center gap-2">
            <Button variant="primary" icon={<Plus size={12} />} busy={create.pending} onClick={() => void submit()}>
              创建并入库
            </Button>
            <Button
              variant="subtle"
              onClick={() => {
                resetForm()
                setShowForm(false)
              }}
            >
              重置
            </Button>
          </div>
        </Panel>
      )}

      {projects.error && <ErrorState error={projects.error} onRetry={() => void projects.reload()} />}
      {projects.loading && !projects.data && <LoadingState label="读取项目列表" />}

      {!projects.loading && !projects.error && list.length === 0 && (
        <EmptyState
          icon={<FolderTree size={22} />}
          title="暂无项目"
          description="点击右上角「新建项目」，填入游戏目录即可开始。所有处理都在本机完成，不会上传任何游戏资源。"
          action={
            <Button variant="primary" icon={<Plus size={12} />} onClick={() => setShowForm(true)}>
              新建项目
            </Button>
          }
        />
      )}

      <div className="grid gap-3 xl:grid-cols-2">
        {list.map((project) => (
          <Panel
            key={project.id}
            frame="corners"
            className="transition-colors hover:border-cyan/30"
          >
            <div className="flex items-start justify-between gap-3">
              <div className="min-w-0">
                <h3 className="truncate text-[13px] tracking-[0.08em] text-ink">
                  <EffText>{project.name}</EffText>
                </h3>
                <p className="mt-0.5 truncate font-mono text-[10px] text-ink-faint" title={project.game_dir}>
                  {project.game_dir}
                </p>
              </div>
              <IconButton
                label={confirming === project.id ? '再次点击确认删除' : '删除项目'}
                variant="danger"
                icon={<Trash2 size={12} />}
                disabled={remove.pending}
                onClick={() => void destroy(project)}
                className={cx(confirming === project.id && 'border-err text-err')}
              />
            </div>

            <div className="mt-2.5 flex flex-wrap items-center gap-1.5">
              <Badge tone="accent">
                <Cpu size={10} />
                {project.engine ?? '未识别'}
                {project.engine_version ? ` ${project.engine_version}` : ''}
              </Badge>
              <Badge tone="neutral">ID {project.id.slice(0, 8)}</Badge>
              <Badge tone="neutral" className="normal-case">
                <Activity size={10} />
                更新 {relativeTime(project.updated_at)}
              </Badge>
              <span className="text-[9px] tracking-[0.12em] text-ink-faint/70 uppercase">
                创建 {shortTime(project.created_at)}
              </span>
            </div>

            <div className="mt-3 border-t border-line/50 pt-2.5">
              <ProjectProgressSummary progress={project.progress ?? {}} />
            </div>

            <div className="mt-3 flex flex-wrap items-center gap-1.5">
              <Button size="sm" icon={<ScanLine size={11} />} onClick={() => navigate(`/scan/${project.id}`)}>
                扫描
              </Button>
              <Button size="sm" icon={<FileText size={11} />} onClick={() => navigate(`/text/${project.id}`)}>
                文本审校
              </Button>
              <Button size="sm" icon={<ImageIcon size={11} />} onClick={() => navigate(`/images/${project.id}`)}>
                贴图审校
              </Button>
              <Button size="sm" icon={<Type size={11} />} onClick={() => navigate(`/fonts/${project.id}`)}>
                字体
              </Button>
              <Button size="sm" icon={<Gauge size={11} />} onClick={() => navigate(`/qa/${project.id}`)}>
                质检
              </Button>
            </div>
          </Panel>
        ))}
      </div>

      <ToastStack items={toasts.items} onDismiss={toasts.dismiss} />
    </div>
  )
}
