# Switch-codex · Codex 调度站

OpenAI **Responses** 风格的多上游 API 路由代理 + 管理 UI，并按 DeepSeek **官方 setup 脚本**同步 Codex 本地配置。

中文名「Codex 调度站」：请求像列车一样进站，按模型池调度到不同上游轨道，失败自动换轨（failover）。

## 行为

| 使用模型 | 路由 | Codex `config.toml` / `models.json` |
|----------|------|--------------------------------------|
| `openai-all` | 只走该池上游；**透传** body.model | **自动配置**走 4100：`[model_providers.simple]` + `auth.json`，保留用户模型与自定义段 |
| `deepseek-v4-flash` 等 | 只走该池上游；**不 rewrite** | **官方同款**：写 model / reasoning / catalog，删冲突 key，写入官方 `models.json`，自动补 provider + `auth.json` |
| `本机原配置` | 不经 4100（Codex 直连原 provider） | **恢复介入前快照**：`config.toml` / `models.json` / `auth.json` 原样还原 |
| 其它自定义池 | 仅路由 | 不改 Codex |

> 默认开箱配置中的 `openai-all` 池与 `gpt-5.6-luna / terra / sol` 客户端模型只是示例，
> 便于首次启动就能跑通路由与可用性看板。实际使用时请按自己的上游和模型名修改：
> 模型池、上游 Base URL / API Key、模型映射与单价都在管理后台配置。

官方 TARGET keys：`model`、`model_provider`（本路由固定为 `simple` 以便走 4100）、`preferred_auth_method`、`forced_login_method`、`model_reasoning_effort=high`、`model_catalog_json`。
官方 DEL_B 等冲突项（`model_context_window`、`plan_mode_reasoning_effort`、`xhigh` 等）在 DeepSeek 模式下删除。

Key 仍在上游配置里，**不会**写入 `experimental_bearer_token`。
首次切换 openai-all / DeepSeek 时自动写入 `~/.codex/auth.json`（`OPENAI_API_KEY = SQLite 中的 master_key`），无需手动配置。
`data/deepseek-models.json` 缺失时会自动从官方 `codex-deepseek-setup.sh`（cdn.deepseek.com）下载。

## 启动

```bash
./scripts/start.sh
```

- UI: http://127.0.0.1:4100/ （前端使用路径路由（`/logs`、`/settings/pricing` 等），刷新后停留在当前页面）
- 管理 UI 登录：初始默认密码 `admin123`，**首次登录后必须修改**（修改后存于 `data/auth.json`，PBKDF2 加盐哈希）；这仅用于本地开箱试用，生产部署请务必修改
- 客户端 Key（Codex 走 `/v1/responses` 用）：`sk-switch-codex`（旧安装仍为 `sk-switchyard` / `sk-local-router`），存于 `data/switchyard.db` 的 settings.master_key
- API: `POST http://127.0.0.1:4100/v1/responses`

### 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `SW_HOST` | `127.0.0.1` | uvicorn 监听地址 |
| `SW_PORT` | `4100` | 监听端口（`SR_PORT` 兼容旧脚本） |
| `SW_BASE_URL` | `http://127.0.0.1:4100/v1` | 写入 Codex `config.toml` 的 providers base_url |

## Codex

首次在 UI 选择 `openai-all` 或 `DeepSeek` 时，项目会自动把 Codex 配置成走本路由：
写入 `model_provider = "simple"`、`[model_providers.simple]`（`base_url = http://127.0.0.1:4100/v1`、`wire_api = responses`、`requires_openai_auth = true`）以及 `~/.codex/auth.json`；DeepSeek 还会按官方规则执行 TARGET/DEL keys 并写入官方 `models.json`。全新安装的 Codex 也能直接用。

选择「本机原配置」则恢复项目介入前的快照（首次启动时自动生成），Codex 直连原 provider，不经 4100。

切换模型后请 **新开 Codex 会话**。

## Claude Code

路由提供 Anthropic 兼容端点 `/v1/messages`（与 `/v1/responses` 共用同一套模型池、
倍率、failover 与日志）。UI 的「Claude Code 配置」支持三种模式：

