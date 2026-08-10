# Switchyard Claude Code 本地路由修复报告（对照 cc-switch）

> 日期：2026-08-10  
> 对照对象：`/workspace/cc-switch`（commit `0345fad`，MIT License）  
> 审查对象：`/workspace/switchyard` 当前未提交改动（`/v1/messages`、`claude_sync.py`、`anthropic.py`、日志/用量统计改动）

## 0. 结论摘要

1. cc-switch 已经实现了一整套成熟、经过大量单测打磨的“Claude Code 本地路由”：本地代理 15721、配置接管/还原、Anthropic ↔ Responses/Chat/Gemini 转换、流式转换、故障转移、熔断器、用量解析、请求整流。它正好覆盖 Switchyard 这次手搓 `/v1/messages` 想做的全部事情，而且做得更完整。
2. **能借的就不要自己造**：Anthropic 协议转换、流式状态机、usage 解析、熔断器、错误信封、`anthropic-version` 处理、stream 请求收到 JSON 的兜底，全部可以从 cc-switch 直接移植（MIT 许可，保留版权声明），或者干脆把 cc-switch 用作 Claude Code 的本地路由层。
3. **不能借的才自己研究**：Switchyard 的按模型池多上游路由、model_map/倍率/优先级、级联探测、定价成本、请求看板、NewAPI 比例探测、公网访问控制、Codex 配置同步，cc-switch 没有或不适配，这部分保留在 Switchyard 自研。
4. 如果继续在 Switchyard 里保留 `/v1/messages`，当前有 5 个必须修的 P0/P1 问题；其中 4 个在 cc-switch 里已有现成答案，照抄语义即可。

> **决策（2026-08-10）**：不直接使用 cc-switch，采用**方案 B——把 cc-switch 的成熟实现/语义借鉴移植到 Switchyard**。桌面方案 A 仅作为以后本机使用的备选，不作为本次改造路径。

### 本次已落地的改动（2026-08-10）

- P0-1：`/v1/messages` 的 chat/completions 上游补上 Chat → Responses → Anthropic 双段转换（流式/非流式），不再把 chat 响应当 Responses 解析。
- P0-2：`anthropic-version` 大小写不敏感收集、passthrough 透传、缺失补 `2023-06-01`。
- P0-3：`_extract_usage` 改为“最早 input/cache + 最后 output”合并；`_usage_numbers` 把 cache_read 与 cache_creation 都加回总输入。
- P1-4：Anthropic thinking/output_config/reasoning_effort → Responses `reasoning.effort` 映射（含 openai-all 映射后补映射）。
- P1-5：非透传上游 4xx 统一包成 Anthropic 错误信封。
- P1-6：`stream=true` 但上游回 JSON 时，合成完整 Anthropic SSE 下发。
- P2-7 ~ P2-10、P3-11 ~ P3-15：model 一致性、mode 400 校验、JS 注册位置、reasoning 双计、死字段、escapeHtml、endpoint 一致性、`_parse_usage` 兜底、README 均已处理。
- 请求日志新增 `session_id`（Claude Code 的 `x-claude-code-session-id` / metadata.session_id，Responses 客户端的 session_id 头），同一会话稳定不变；日志表把“上游”与“端口”合并为一列（上游名称 + 端口徽标）。

未做（后续可选项）：熔断器状态机、thinking signature 整流、模型级 media 整流——这些属于增强，不阻塞 Claude Code 基本可用。

## 1. cc-switch 本地路由能力与 Switchyard 现状对照

