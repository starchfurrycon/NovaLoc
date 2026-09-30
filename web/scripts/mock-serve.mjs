/**
 * Local smoke harness (development only — not part of the app build).
 *
 * Serves `dist/` and a mock of the frozen API contract so the built bundle can
 * be exercised without the real FastAPI backend.
 *
 *   node scripts/mock-serve.mjs [port]
 */

import { createServer } from 'node:http'
import { createHash } from 'node:crypto'
import { readFile, stat } from 'node:fs/promises'
import { extname, join, normalize, resolve } from 'node:path'

const root = resolve(import.meta.dirname, '..')
const dist = join(root, 'dist')
const port = Number(process.argv[2] ?? 5599)

const MIME = {
  '.html': 'text/html; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.svg': 'image/svg+xml',
  '.png': 'image/png',
  '.json': 'application/json; charset=utf-8',
}

const now = new Date().toISOString()
const project = {
  id: 'p-1',
  name: '星海旅人 汉化',
  game_dir: 'D:\\Games\\StarTraveler',
  engine: 'unity',
  engine_version: '2021.3.14f1',
  created_at: now,
  updated_at: now,
  progress: { stage: 'translate', status: 'running', progress: 0.42, message: '已翻译 420/1000' },
}

const PNG = Buffer.from(
  'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8DwHwAFAAH/q842iQAAAABJRU5ErkJggg==',
  'base64',
)

const routes = {
  'GET /api/health': () => ({
    ok: true,
    version: '0.1.0',
    ollama: { available: true, base_url: 'http://127.0.0.1:11434', models: ['qwen2.5:7b', 'qwen2.5vl:7b'] },
    gpu: { directml: true, name: 'AMD Radeon 780M' },
    engines: [
      { id: 'unity', display_name: 'Unity' },
      { id: 'unreal', display_name: 'Unreal Engine' },
      { id: 'renpy', display_name: 'Ren\u2019Py' },
      { id: 'rpgmaker', display_name: 'RPG Maker' },
    ],
  }),
  'GET /api/projects': () => [project],
  'GET /api/projects/p-1': () => project,
  'POST /api/projects/p-1/detect': () => ({
    engine_id: 'unity',
    display_name: 'Unity',
    confidence: 0.93,
    version: '2021.3.14f1',
    evidence: ['Assets/ 目录存在', 'globalgamemanagers 文件命中', 'Managed/Assembly-CSharp.dll'],
  }),
  'POST /api/projects/p-1/extract': () => ({ job_id: 'job-1' }),
  'POST /api/projects/p-1/translate': () => ({ job_id: 'job-1' }),
  'POST /api/projects/p-1/fonts': () => ({ job_id: 'job-1' }),
  'POST /api/projects/p-1/images': () => ({ job_id: 'job-1' }),
  'POST /api/projects/p-1/run': () => ({ job_id: 'job-1' }),
  'POST /api/projects/p-1/apply': () => ({ job_id: 'job-1' }),
  'GET /api/projects/p-1/text': () => ({
    total: 3,
    entries: [
      { uid: 't1', source: 'Hello, {name}!', target: '你好，{name}！', status: 'translated', kind: 'dialogue', speaker: 'Aria', warnings: [] },
      { uid: 't2', source: 'Use %s to open the gate', target: '使用 ??? 打开大门', status: 'pending', kind: 'ui', speaker: null, warnings: ['占位符数量不一致: 期望 1 个, 实际 0 个'] },
      { uid: 't3', source: 'Potion of Healing', target: '', status: 'pending', kind: 'item', speaker: null, warnings: [] },
    ],
  }),
  'PATCH /api/projects/p-1/text': () => ({ updated: 1 }),
  'GET /api/projects/p-1/images': () => ({
    total: 1,
    images: [
      {
        uid: 'img1',
        path: 'Assets/UI/title.png',
        width: 1024,
        height: 512,
        blocks: [
          { id: 'b1', source: 'START GAME', target: '开始游戏', status: 'translated', confidence: 0.97 },
          { id: 'b2', source: 'OPTIONS', target: '', status: 'pending', confidence: 0.88 },
        ],
      },
    ],
  }),
  'PATCH /api/projects/p-1/images': () => ({ updated: 1 }),
  'GET /api/projects/p-1/fonts': () => ({
    coverage: [
      { font_id: 'f1', path: 'Assets/Fonts/main.ttf', family: 'Noto Sans SC', coverage_ratio: 0.9871, missing_count: 214 },
      { font_id: 'f2', path: 'Assets/Fonts/ui.ttf', family: 'Source Han Sans', coverage_ratio: 1, missing_count: 0 },
    ],
    patches: [{ font_id: 'f1', family: 'Noto Sans SC', status: 'planned', added_count: 214, plan: '子集化并合并缺失字形' }],
  }),
  'GET /api/projects/p-1/qa': () => ({
    ok: false,
    issues: [
      { severity: 'error', stage: 'translate', message: '占位符数量不一致', detail: 'uid=t2 source="Use %s to open the gate"' },
      { severity: 'warning', stage: 'fonts', message: '字体缺字 214 个', detail: 'Assets/Fonts/main.ttf' },
      { severity: 'info', stage: 'images', message: '1 个图块未翻译', detail: 'img1/b2' },
    ],
    stats: { entries: 3, translated: 1, pending: 2, images: 1, blocks: 2, fonts: 2 },
  }),
  'GET /api/jobs/job-1': () => ({
    id: 'job-1',
    stage: 'translate',
    status: 'running',
    progress: 0.42,
    message: '已翻译 420/1000',
    started_at: now,
    finished_at: null,
    error: null,
    events: [
      { kind: 'stage_start', stage: 'detect', message: '识别引擎', progress: 0, severity: 'info', data: null, ts: now },
      { kind: 'stage_end', stage: 'detect', message: 'Unity 2021.3.14f1', progress: 1, severity: 'info', data: null, ts: now },
      { kind: 'progress', stage: 'translate', message: '已翻译 420/1000', progress: 0.42, severity: 'info', data: null, ts: now },
    ],
  }),
  'GET /api/settings': () => ({
    ollama_base_url: 'http://127.0.0.1:11434',
    model_translate: 'qwen2.5:7b',
    model_vision: 'qwen2.5vl:7b',
    target_language: 'zh-Hans',
    ocr_tier: 'balanced',
    use_directml: true,
    font_policy: 'report-only',
    models: ['qwen2.5:7b', 'qwen2.5vl:7b'],
    target_languages: ['zh-Hans', 'zh-Hant', 'ja', 'en'],
    ocr_tiers: ['fast', 'balanced', 'accurate'],
    font_policies: ['subset', 'repackage', 'report-only'],
  }),
  'PUT /api/settings': () => routes['GET /api/settings'](),
  'GET /api/projects/p-1/images/img1/annotated': () => PNG,
}