- **本机原配置**：恢复项目介入前的 `~/.claude/settings.json` 快照，Claude Code 直连原 provider；
- **openai-all**：把 `ANTHROPIC_BASE_URL` 指向本路由，模型走 `openai-all` 池；
- **DeepSeek slug**：同上，但模型走对应 DeepSeek 池。
- **auto-mode-bridge 集成**：应用 `openai-all` / DeepSeek 模式时自动安装本地 PreToolUse hook（`~/.claude/auto-mode-bridge/classifier.py`），替代 Anthropic 服务端 auto 分类器（DeepSeek 后端没有服务端分类器）。规则文件为 `~/.claude/auto-mode-bridge/rules.json`，仅在首次安装时从 `sy/bridge/` 拷贝，之后**永不覆盖用户修改**。选择「本机原配置」还原时，hook 注册随快照恢复一并移除（bridge 目录保留）；也可在 UI 手动开关（Claude Code 卡片里的勾选框）。修改后需 **新开 Claude Code 会话** 生效。
- **fail-open 策略**：hook 三层判定 fail-open——deny 规则 → allow 规则 → LLM 兜底（LLM 兜底自动走本路由 `/v1/messages`，低 effort 分类）；兜底遇 5xx 或超时一律放行。

上游可在「支持 Anthropic 原生 /messages」开关开启后，把 Claude Code 请求以
Anthropic 格式直接透传（零转换）；未开启的上游走 Anthropic → Responses 转换层，
再把响应转回 Anthropic 格式（支持流式与非流式，含 chat/completions 上游回退）。
切换配置后请 **新开 Claude Code 会话**。

注意：`~/.claude/settings.json` 由本路由管理；如果同时使用其它会改写该文件的工具
（例如 cc-switch 的本地代理接管），请只保留一个写入方，避免配置互相覆盖。

## Grok

路由同样服务 Grok CLI（grok-build）的 OpenAI **Responses** 请求（与 `/v1/responses`
共用同一套模型池、倍率、failover 与日志）。UI 的「Grok 配置」支持两种模式：

- **本机原配置**：恢复项目介入前的 `~/.grok/config.toml` 快照，Grok CLI 直连原 provider；
- **grok 池**：在 `~/.grok/config.toml` 写入受管模型段 `[model."switchyard"]`（`base_url`
  指向本路由、`api_backend = "responses"`、`api_key` 为路由 master key），并把
  `[models].default` 切到该段。受管段的 `model` 为 grok 池下的客户端模型
  （默认 `grok-4.6`），其它配置（用户自定义模型、mcp_servers、ui、marketplace 等）原样保留。

grok 池的初始上游在服务启动时自动从 `~/.grok/config.toml` 中现有的自定义 grok 端点
seed 一次（settings 键 `grok_pool_v1` 门控）；没有可用端点时会跳过并在下次启动重试，
也可在管理后台手动添加 `grok` 池上游。切换配置后请 **新开 Grok 会话**。

注意：`~/.grok/config.toml` 由本路由管理受管段与 `[models].default`；如果同时使用其它
会改写该文件的工具，请只保留一个写入方，避免配置互相覆盖。

## 代码结构

后端拆成 `sy/` 包，前端拆成独立 JS 模块，不再有数千行单文件：

```
app.py                  # FastAPI 入口：组装路由、静态文件、后台任务
sy/
  const.py              # 品牌常量与默认值
  db.py                 # SQLite 存储层（switchyard.db，自动迁移旧 simple_router.db）
  codex_sync.py         # Codex 官方同款 apply / restore
  core.py               # 配置、路由池、上游归一化、计费
  logbook.py            # 请求/错误日志、统计、历史可用性时间线
  probes.py             # 模型级联探测 + NewAPI 倍率探测
  auth.py               # 管理登录会话与客户端 key
  proxy.py              # /v1/responses、/v1/messages 透传/转换与 failover
  anthropic.py          # Anthropic Messages ↔ Responses 转换（含流式）
  claude_sync.py        # Claude Code settings.json 快照/恢复
  grok_sync.py          # Grok CLI config.toml 受管段/快照/恢复
  migrate_grok.py       # 一次性 seed grok 池上游（读 ~/.grok/config.toml）
  bridge/               # vendored claude-auto-mode-bridge（MIT，classifier.py + rules.json + LICENSE，纯拷贝不改）
  api.py                # 管理 API 路由
static/
  index.html            # 页面壳子（侧边栏 + 顶栏 + hash 路由）
  css/switchyard.css    # 设计系统（信号控制室风格）
  js/                   # core / router / forms / overview / pages / app
```