| 能力 | cc-switch 实现 | Switchyard 现状 | 结论 |
|---|---|---|---|
| 本地代理服务 + 客户端配置接管/还原 | `services/proxy.rs`、`docs/user-manual/en/4-proxy/4.1-service.md`、`4.2-routing.md` | `claude_sync.py` 直写 `~/.claude/settings.json` 指向 4100，有一次性快照和恢复 | **借鉴**：app 级接管状态机、热切换、异常退出后的配置还原比 Switchyard 的“手动点恢复”更稳 |
| Provider 队列 + 故障转移 | `proxy/provider_router.rs`、`database/dao/failover.rs`、`docs/4.3-failover.md` | 按模型池排序 + failover 状态码 + 重级联，但没有持久化 failover 队列和“当前 provider”显式概念 | **借鉴语义**：队列是 provider 级，Switchyard 是 upstream 级，模型不完全一样，但“队列 + 显式当前项”值得吸收 |
| 熔断器 | `proxy/circuit_breaker.rs`（closed/open/half-open、失败阈值、错误率、恢复成功数、等待时间、热更新、half-open permit） | 只有 availability 缓存 + 级联探测，没有熔断状态机 | **借鉴**：可直接移植到 Python，或接入 cc-switch |
| Anthropic ↔ Responses 非流式转换 | `proxy/providers/transform_responses.rs`、`transform_codex_anthropic.rs` | `anthropic.py` 简化版：缺 thinking 映射、图像/document、cache_control 剥离、历史规范化、failed 状态校验 | **借鉴**：直接移植或使用 cc-switch |
| Anthropic ↔ Responses 流式转换 | `proxy/providers/streaming_responses.rs`、`streaming_codex_anthropic.rs` | `ResponsesSseToAnthropic` 简化版：缺 JSON 兜底、late delta 防护、thinking/signature、usage 合并 | **借鉴**：直接移植或使用 cc-switch |
| usage/token 解析 | `proxy/usage/parser.rs`：`TokenUsage` 分开存 input/cache_read/cache_creation，流式合并 message_start + message_delta，带 delta 修正启发式，`has_billable_tokens` 过滤空行 | `logbook._extract_usage` 取“最后一个 usage”，Anthropic 流式下丢 input/cache；`_usage_numbers` 缓存语义错误 | **借鉴**：照抄 cc-switch 的三桶模型 |
| 错误映射与错误信封 | `proxy/error_mapper.rs`、`handlers.rs`（上游错误透传状态码、超时 504、转发失败 502、无可用/全熔断 503、转换错误 422） | 非 failover 4xx 直接把 OpenAI 风格错误文本透传给 Claude Code | **借鉴**：统一错误信封 |
| 请求头处理 | `proxy/forwarder.rs`：`anthropic-version` 大小写不敏感透传 + 缺省 `2023-06-01`；认证头替换；hop-by-hop 头清理；Accept 规范化 | `_forward_once` 读 `extra_headers.get("anthropic-version")` 但 `extra` 根本没收集它 | **借鉴**：抄 forwarder 的规则即可 |
| stream=true 但上游返回 JSON | `streaming_responses.rs` 的 `responses_json_to_anthropic_sse` | 直接把 JSON 回给按 SSE 等待的 Claude Code | **借鉴**：抄这个兜底 |
| 请求整流 | `proxy/thinking_rectifier.rs`、`thinking_budget_rectifier.rs`、`media_sanitizer.rs`、`tool_media.rs`、`model_mapper.rs` | 无 | **按需借鉴**：先做 thinking/signature 整流，其余后续 |
| 模型测试/健康检查 | `docs/4.5-model-test.md`、`commands/stream_check.rs` | Switchyard 已有 probes/级联/availability | **保留自研**，可借鉴 stream check 的延迟/首包指标 |
| 请求日志/用量统计/看板 | `proxy/usage/logger.rs`、`docs/4.4-usage.md` | Switchyard 已有 logbook/pricing/dashboard | **保留自研**，只吸收 usage 归一化语义 |
| 配置备份/恢复 | `services/proxy.rs` + docs 4.1/4.2 | `claude_sync.py` 已有快照 + restore 备份点 | 已类似，可借鉴“接管/停用全流程”的状态机 |

## 2. 能借鉴的（不要自己造）

### 2.1 最省事：直接用 cc-switch 做 Claude Code 本地路由

架构改为：