async function serveFile(path, res) {
  try {
    const info = await stat(path)
    if (!info.isFile()) throw new Error('not a file')
    const body = await readFile(path)
    res.writeHead(200, { 'content-type': MIME[extname(path)] ?? 'application/octet-stream' })
    res.end(body)
    return true
  } catch {
    return false
  }
}

/* ------------------------------------------------------------------ *
 * Minimal RFC6455 server push for /ws/jobs/{id} (text frames only).
 * ------------------------------------------------------------------ */

const WS_GUID = '258EAFA5-E914-47DA-95CA-C5AB0DC85B11'

function wsAccept(key) {
  return createHash('sha1')
    .update(key + WS_GUID)
    .digest('base64')
}

function wsFrame(text) {
  const payload = Buffer.from(text, 'utf8')
  if (payload.length < 126) {
    return Buffer.concat([Buffer.from([0x81, payload.length]), payload])
  }
  const header = Buffer.alloc(4)
  header[0] = 0x81
  header[1] = 126
  header.writeUInt16BE(payload.length, 2)
  return Buffer.concat([header, payload])
}

function handleUpgrade(req, socket, jobId) {
  const key = req.headers['sec-websocket-key']
  if (!key) {
    socket.destroy()
    return
  }
  socket.write(
    [
      'HTTP/1.1 101 Switching Protocols',
      'Upgrade: websocket',
      'Connection: Upgrade',
      `Sec-WebSocket-Accept: ${wsAccept(key)}`,
      '',
      '',
    ].join('\r\n'),
  )

  const script = [
    { kind: 'stage_start', stage: 'extract', message: '开始提取文本', progress: 0, severity: 'info' },
    { kind: 'progress', stage: 'extract', message: '已解析 120/300 个资源', progress: 0.4, severity: 'info' },
    { kind: 'stage_end', stage: 'extract', message: '提取完成，共 1000 条', progress: 1, severity: 'info' },
    { kind: 'stage_start', stage: 'translate', message: '调用 qwen2.5:7b', progress: 0, severity: 'info' },
    { kind: 'log', stage: 'translate', message: '模型已加载', progress: 0, severity: 'debug' },
    { kind: 'progress', stage: 'translate', message: '占位符不一致: uid=t2', progress: 0.5, severity: 'warning' },
    { kind: 'stage_error', stage: 'fonts', message: '字体缺字 214 个', progress: 0.8, severity: 'error' },
  ]

  let index = 0
  const timer = setInterval(() => {
    if (index >= script.length || socket.destroyed) {
      clearInterval(timer)
      return
    }
    const event = { ...script[index], data: null, ts: new Date().toISOString() }
    index += 1
    socket.write(wsFrame(JSON.stringify(event)))
  }, 350)

  socket.on('close', () => clearInterval(timer))
  socket.on('error', () => clearInterval(timer))
}

const server = createServer(async (req, res) => {
  const url = new URL(req.url ?? '/', `http://127.0.0.1:${port}`)
  const key = `${req.method} ${url.pathname}`

  if (key in routes) {
    const payload = routes[key]()
    if (Buffer.isBuffer(payload)) {
      res.writeHead(200, { 'content-type': 'image/png' })
      res.end(payload)
      return
    }
    res.writeHead(200, { 'content-type': 'application/json; charset=utf-8' })
    res.end(JSON.stringify(payload))
    return
  }

  const safe = normalize(url.pathname).replace(/^([/\\])+/, '')
  if (safe && (await serveFile(join(dist, safe), res))) return
  if (await serveFile(join(dist, 'index.html'), res)) return

  res.writeHead(404, { 'content-type': 'text/plain; charset=utf-8' })
  res.end('not found')
})

server.on('upgrade', (req, socket) => {
  const match = /^\/ws\/jobs\/([^/?]+)/.exec(req.url ?? '')
  if (!match) {
    socket.destroy()
    return
  }
  handleUpgrade(req, socket, decodeURIComponent(match[1]))
})

server.listen(port, '127.0.0.1', () => {
  console.log(`mock-serve listening on http://127.0.0.1:${port}`)
})
