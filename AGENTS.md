# GrokSearch

## 功能描述

GrokSearch 是一个基于 FastMCP 的 MCP 服务器，提供以下能力：

- `web_search`：通过 Grok 执行 AI 驱动的网络搜索
- `get_sources`：读取 `web_search` 缓存的信源
- `web_fetch`：通过 Tavily 提取网页内容，并在失败时回退到 Firecrawl
- `web_map`：通过 Tavily 生成站点地图
- `plan_*`：为复杂搜索提供分阶段结构化规划
- `get_config_info` / `switch_model` / `toggle_builtin_tools`：配置诊断、模型切换、Claude Code 工具路由控制

## 使用方法

### 本地开发

```bash
PYTHONPATH=src uv run python -m grok_search.server
```

### 测试

```bash
uv run --with pytest --with pytest-asyncio pytest -q
```

### 安装为 MCP

请参考 [README.md](./README.md) 与 [README.en.md](./README.en.md) 中的 `claude mcp add-json` 示例。

当前公开的 `stdio` 安装示例默认以维护中的发布仓库 `Boulea7/GrokSearchTool` 为准；本地开发工作区与历史远端命名不应被理解为仍按 `fork/upstream` PR 模式协作。

## 参数说明

### 核心环境变量

- `GROK_API_URL`：Grok/OpenAI-compatible API 地址
- `GROK_API_KEY`：Grok API 密钥
- `GROK_MODEL`：默认模型；优先级为进程环境变量 > 项目 `.env.local` > 项目 `.env` > `~/.config/grok-search/config.json` 持久化值 > 代码默认值（当前内建默认首选为 `grok-4.20-0309`）
- `GROK_MODEL_SOURCE`：`get_config_info` 基础配置快照里的活动模型来源层；当前可能是 `process_env`、`project_env_local`、`project_env`、`persisted_config` 或 `default`
- `GROK_TIME_CONTEXT_MODE`：时间上下文注入策略，支持 `always` / `auto` / `never`
- `TAVILY_API_KEY` / `TAVILY_API_URL`：Tavily 配置；用于 `web_fetch` / `web_map`，也用于 Tavily supplemental `web_search`
- `FIRECRAWL_API_KEY` / `FIRECRAWL_API_URL`：Firecrawl 配置；用于 `web_fetch` 托底，也可用于 supplemental `web_search`
- `GROK_RETRY_MAX_ATTEMPTS` / `GROK_RETRY_MULTIPLIER` / `GROK_RETRY_MAX_WAIT`：重试配置
- `GROK_OUTPUT_CLEANUP`：是否启用 `web_search` 输出清洗

## 返回值说明

### `web_search`

返回 `dict`：

- `session_id`：信源缓存 ID
- `content`：搜索结果正文，失败时返回可诊断错误文本
- `sources_count`：缓存的信源数量
- `status`：`ok` / `partial` / `error`
- `effective_params`：标准化后的生效搜索参数回显
- `warnings`：非致命警告列表；除补充搜索控制项未生效外，也可能包含 `body_missing_sources_only`、`body_probably_truncated`
- `error`：稳定的机器可读错误码；无错误时为 `null`

### `get_sources`

返回 `dict`：

- 成功时返回：`session_id`、`sources`、`sources_count`、`search_status`、`search_error`、`source_state`
- miss 时返回：`session_id`、`sources=[]`、`sources_count=0`
- `error`：仅在 `session_id` 缺失或过期时返回

## 文档政策

- 公开维护的路线图仅放在 [docs/ROADMAP.md](./docs/ROADMAP.md)
- 详细开发文档、研究笔记、内部决策、发布准备清单统一放在 `.local/docs/`
- `.local/` 必须保持在 `.gitignore` 中，不得提交到 GitHub
- `AGENTS.md` 只保留稳定操作上下文，不承担内部迭代计划与临时研究记录
- README 首屏应优先说明项目定位、推荐路径、能力分层与运行时边界，不保留时效性强的“效果展示”截图作为主内容

## 当前定位