```text
Claude Code
   │  ~/.claude/settings.json → ANTHROPIC_BASE_URL=http://127.0.0.1:15721
   ▼
cc-switch 本地代理（15721）
   │  按 Claude provider 队列路由、熔断、热切换、usage 统计
   ▼
Switchyard（http://127.0.0.1:4100/v1）
   │  openai_responses 上游，继续做模型池/多上游路由/倍率/定价
   ▼
真实上游
```

- 在 cc-switch 中把 Switchyard 添加为 Claude Code 的 provider：`api_format = openai_responses`、`base_url = http://127.0.0.1:4100/v1`、认证用 Switchyard master key。
- 这样 Anthropic ↔ Responses 转换、流式转换、熔断、failover、热切换全部由 cc-switch 承担，Switchyard **不需要 `/v1/messages`**，可以回退这批未提交改动中协议转换部分，只保留 `claude_sync` 里真正需要的快照思路。
- 代价：cc-switch 是 Tauri 桌面程序，需要本机桌面/托盘环境；无头服务器上不适合。

### 2.2 如果必须留在 Switchyard：移植 cc-switch 的关键模块

cc-switch 是 MIT License，移植时保留版权声明即可。建议按以下顺序移植：

1. **usage 三桶模型**（最高优先）：`TokenUsage { input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens }`；流式解析合并 `message_start`（input/cache）与 `message_delta`（output），并处理 delta 修正。参考 `proxy/usage/parser.rs`。
2. **Responses SSE → Anthropic SSE 流式转换器**：包含 JSON 兜底、late delta 忽略、message_start 兜底生成、thinking/signature、usage 合并。参考 `proxy/providers/streaming_responses.rs`。
3. **Anthropic → Responses 请求转换补全**：thinking 映射、`max_tokens` 上限、forced tool_choice 与 thinking 互斥、图像/document、历史规范化。参考 `proxy/providers/transform_responses.rs` 与 `transform_codex_anthropic.rs`。
4. **Chat Completions 链路补全**：Switchyard 的 `/v1/messages` 目前把 chat 上游当 Responses 解析，必须接入 `convert.ChatSseToResponses` / `chat_response_to_responses`。参考 cc-switch `streaming_codex_chat.rs` / `transform_codex_chat.rs`。
5. **请求头规则**：`anthropic-version` 大小写不敏感透传 + 缺省值；认证头替换；hop-by-hop 清理。参考 `proxy/forwarder.rs` 头部处理段。
6. **熔断器**：把 cc-switch 的 closed/open/half-open 状态机移植成 Python（或先在 Switchyard 现有 availability 缓存上叠加）。
7. **错误信封**：Claude 端统一返回 `{"type":"error","error":{...}}`，状态码按 error_mapper 语义。

## 3. 不能借鉴的（自己研究/保留）

- **按模型池的多上游路由**：cc-switch 是“一个 app 一个 provider 队列”，Switchyard 是“一个 model pool 多条上游，带 model_map、优先级、倍率、可用性缓存”。这部分没有现成轮子，保持自研。
- **级联探测与 availability 缓存**：cc-switch 的熔断是 per-provider 请求维度，Switchyard 的整五分钟级联探测、失败重级联是特有的，继续自研。
- **定价/成本/看板**：Switchyard 的 multiplier、USD/CNY、历史可用性色块是差异化能力，保留。
- **NewAPI 比例探测**、**公网访问/IP 黑白名单**、**Codex 配置同步**：Switchyard 独有，保留。
- **两套 usage 统计的唯一事实源**：cc-switch 和 Switchyard 都会记日志，需要自己定主从关系或合并规则。
- **无头场景**：cc-switch 需要桌面/托盘，容器里不能直接跑，需要研究虚拟显示或 headless 方案；如果确认无头是硬需求，就必须走移植路线。

## 4. 当前 Switchyard `/v1/messages` 问题清单（保留自研时必修）

