/** 设置 — Ollama / 模型 / OCR / DirectML / 字体策略. */

import { useCallback, useEffect, useMemo, useState, type ReactNode } from 'react'
import {
  Cpu,
  Database,
  Gauge,
  Languages,
  Plug,
  RefreshCw,
  RotateCcw,
  Save,
  ScanText,
  Server,
  Type,
} from 'lucide-react'
import { api } from '../api'
import type { Health, Settings, SettingsResponse } from '../api'
import { errorMessage, useAction, useResource, useToasts } from '../hooks'
import { cx } from '../ui'
import { PageHeader } from '../components/PageHeader'
import {
  Badge,
  Button,
  ErrorState,
  Field,
  Input,
  KV,
  LoadingState,
  Panel,
  Select,
  Toggle,
  ToastStack,
} from '../components/ui'

const FALLBACK_TARGETS = ['zh-Hans', 'zh-Hant', 'ja', 'ko', 'en', 'fr', 'de', 'es', 'ru']
const FALLBACK_OCR_TIERS = ['fast', 'balanced', 'accurate']
const FALLBACK_FONT_POLICIES = ['subset', 'repackage', 'report-only']

function pickSettings(payload: SettingsResponse): Settings {
  return {
    ollama_base_url: payload.ollama_base_url ?? '',
    model_translate: payload.model_translate ?? '',
    model_vision: payload.model_vision ?? '',
    target_language: payload.target_language ?? '',
    ocr_tier: payload.ocr_tier ?? 'balanced',
    use_directml: Boolean(payload.use_directml),
    font_policy: payload.font_policy ?? 'report-only',
  }
}

function Datalist({ id, options }: { id: string; options: string[] }): ReactNode {
  return (
    <datalist id={id}>
      {options.map((option) => (
        <option key={option} value={option} />
      ))}
    </datalist>
  )
}