- 核心产品是轻量 `MCP + companion skill`
- 默认能力以高频、短时、低摩擦的信息注入为主，目标是在大多数场景下尽快返回有用结果与可核验来源
- `web_search`、`get_sources`、`web_fetch`、`web_map`、`get_config_info` 是默认主能力
- `plan_*` 是默认的轻量规划入口，推荐作为复杂搜索与多数常规搜索的前置步骤；对明确单跳、低歧义且规划收益很低的查询，允许直接进入 `web_search`
- `web_search` 是默认搜索执行入口，既可直接调用，也可承接 `plan_*` 之后的搜索执行
- `plan_*` 的额外开销当前较低，适合作为高频、轻量的默认规划入口
- 更长时间、更强编排的复杂任务与深度探索能力统一收口为 `deep research` 框架，并与核心默认体验解耦
- 当前推荐分层是：`plan_* -> web_search` 负责轻量层，`deep research` 负责高级深度探索层
- 所有需要用户编辑、确认或阶段性干预的交互只放在 CLI；MCP 与 companion skill 只承载非交互式触发、状态查询和结果获取

## 稳定注意事项

- `GROK_API_URL` 应尽量使用 OpenAI-compatible 根路径并显式带上 `/v1`；代码层当前不会仅因缺少 `/v1` 就预先拦截请求，但多数 OpenAI-compatible 端点仍可能因此在运行时失败，并通常伴随兼容性 warning
- 配置读取当前应遵循：进程环境变量优先；若缺失，再回落到项目根目录的 `.env.local`，仍缺失时再看 `.env`。这里的“优先”按键是否存在判断：即使环境变量值为空字符串，也不会再回落到项目文件
- 当前运行时模型选择对 Grok 4.1+ 族应保持弹性：若显式或隐式请求的模型不在 `/models` 返回列表里，但存在兼容的 Grok 4.1+ 可用模型，则应优先回退到更合适的可用模型，而不是仅因后缀不同就直接失败
- 项目级环境变量回退当前同时支持普通 dotenv 形式的 `KEY=value` 与可选 `export KEY=value`
- `get_config_info` 当前可用于配置与连通性初检，并默认执行最小真实 `search/fetch` 探针；但还不是完整的端到端兼容性诊断
- `web_search` 当前支持轻量显式控制：`topic`、`time_range`、`include_domains`、`exclude_domains`；其中 `topic` 当前支持 `general` / `news` / `finance`，`time_range` 当前支持 `day` / `week` / `month` / `year`，并兼容 `d` / `w` / `m` / `y`
- `web_search` 的本地时间上下文注入当前受 `GROK_TIME_CONTEXT_MODE` 控制，默认 `always`
- `get_sources` 当前会统一返回标准化 metadata；`rank` 当前会按 `score`、来源身份清晰度与稳定去重顺序生成，不再对 Grok 引用额外偏置
- `standardize_sources` 当前会在去重时规范化 URL 的 scheme/host 大小写，因此同一页面的 mixed-case 变体可能折叠为单个 source；这会影响最终的 `sources_count` 与 `rank`
- `standardize_sources` 当前不会把显式默认端口（如 `:443` / `:80`）与隐式默认端口 URL 自动折叠；如需调整该语义，应先视为明确 contract change 并补回归测试与文档
- `get_sources` 当前依赖当前服务器进程内的内存型 LRU 缓存；默认 TTL 约 1 小时、当前上限 256 个 session。`session_id` 是 shared-daemon、transient、非 durable、非 caller-bound handle，也不应被理解为 secret token；`session_id_not_found_or_expired` 当前统一覆盖进程重启、TTL 到期、缓存淘汰与不可读旧缓存 miss
- `Config.get_config_info()` 只返回基础配置快照；MCP 工具 `get_config_info` 会保留该快照，并新增 `connection_test`、`doctor`、`feature_readiness` 与最小真实探针结果；当前还支持 additive `detail=full|summary` 分级输出，默认仍为 `full`
- `detail=summary` 当前只是同一次诊断结果的紧凑字段投影，不是额外的轻执行路径
- `detail=summary` 当前应保留 `Config.get_config_info()` 返回的全部基础配置快照键；新增基础字段时，不应只出现在 `full`
- planning `session_id` 当前是进程内的 transient handle，默认 TTL 约 1 小时、LRU 上限 256；若进程重启、TTL 到期或缓存淘汰，应从新的 `plan_intent` 重新开始
- `plan_*` wrapper 当前刻意保持标量 shim 形态，例如 CSV `depends_on`、分号分组的 `parallel_groups`、字符串化 JSON 的 `params_json`；`executable_plan` 返回的是规范化后的结构化形状
- planning engine 当前会先规范化传入的 `id` / `sub_query_id` 再做 duplicate guard，避免空白包裹的重复 ID 绕过校验
- `plan_search_term` 当前在非 `is_revision` 追加时只会累积 `search_terms`，不会隐式改写既有 `approach` / `fallback_plan`；若要替换整组 search strategy，应显式使用 `is_revision=true`
- 首次建立 `search_strategy` 时必须提供 `approach`；只有 strategy 已建立后，后续非 `is_revision` 调用才允许只追加 `search_terms`
- `connection_test` 当前只反映 `/models` 连通性；真实运行时可用性应结合 `doctor` 与 `feature_readiness` 判断
- `grok_model_selection` 当前表示 `/models` 可见性阶段就已确认当前模型不适合直接使用，并会在真实请求前预选更合适的 Grok 候选模型；`grok_model_runtime_fallback` 则表示当前 probe model 在真实 `chat/completions` 路径上仍需在运行时二次回退才成功；这两个 check 可能同时出现
- `web_search` 当前在“只返回信源列表、没有正文”或“正文疑似截断”时也会降级为 `partial`；`get_sources.search_status` 会保留该降级状态，但 `get_sources` 当前仍不会回放原始 `warnings`
- `grok_search_probe` 当前除 `ok` / `error` 外，也可能因为正文质量降级返回 `warning`；例如只拿到信源列表或正文疑似截断时，`feature_readiness.web_search` 应显示为 `degraded`
- 当前运行时模型回退属于 best-effort 兼容路径：依赖 `/models` 返回候选列表，且上游错误摘要命中“模型不可用”类文案；若 `/models` 本身不可用，或错误类型不匹配，则不保证自动继续回退
- 当前诊断 `web_search degraded` 时，`GROK_MODEL_SOURCE` 应视为根因分析的一等信息；若活动模型来自进程 env 或项目 `.env.local` / `.env` 覆盖，则与持久化配置层造成的 mismatch 是不同问题
- `feature_readiness.get_sources` 当前只有在运行中进程里至少存在一个非 error 的可读取 source session 时才会显示 `ready`；若缓存里只有失败搜索留下的 session，则应保持 `partial_ready`
- `feature_readiness.get_sources` 属于 `transient` readiness 信号；当前不应仅因其为 `partial_ready` 就拉低 overall doctor
- `doctor` 当前会保留字符串版 `recommendations`，并额外提供结构化 `recommendations_detail`
- 若 `GROK_MODEL_SOURCE` 是 `process_env`、`project_env_local` 或 `project_env`，单独调用 `switch_model` 不会改变当前进程；应先修改或删除对应覆盖层
- 即使 API Key 已脱敏，`get_config_info` / `doctor` 输出当前仍可能包含本机绝对路径、endpoint/hostname 与精简后的上游错误摘要；对外分享前应先复核
- `feature_readiness.web_fetch` 当前会附带 provider 级细节，并在 `verified_path` 中标注真实抓取探针实际打通的后端；未执行的 provider 可能带有 `skipped_reason`
- `feature_readiness.web_fetch` 当前应优先尊重真实 `web_fetch_probe` 的结果；即使单点 provider 探测通过，真实抓取探针失败时也应保持 `degraded`
- `GROK_DEBUG=false` 时，`log_info()` 当前不会写这类 helper progress log，也不会通过 `ctx.info()` 对外转发中间进度；这些信号当前是 debug-only progress/debug signal
- redirect preflight 因 timeout 或请求级错误被标记为 `skipped_due_to_error` 时，当前还会通过 MCP context 发出 caller-visible warning，但不会改写成功返回体
- `web_fetch` 目前优先使用 Tavily extract，失败时回退到 Firecrawl scrape
- Tavily supplemental search 当前会把 `max_results` 限制在 Tavily 文档给出的上限 `20`
- `web_fetch` / `web_map` / Tavily 补充搜索当前只暴露 provider 能力的受控子集，不等同于 Tavily / Firecrawl 的全量原生 API
- `web_map` 当前可能返回外部域名链接；若需要更接近站内 sitemap 的结果，应在调用方进一步收敛或过滤。这一表现当前对应 Tavily 文档中的默认 `allow_external=true`，且本封装暂未直接暴露该开关
- 若上游 endpoint 指向 loopback，本地请求当前会强制 `trust_env=False`，因此也会绕过 `HTTP_PROXY` / `HTTPS_PROXY` / `ALL_PROXY` / `NO_PROXY` 与 `SSL_CERT_FILE` / `SSL_CERT_DIR`
- `web_fetch` / `web_map` 当前会默认拒绝非 `http/https`、loopback、明显私有网络目标、单标签主机名、常见私网后缀主机（如 `.internal` / `.local` / `.lan` / `.home` / `.corp`）、常见 loopback helper 域名（如 `localtest.me` / `lvh.me`），以及常见把本地/私网 IP 编进公网 DNS 名的 alias 形态（如 `nip.io` / `xip.io` / `sslip.io`），避免工具被误用成内网抓取入口
- 对通过静态 URL 边界检查的目标，`web_fetch` / `web_map` 当前还会在真正调用 provider 前继续复检可见的 redirect 目标
- 当前可见 redirect 复检使用 `GET` 而不是 `HEAD`；对 presigned URL、one-shot token 或读取本身可能有副作用的链接，可能存在额外一次预检读取，这属于当前已知边界
- 当前可见 redirect 复检最多会发起 `5` 次预检请求；如果到第 `5` 次预检时仍然看到新的可见重定向，就会以“目标 URL 重定向次数过多”硬拒绝，不再继续调用下游 provider
- 若 redirect 预检发生超时或请求级错误，当前实现会将该步骤标记为 `skipped_due_to_error`；`web_fetch` / `web_map` 目前仍会继续调用下游 provider
- 当前这层边界不会仅因为本机 DNS 把某个公网 hostname 解析到私网就直接拒绝请求，因此不应被理解为对 split-horizon / 本地 DNS 私有解析的强保证
- `split_answer_and_sources` / `standardize_sources` 当前会尽量避免把 generic 尾部链接列表误拆成真实信源，并会对明显敏感的 query 签名参数、常见 OAuth/OIDC credential 参数，以及高置信度 cloud-signed credential 键（如 `X-Amz-Credential` / `X-Goog-Credential` / `GoogleAccessId`）做最小遮罩
- `standardize_sources` 当前会保留普通锚点（fragment）以避免不同页面段落引用被误合并；但 URL `userinfo`、常见签名参数以及 `client_secret` / `refresh_token` / `id_token` / `password` 这类常见 credential 参数会被遮罩；`X-Amz-Credential` / `X-Goog-Credential` / `GoogleAccessId` 这类高置信度 cloud-signed credential 键当前也属于遮罩范围
- 当前默认不会把裸 `auth` / `key` 这类宽泛参数名直接当作敏感字段；masking 仍优先针对高置信度 credential / signature 键，避免误伤普通诊断信息、示例 URL 与可核验 source 链接
- `toggle_builtin_tools` 仅针对 Claude Code 项目级设置生效，不应视为通用 MCP 特性
- 多数对外 user-facing 错误当前不再附带 `request_id`；但个别上游空占位 completion 异常当前仍可能携带 `request_id`。`get_config_info` 中的 Claude 项目上下文检查也不再回显绝对 Git 根路径
- 根包 `grok_search` 当前对 `mcp` 采用 lazy export；非 server 模块导入不应再因为 `fastmcp` 缺失而提前失败
- `grok_search.providers.GrokSearchProvider` 当前也采用 access-time lazy export；普通非 provider 导入不应仅因 Grok provider 相关依赖缺失而提前失败
- 上述 lazy export 只是 import-time boundary，不应被理解为安装依赖已变成 optional extra；`pyproject.toml` 中的依赖声明当前仍然是安装时 contract
- `split_answer_and_sources` 当前应避免把 fenced code 中的 `sources(...)` 示例，或正文语义上的普通尾部链接列表，误拆成真实信源
- server 主进程当前应在非预期致命异常下以非零退出码结束，而不是统一伪装成成功退出
- 若本地 `stdio` 安装/启动在企业或自签证书环境中失败，优先在 `uvx` 启动命令中增加 `--native-tls`，不要草率记录成关闭 TLS 校验