| # | 严重度 | 问题 | Switchyard 位置 | cc-switch 参考 | 修法 |
|---|---|---|---|---|---|
| 1 | P0 | `chat_completions` 上游：请求转 chat 后响应仍按 Responses 解析，流式/非流式都会空/错 | `sy/proxy.py:122-130`、`sy/proxy.py:1163`、`sy/proxy.py:1296` | `streaming_codex_chat.rs`、`transform_codex_chat.rs` | 要么禁止 chat 上游参与 `/v1/messages`，要么接 Chat→Responses 转换后再进 Anthropic 转换 |
| 2 | P0 | `anthropic-version` 没有透传：`extra` 只收 accept/openai-beta，且读取大小写敏感 | `sy/proxy.py:809`、`sy/proxy.py:142` | `forwarder.rs` 头部处理（大小写不敏感 + 缺省 `2023-06-01`） | 收集并统一小写，缺省补 `2023-06-01` |
| 3 | P0 | Anthropic usage 统计错误：`_extract_usage` 取最后一条（Anthropic 流式最后只有 output），`_usage_numbers` 只把 cache_read 加回 input | `sy/logbook.py:88-92`、`sy/logbook.py:96-112` | `usage/parser.rs` 的 `from_claude_stream_events`、三桶模型 | 合并 message_start + message_delta；input 与 cache_read/cache_creation 分开存 |
| 4 | P1 | thinking/reasoning 配置在 Anthropic→Responses 转换里被丢弃 | `sy/anthropic.py:222-276`、`sy/proxy.py:887-899` | `transform_codex_anthropic.rs` 的 effort→budget 映射、`transform_responses.rs` | 映射 `thinking`/`reasoning_effort`，处理 max_tokens 上限和 forced tool_choice 互斥 |
| 5 | P1 | 非 failover 4xx 把 OpenAI 风格错误体透传给 Claude Code | `sy/proxy.py:1362-1387` | `error_mapper.rs`、Claude 错误信封 | 统一 `anthropic_error_response` |
| 6 | P1 | `stream=true` 但上游返回 JSON 时直接回 JSON | `sy/proxy.py:1276-1298` | `streaming_responses.rs` 的 `responses_json_to_anthropic_sse` | 把 JSON 合成完整 Anthropic SSE |
| 7 | P2 | 非流式 `model` 用原始请求体（openai-all），流式用映射后模型，不一致 | `sy/proxy.py:1296` vs `sy/proxy.py:1163` | `handlers.rs` 统一 fallback model | 统一传转换后模型 |
| 8 | P2 | 未知 `mode` 返回 500 而非 400 | `sy/core.py:933-944` | Pydantic `Literal` | 加枚举校验 |
| 9 | P2 | `btnApplyClaude` 注册在 `loadUpstreams` 循环里 | `static/js/pages.js:121-151` | 无 | 移到初始化区 |
| 10 | P2 | reasoning/thinking 双计数：`output_tokens + reasoning_tokens` | `static/js/overview.js:452`、`sy/logbook.py:366` | `usage/parser.rs` 的三桶模型 | 明确 output 是否含思考，统一口径 |
| 11 | P3 | `claude_sync` 字段复制 codex_sync 且未用 | `sy/api.py:223-227` | 无 | 删除或实现真实状态 |
| 12 | P3 | `textContent` 里套 `escapeHtml`，URL 含 `&` 会显示 `&amp;` | `static/js/pages.js:802-803` | 无 | 去掉 escapeHtml |
| 13 | P3 | `/v1/messages` 的 `record()` 有 `endpoint` 参数但从没传；`/v1/responses` 里 chat 转换的失败记录也没传 `endpoint="chat"`，错误日志会显示成 `response` | `sy/proxy.py:814-825`、`sy/proxy.py:502/553`（只覆盖成功路径） | 无 | 删除死参数，或成功/失败统一传 `endpoint` |
| 14 | P3 | `_parse_usage` 兜底取“前两个 int”，可能把 cache 字段误当 input/output | `sy/anthropic.py:110-113` | `usage/parser.rs` 只按已知 key 解析 | 按字段解析，去掉任意取值兜底 |
| 15 | P3 | README 没有 Claude Code / `/v1/messages` 使用说明 | `README.md` | `cc-switch/docs/user-manual/en/4-proxy/*` | 补充三种模式、端点用法和限制 |

