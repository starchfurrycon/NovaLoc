# NovaLoc / 新译 — 前端 (web)

NovaLoc 的桌面端前端：Vite + React 19 + TypeScript(strict) + Tailwind 4 + Arwes + lucide-react。
轻科幻技术风格（深色底 + 青绿强调色 + 等宽字体），构建产物 `src/novaloc/web_dist/` 会被 FastAPI 后端直接静态托管。

---

## 环境要求

| 依赖 | 版本 |
| --- | --- |
| Node.js | ≥ 20.19（开发机验证于 v25.1.0） |
| pnpm | ≥ 9（开发机验证于 10.4.1） |

`motion` 必须锁在 **v10**：Arwes 依赖 `motion` 导出的 `glide`，v11+ 已移除该导出。
`@arwes/react` 的 peer 声明为 React 18，本项目实际使用 React 19，因此安装时必须开启 legacy peer deps。

## 安装

```powershell
cd web
pnpm install --legacy-peer-deps
```

> pnpm 10 已移除 `--legacy-peer-deps` CLI 开关，因此仓库内提供了 `web/.npmrc`（内容 `legacy-peer-deps=true`），
> 直接执行 `pnpm install` 即可；上面的写法在 pnpm 9 与新版本下都能工作。
> 若在 pnpm 10 上被拒绝，使用等价形式：`pnpm install --config.legacy-peer-deps=true`。

## 开发

```powershell
cd web
pnpm run dev
```

- 默认地址 <http://127.0.0.1:5173>。
- `vite.config.ts` 已配置开发代理：`/api` 与 `/ws` 转发到 `http://127.0.0.1:8000`（后端默认端口）。
- 后端跑在别的地址时，复制 `.env.example` 为 `.env.local` 并设置 `VITE_API_BASE` / `VITE_WS_BASE`。

## 构建

```powershell
cd web
pnpm run build      # tsc -b && vite build  → src/novaloc/web_dist/
pnpm run typecheck  # 仅做类型检查
pnpm run preview    # 本地预览构建产物
```

构建产物：`src/novaloc/web_dist/index.html`、`src/novaloc/web_dist/assets/*.js`、`src/novaloc/web_dist/assets/*.css`。

**构建产物是刻意提交进仓库的**，这样没有 Node 工具链的机器也能直接运行工具；
`web/node_modules/` 仍被忽略。

产物的位置由 `vite.config.ts` 的 `build.outDir` 决定，指向
**`../src/novaloc/web_dist`** —— 也就是**放进 Python 包内部**。
这不是随意选的：`pyproject.toml` 里 hatchling 只打包 `src/novaloc`，
产物放仓库根的话 **wheel 里会没有前端**，`pip install nova-loc`
装出来界面 404 且构建时毫无警告。改这个路径前请先确认打包仍然生效。

## 路由

使用 `HashRouter`，因此静态托管无需服务端重写规则，后端把 `novaloc/web_dist` 挂到任意路径都能工作。

| 路由 | 页面 | 主要接口 |
| --- | --- | --- |
| `#/` | 项目 | `GET/POST /api/projects`、`DELETE /api/projects/{id}` |
| `#/scan/:id` | 扫描 | `POST .../detect`、`.../extract`、`.../translate`、`.../images`、`.../fonts`、`.../run` |
| `#/text/:id` | 文本审校 | `GET/PATCH /api/projects/{id}/text` |
| `#/images/:id` | 贴图审校 | `GET/PATCH /api/projects/{id}/images`、`.../images/{uid}/annotated` |
| `#/fonts/:id` | 字体 | `GET /api/projects/{id}/fonts` |
| `#/jobs/:id` | 任务进度 | `WS /ws/jobs/{id}`、`GET /api/jobs/{id}` |
| `#/settings` | 设置 | `GET/PUT /api/settings` |
| `#/qa/:id` | 质检报告 | `GET /api/projects/{id}/qa` |

## 源码结构

```
web/
├─ index.html
├─ vite.config.ts          # base:"./"，outDir:"dist"，dev 代理
├─ tsconfig.json           # project references
├─ tsconfig.app.json       # strict + noUnusedLocals
├─ tsconfig.node.json
├─ scripts/mock-serve.mjs  # 仅开发用：托管 dist + 模拟 API/WS，便于离线验证
└─ src/
   ├─ main.tsx             # HashRouter 挂载
   ├─ api.ts               # 类型化 API 客户端（唯一网络出口）
   ├─ useJobSocket.ts      # 任务事件 WebSocket（自动重连 + 轮询兜底）
   ├─ hooks.ts             # useResource / useAction / useToasts
   ├─ ui.ts                # 工具函数与设计令牌
   ├─ styles.css           # Tailwind 4 + @theme 令牌
   ├─ components/          # AppFrame、UI 组件库、虚拟列表、日志流
   └─ pages/               # 8 个路由页面
```

### 关键实现说明

- **`src/api.ts`** 是唯一网络出口，类型与后端冻结契约一一对应；`VITE_API_BASE` 为空时使用同源，
  `apiUrl()` 会把 `/api/...` 解析为绝对地址（贴图 `<img src>` 需要）。
- **`src/useJobSocket.ts`** 订阅 `ws://<host>/ws/jobs/{job_id}`，按 `kind ∈ log|progress|stage_start|stage_end|stage_error`
  累积事件，指数退避重连（上限 8s）；通道断开期间自动回退到 `GET /api/jobs/{job_id}` 轮询。
- **文本审校** 默认只请求 `limit=300` 条并做窗口化渲染，滚动到底部自动扩量；筛选/搜索参数会一并作为查询串发送，
  后端可安全忽略未知参数。修改未保存时关闭页面会触发浏览器离开确认。
- **写回顺序**：先「文本审校 / 贴图审校」定稿，再在「扫描」页运行 `.../apply`（`POST /api/projects/{id}/apply`）。
- 自定义音效（Arwes `BleepsProvider` 必需的 `bleeps` prop）在运行时用 Web Audio 波形合成，
  不引入任何音频素材文件；请勿删除该 prop，否则 Arwes 会抛错。

## 离线自检（可选）

不启动后端也能验证构建产物是否可用：

```powershell
cd web
pnpm run build
node scripts/mock-serve.mjs 5599   # http://127.0.0.1:5599 托管 dist 并模拟 /api 与 /ws/jobs/{id}
```