export default function SettingsPage(): ReactNode {
  const toasts = useToasts()
  const [draft, setDraft] = useState<Settings | null>(null)

  const settings = useResource<SettingsResponse>((signal) => api.getSettings(signal), [])
  const health = useResource<Health>((signal) => api.health(signal), [], { intervalMs: 20000 })
  const save = useAction(api.putSettings)

  useEffect(() => {
    if (!settings.data) return
    setDraft(pickSettings(settings.data))
  }, [settings.data])

  const models = useMemo(() => {
    const fromSettings = settings.data?.models ?? []
    const fromHealth = health.data?.ollama.models ?? []
    return [...new Set([...fromSettings, ...fromHealth])].filter(Boolean).sort()
  }, [settings.data, health.data])

  const targets = useMemo(
    () => (settings.data?.target_languages?.length ? settings.data.target_languages : FALLBACK_TARGETS),
    [settings.data],
  )
  const tiers = useMemo(
    () => (settings.data?.ocr_tiers?.length ? settings.data.ocr_tiers : FALLBACK_OCR_TIERS),
    [settings.data],
  )
  const policies = useMemo(
    () =>
      settings.data?.font_policies?.length ? settings.data.font_policies : FALLBACK_FONT_POLICIES,
    [settings.data],
  )

  const baseline = useMemo(() => (settings.data ? pickSettings(settings.data) : null), [settings.data])

  const dirty = useMemo(() => {
    if (!draft || !baseline) return false
    return (Object.keys(baseline) as Array<keyof Settings>).some((key) => draft[key] !== baseline[key])
  }, [draft, baseline])

  const patch = useCallback(<K extends keyof Settings>(key: K, value: Settings[K]) => {
    setDraft((previous) => (previous ? { ...previous, [key]: value } : previous))
  }, [])

  const submit = useCallback(async () => {
    if (!draft) return
    const result = await save.run(draft)
    if (!result) {
      toasts.push('err', `保存失败：${errorMessage(save.error)}`)
      return
    }
    toasts.push('ok', '设置已保存。')
    settings.set(result)
    setDraft(pickSettings(result))
  }, [draft, save, settings, toasts])

  const inspect = useCallback(async () => {
    await health.reload()
    const data = health.data
    if (!data) {
      toasts.push('err', `后端不可达：${errorMessage(health.error)}`)
      return
    }
    toasts.push(
      data.ollama.available ? 'ok' : 'info',
      data.ollama.available
        ? `Ollama 可用（${data.ollama.models.length} 个模型）。`
        : `后端在线，但 Ollama 不可用（${data.ollama.base_url || '未配置地址'}）。`,
    )
  }, [health, toasts])

  if (settings.loading && !settings.data) return <LoadingState label="读取设置" />

  return (
    <div className="space-y-4">
      <PageHeader
        eyebrow="SETTINGS / 全局配置"
        title="设置"
        description="配置本地模型服务、OCR 精度档位、DirectML 加速与字体处理策略。所有请求都发往本机，不会上传任何数据。"
        meta={
          <>
            <Badge tone={health.data?.ollama.available ? 'ok' : 'warn'}>
              <Server size={10} />
              Ollama {health.data?.ollama.available ? '可用' : '不可用'}
            </Badge>
            <Badge tone={health.data?.gpu.directml ? 'accent' : 'neutral'}>
              <Cpu size={10} />
              {health.data?.gpu.name || 'CPU'}
            </Badge>
            {dirty && (
              <Badge tone="info">
                <Save size={10} />
                有未保存改动
              </Badge>
            )}
          </>
        }
        actions={
          <>
            <Button icon={<Plug size={12} />} onClick={() => void inspect()} busy={health.loading}>
              测试连接
            </Button>
            <Button
              icon={<RotateCcw size={12} />}
              disabled={!dirty || !baseline}
              onClick={() => baseline && setDraft(baseline)}
            >
              放弃改动
            </Button>
            <Button
              variant={dirty ? 'primary' : 'ghost'}
              icon={<Save size={12} />}
              disabled={!dirty || !draft}
              busy={save.pending}
              onClick={() => void submit()}
            >
              保存设置
            </Button>
          </>
        }
      />

      {settings.error && <ErrorState error={settings.error} onRetry={() => void settings.reload()} />}
      {save.error && <ErrorState error={save.error} />}

      {draft && (
        <div className="grid gap-3 xl:grid-cols-2">
          <Panel title="模型服务" subtitle="Ollama 与本机推理" icon={<Server size={13} />} frame="nero">
            <div className="space-y-3">
              <Field
                label="Ollama 基础地址"
                hint="默认 http://127.0.0.1:11434"
              >
                <Input
                  value={draft.ollama_base_url}
                  spellCheck={false}
                  placeholder="http://127.0.0.1:11434"
                  onChange={(event) => patch('ollama_base_url', event.target.value)}
                />
              </Field>

              <Field label="翻译模型" hint="用于批量文本翻译，建议使用 7B 以上的指令模型。">
                <Input
                  value={draft.model_translate}
                  list="nl-models"
                  spellCheck={false}
                  placeholder="qwen2.5:7b-instruct"
                  onChange={(event) => patch('model_translate', event.target.value)}
                />
              </Field>

              <Field label="视觉模型" hint="用于贴图文字识别与版面理解。">
                <Input
                  value={draft.model_vision}
                  list="nl-models"
                  spellCheck={false}
                  placeholder="qwen2.5vl:7b"
                  onChange={(event) => patch('model_vision', event.target.value)}
                />
              </Field>

              <Datalist id="nl-models" options={models} />

              <div className="border border-line/60 bg-abyss/50 px-3 py-2">
                <div className="nl-label mb-1 flex items-center gap-1.5">
                  <Database size={10} /> 已发现的本地模型（{models.length}）
                </div>
                {models.length === 0 ? (
                  <p className="text-[11px] text-ink-faint">
                    未从后端获取到模型列表。可直接填写模型名，或点击「测试连接」重新探测。
                  </p>
                ) : (
                  <div className="flex flex-wrap gap-1">
                    {models.map((model) => (
                      <button
                        key={model}
                        type="button"
                        onClick={() => patch('model_translate', model)}
                        title="设为翻译模型"
                        className="border border-line px-1.5 py-[1px] font-mono text-[10px] text-ink-dim hover:border-cyan/50 hover:text-cyan-soft"
                      >
                        {model}
                      </button>
                    ))}
                  </div>
                )}
              </div>
            </div>
          </Panel>

          <Panel title="本地化与 OCR" subtitle="目标语言与识别精度" icon={<ScanText size={13} />}>
            <div className="space-y-3">
              <Field label="目标语言" hint="写入 BCP-47 标签，影响字体字符集与译文风格。">
                <Select
                  value={targets.includes(draft.target_language) ? draft.target_language : '__custom__'}
                  onChange={(event) => {
                    if (event.target.value !== '__custom__') patch('target_language', event.target.value)
                  }}
                >
                  {targets.map((target) => (
                    <option key={target} value={target}>
                      {target}
                    </option>
                  ))}
                  <option value="__custom__">自定义…</option>
                </Select>
              </Field>

              <Field label="自定义语言标签">
                <Input
                  value={draft.target_language}
                  spellCheck={false}
                  onChange={(event) => patch('target_language', event.target.value)}
                />
              </Field>

              <Field label="OCR 精度档位" hint="档位越高，识别越准、耗时越长。">
                <Select
                  value={draft.ocr_tier}
                  onChange={(event) => patch('ocr_tier', event.target.value)}
                >
                  {tiers.map((tier) => (
                    <option key={tier} value={tier}>
                      {tier}
                    </option>
                  ))}
                  {!tiers.includes(draft.ocr_tier) && (
                    <option value={draft.ocr_tier}>{draft.ocr_tier}</option>
                  )}
                </Select>
              </Field>

              <Toggle
                checked={draft.use_directml}
                onChange={(next) => patch('use_directml', next)}
                label="启用 DirectML 加速"
                hint="在 Windows 上使用 GPU 推理；若驱动不稳定可关闭回退 CPU。"
              />

              <div className="border border-line/60 bg-abyss/50 px-3 py-2">
                <div className="nl-label mb-1 flex items-center gap-1.5">
                  <Gauge size={10} /> 当前 GPU
                </div>
                <p className="text-[11px] text-ink-dim">
                  {health.data?.gpu.name || '未探测到 GPU'}
                  {health.data?.gpu.directml ? ' · DirectML 已启用' : ''}
                </p>
              </div>
            </div>
          </Panel>

          <Panel title="字体策略" subtitle="写回资源时的字形处理方式" icon={<Type size={13} />}>
            <div className="space-y-3">
              <Field
                label="字体策略"
                hint="subset 仅子集化；repackage 重新打包字体；report-only 只出报告不改文件。"
              >
                <Select
                  value={draft.font_policy}
                  onChange={(event) => patch('font_policy', event.target.value)}
                >
                  {policies.map((policy) => (
                    <option key={policy} value={policy}>
                      {policy}
                    </option>
                  ))}
                  {!policies.includes(draft.font_policy) && (
                    <option value={draft.font_policy}>{draft.font_policy}</option>
                  )}
                </Select>
              </Field>

              <div className="border border-line/60 bg-abyss/50 px-3 py-2 text-[11px] leading-relaxed text-ink-faint">
                缺字会直接导致游戏内显示为方块。建议先以 <code className="text-cyan-dim">report-only</code>{' '}
                模式跑一遍完整流水线，确认覆盖情况后再切换为写回策略。
              </div>

              <div className="flex flex-wrap gap-1.5">
                {policies.map((policy) => (
                  <button
                    key={policy}
                    type="button"
                    onClick={() => patch('font_policy', policy)}
                    className={cx(
                      'border px-2 py-1 text-[10px] tracking-[0.1em] uppercase transition-colors',
                      draft.font_policy === policy
                        ? 'border-cyan/60 bg-cyan/12 text-cyan-soft'
                        : 'border-line text-ink-faint hover:text-ink-dim',
                    )}
                  >
                    {policy}
                  </button>
                ))}
              </div>
            </div>
          </Panel>

          <Panel title="当前生效值" subtitle="PUT /api/settings 提交内容预览" icon={<Languages size={13} />}>
            <div className="space-y-0.5">
              {(Object.keys(draft) as Array<keyof Settings>).map((key) => (
                <KV key={key} label={key}>
                  {typeof draft[key] === 'boolean' ? (draft[key] ? 'true' : 'false') : String(draft[key] || '—')}
                </KV>
              ))}
            </div>
            <pre className="scroll-thin mt-3 max-h-52 overflow-auto border border-line/50 bg-void/70 p-2 font-mono text-[10px] leading-relaxed text-ink-dim">
              {JSON.stringify(draft, null, 2)}
            </pre>
            <div className="mt-3 flex items-center gap-2">
              <Button size="sm" icon={<RefreshCw size={11} />} onClick={() => void settings.reload()}>
                重新拉取
              </Button>
              <Button
                size="sm"
                variant={dirty ? 'primary' : 'ghost'}
                icon={<Save size={11} />}
                disabled={!dirty}
                busy={save.pending}
                onClick={() => void submit()}
              >
                提交
              </Button>
            </div>
          </Panel>
        </div>
      )}

      <ToastStack items={toasts.items} onDismiss={toasts.dismiss} />
    </div>
  )
}