## 5. 推荐落地路径

### 方案 A（最推荐，能借就不造）：cc-switch 做 Claude Code 本地路由，Switchyard 只做 Responses 上游

1. 在 cc-switch 添加 Switchyard provider（`api_format=openai_responses`，`base_url=http://127.0.0.1:4100/v1`，auth=master key）。
2. 启用 cc-switch 的 Claude 接管 + failover 队列 + 熔断，Claude Code 的配置由 cc-switch 写入/还原。
3. Switchyard 回退 `/v1/messages` 相关协议代码（`anthropic.py`、`proxy_anthropic_messages`、upstream 的 `anthropic_messages` 开关），保留 `claude_sync.py` 的快照思路作为参考，但不再由 Switchyard 直写 `~/.claude/settings.json`，避免两套写入冲突。
4. Switchyard 继续深耕模型池路由、定价、看板、公网控制。

适合：本机有桌面/托盘环境，希望 Claude Code 热切换、熔断、转换都白嫖 cc-switch 的成熟实现。

### 方案 B（无头/必须单进程）：把 cc-switch 关键模块移植到 Switchyard

按第 2.2 节顺序移植，先解决 P0 三项（usage、转换、头部），再补 thinking/错误信封/熔断。移植时保留 MIT 版权声明，并给 `anthropic.py` 补上 cc-switch 同级别的单元测试（cc-switch 的 `streaming_responses.rs` 自带大量测试可直接参考翻译）。

适合：服务器/容器无桌面环境，Switchyard 必须独立承担 Claude Code 入口。

### 方案 C（折中）：双入口，按部署环境选

- 桌面环境走方案 A；
- 无头环境走方案 B，但只移植到“能用 + 数据准确”的最小集（P0 三项 + 错误信封），熔断/整流后续再补。

## 6. 风险与待验证

1. **cc-switch 无头运行**：Tauri 需要窗口/托盘，容器里要 Xvfb/Wayland 或研究其 lightweight 模式是否能在无显示环境常驻；不行则方案 A 只在用户本机成立。
2. **两套日志/计费会重复**：cc-switch 与 Switchyard 都记 usage。需要确定主账本（建议 Switchyard 记账，cc-switch 只做转换/熔断），否则费用和 token 统计会翻倍。
3. **api_format 选择**：cc-switch 指向 Switchyard 时，选 `openai_responses` 比选 `anthropic` 更稳：Switchyard 只需要原生 `/v1/responses`，不用承担 Anthropic 转换质量风险。
4. **模型池语义**：cc-switch 的 provider 队列是 provider 级，Switchyard 的 pool/upstream 是模型级；如果两者叠加，需要在 Switchyard 侧保留“按客户端模型选 pool”的规则，不能把 cc-switch 的队列当成模型池。
5. **配置写入冲突**：如果同时启用 cc-switch 接管和 Switchyard `claude_sync`，两边都会写 `~/.claude/settings.json`，必须只保留一个写入方。

## 7. 验收清单

- [ ] Claude Code 流式/非流式请求在真实上游下通过；
- [ ] `chat_completions` 上游（如果保留）在 `/v1/messages` 下正确转换；
- [ ] `anthropic-version` 透传或补默认值；
- [ ] usage 日志：input/cache_read/cache_creation/output 四类分开且与上游账单一致；
- [ ] thinking/reasoning 在非 passthrough 上游生效；
- [ ] 4xx/5xx/转换错误都返回 Anthropic 错误信封；
- [ ] `stream=true` 但上游返回 JSON 时客户端仍收到合法 SSE；
- [ ] 熔断器有 closed/open/half-open 三态，且可配置、可手动重置；
- [ ] 关闭路由后 `~/.claude/settings.json` 能还原到介入前状态；
- [ ] 两套日志系统有明确主从关系，费用/token 不重复；
- [ ] README 已补充 Claude Code 路由说明；
- [ ] 保留 cc-switch MIT 版权声明（若移植代码）。