## 文件

| 路径 | 说明 |
|------|------|
| `data/switchyard.db` | SQLite 主存储（WAL）：配置、上游、NewAPI 探测、认证、请求/错误日志、可用性历史 |
| `data/simple_router.db` | 旧版数据库；首次启动自动迁移到 switchyard.db，旧文件保留作备份 |
| `data/legacy-backup/` | 首次启动时自动归档的旧 JSON/JSONL 文件（一次性迁移） |
| `data/deepseek-models.json` | 官方 models 模板 |
| `data/codex-backup/` | DeepSeek 模式前的 Codex 备份 |
| `data/grok-backup/` | Grok 介入前快照与 restore-point 备份 |
| `logs/switchyard.tmux.log` | 服务运行日志 |

旧版本使用的 `config.json` / `upstreams.json` / `auth.json` /
`newapi_probes.json` / `request_logs.jsonl` / `error_logs.jsonl` /
`availability_history.jsonl` 会在首次启动时自动导入 SQLite 并移动到
`data/legacy-backup/<时间戳>/`，原数据不会被删除。

## API

| 方法 | 路径 | 说明 |
|------|------|------|
| PUT | `/api/active-model` | `{"active_model":"..."}` 切换池 + 同步 Codex |
| GET | `/api/models` | 模型池 + codex_sync 类型 |
| GET | `/api/config` | 含 codex 状态 |
| PUT | `/api/grok/config` | `{"mode":"local-direct"\|"grok","model":"grok"}` 切换 Grok CLI 配置 |
| GET | `/api/logs/models` | 实际调用模型列表及请求次数（来自 `client_model`） |
| GET | `/api/logs` | 请求日志（`limit/offset/range/start/end/pool/model/status/q`；`range` 支持 `today`/`yesterday`/`3d`/`7d`/`30d`/`custom`，自定义时传 `start`/`end`，status 支持 `success`/`error`/数字） |
| GET | `/api/logs/stats` | 请求统计（支持 `range` 的 `today`/`yesterday`/`3d`/`7d`/`30d` 及 `pool/model` 筛选；含 `per_model`、`model_breakdown`、`per_pool`、`upstream_breakdown`） |
| DELETE | `/api/logs` | 清空请求日志 |
| GET | `/api/errors` | 错误日志（`limit/offset/range/start/end/pool/model/q`，时间范围同 `/api/logs`；每条含状态、每个上游的尝试结果与是否切换、请求体大小；保留 24h） |
| GET | `/api/errors/{id}` | 错误详情（含完整请求体与每次尝试的完整错误响应） |
| DELETE | `/api/errors` | 清空错误日志 |
| GET | `/api/pricing` | 各模型池单价（USD/1M tokens） |
| PUT | `/api/pricing` | 更新单价：`{"pricing":{"deepseek-v4-flash":{"input_per_m":0.28,"cache_read_per_m":0.028,"output_per_m":0.42}}}` |
| GET | `/api/newapi-probes` | NewAPI 探测任务列表（Token 脱敏 + 最近状态 + 可选上游名） |
| POST | `/api/newapi-probes` | 新增探测任务 |
| PUT | `/api/newapi-probes/{id}` | 修改探测任务（间隔 / 分组 / 上游 / Token 等） |
| DELETE | `/api/newapi-probes/{id}` | 删除探测任务 |
| POST | `/api/newapi-probes/run` | 手动触发所有启用任务 |
| POST | `/api/newapi-probes/{id}/run` | 手动触发单个任务 |

## 公网调用

默认仅监听本机地址。如需通过公网访问 `/v1/responses`，请在管理后台「设置 → 公网调用」中
启用公网开关并配置公网地址、IP 黑白名单。

默认**不信任** `X-Forwarded-For` / `cf-connecting-ip` 等代理头，客户端 IP 直接取 socket 对端。
只有当你把 Switch-codex 放在受信反向代理（如 Cloudflare 隧道）后面时，才应勾选
「信任代理头」，否则外部请求可以伪造这些头绕过 IP 限制。
