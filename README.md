# llm-usage

[![docker](https://github.com/MrLinWang/llm-plan-usage/actions/workflows/docker.yml/badge.svg)](https://github.com/MrLinWang/llm-plan-usage/actions/workflows/docker.yml)

统一监控多个 LLM 编程额度 / Token 计划的命令行工具。

## 安装

```bash
pip install -e .
```

若需使用 Web 仪表盘，安装 web extra：

```bash
pip install -e ".[web]"
```

或使用一键安装脚本（需要 [uv](https://docs.astral.sh/uv/)）：

```bash
./setup.sh                    # 创建 .venv 并安装依赖（含 test/web extra）
source .venv/bin/activate     # 激活虚拟环境
```

## 快速开始

```bash
llm-usage config --init     # 生成示例配置 ./config.toml
llm-usage show              # 拉取所有平台用量，渲染 rich 表格
llm-usage show --keys        # 总览 + 多 Key 分组的各 Key 用量明细
llm-usage show --json        # JSON 输出，便于脚本处理
llm-usage tui                # 交互式终端仪表盘（q 退出，r 刷新，+/- 调间隔）
llm-usage web                # 浏览器仪表盘（默认 http://127.0.0.1:8765）
llm-usage history --days 7   # 查看历史快照
```

## Docker

镜像由 GitHub Actions 自动构建并发布到 GHCR（`main` 分支 → `latest`，tag `v*` → 对应版本号，多架构 amd64/arm64）：

```bash
docker run -d --name llm-usage \
  -p 8765:8765 \
  -v llm-usage-data:/data \
  ghcr.io/mrlinwang/llm-plan-usage:latest
```

打开 http://127.0.0.1:8765/ ，首次访问创建管理员账号；之后在「供应商配置」页填写各平台凭证（写回 `/data/config.toml`），或直接注入环境变量：

```bash
docker run -d --name llm-usage -p 8765:8765 -v llm-usage-data:/data \
  -e KIMI_API_KEY=sk-... -e TZ=Asia/Shanghai \
  ghcr.io/mrlinwang/llm-plan-usage:latest
```

- `/data` 卷持久化配置与历史数据库（含用户/会话）；推荐命名卷。bind mount 宿主目录时需对 uid 1000 可写。
- 改端口用 `-e PORT=9000 -p 9000:9000`（healthcheck 跟随 `$PORT`）。
- 本地构建：`docker build -t llm-usage:dev .`

## 支持的平台

| 平台 | 方式 | 说明 |
|------|------|------|
| Kimi Code (Moonshot Coding Plan) | 自动 API | `GET /coding/v1/usages`，Bearer |
| 火山方舟 Coding Plan | 自动 API | Volcengine OpenAPI `GetCodingPlanUsage`，AK/SK + V4 签名 |
| 火山方舟 Agent Plan | 自动 API | Volcengine OpenAPI `GetAFPUsage`，AK/SK + V4 签名 |
| Ollama Cloud | 自动 API | `GET /api/usage`，Bearer |
| OpenCode Go | 自动 API | `GET /zen/go/v1/usage`，Bearer |
| ClinePass | 自动 API | `GET /api/v1/users/me/plan/usage-limits`，Bearer；5小时/每周/每月 百分比窗口 |
| LLM Gateway（本地 Sub2API 网关） | 自动 API | `GET /v1/usage`，Bearer；显示今日 `actual_cost` USD |

以上 7 类为内置平台键；每类还可添加多个**独立实例**（多账号卡片，键形如
`kimi#2`），见下方「同类型供应商实例」。

## 配置

配置文件位于 `./config.toml`（当前目录）。API 密钥支持 `env:VARNAME` 前缀，
从环境变量读取，避免明文存储在磁盘上。运行 `llm-usage config --init` 生成模板。
LLM Gateway 与其他平台一样完全在 `config.toml` 中配置：`base_url` 必填；
`usage.today.actual_cost` 是今日自然日实际扣费，`cost` 仅作为旧接口无
`actual_cost` 时的回退。API keys 以**组**为单位配置：每组共享一个每日额度，
组内所有成功 Key 的 `actual_cost` 之和为该组用量，每组单独显示和保存历史。
模板默认生成一组（`组1`，`env:LLM_GATEWAY_API_KEY`）：

```toml
[platforms.llm-gateway]
enabled = true
base_url = "http://127.0.0.1:18080"

[[platforms.llm-gateway.groups]]
name = "组1"
# daily_limit = 100
api_keys = ["sk-team-a-key-1", "sk-team-a-key-2"]
```

Key 直接写在 config.toml 即可，也支持 `env:VARNAME` 引用环境变量。每个 Key 可
配置**显示名称**（`show --keys` 的 Key 明细与 Web 仪表盘悬浮明细中展示，失败
提示也会带上名称）：

```toml
api_keys = [
    { name = "主 Key", value = "env:LLM_GATEWAY_TEAM_A_1" },
    "sk-team-a-key-2",  # 无名称,显示 key#2
]
```

组内部分 Key 请求失败时，成功 Key 仍会
聚合，但结果会带提示；带提示的部分聚合不会写入历史快照，以免把不完整数据当成完整用量。
同一个 Key 不应配置到多个分组。可用
`LLM_USAGE_CONFIG` 环境变量覆盖路径（历史数据库用 `LLM_USAGE_DB`）。

旧的顶层单 Key 形式（`api_key` + 可选 `daily_limit` + `use_groups`）仍被
provider 兼容，可手工编辑 `config.toml` 使用；Web 端会把这种配置当作一组展示，
保存时自动迁移为 `groups` 并置 `use_groups = true`。

### 多计费套餐（kimi / 火山 ×2 / ollama / opencode-go / clinepass）

除 LLM Gateway 外，其余平台也支持**多凭证**：同一平台下不同凭证 = 该供应商的
**不同计费套餐**。每个凭证彼此完全独立——各自 fetch、各自展示、无共享限额、
不合并数值；仪表盘按套餐分区展示同一平台的用量（`show`/`tui`/`web` 一致）。
与 Gateway 的 groups（共享额度、聚合求和）不同。
只有一个凭证时不显示套餐分区头——单槽（含 Web 编辑器保存时写入的
`name = "套餐1"`）与旧的顶层单 Key 形态展示完全一致；两个及以上凭证才按
套餐分区。

Web「供应商配置」页每个平台默认一个凭证槽（kimi/ollama/opencode-go/clinepass 为
`api_key`；火山两个平台为 `access_key` + `secret_key`），点「添加凭证」
（火山为「添加 AK/SK」）即可增加新套餐；「套餐名」留空时服务端自动命名
`套餐1`、`套餐2`…。凭证值留空 = 保留已保存的值；保存后写回
`credentials` 数组并清除顶层单凭证字段（旧单 Key 形式首次保存即自动迁移）。
手工配置同样支持：

```toml
[platforms.kimi]
enabled = true
# base_url = "https://api.kimi.com/coding/v1"

[[platforms.kimi.credentials]]
name = "套餐A"
api_key = "env:KIMI_API_KEY_A"

[[platforms.kimi.credentials]]
name = "套餐B"
api_key = "sk-kimi-xxx"
```

每个凭证 = 一个独立计费套餐：`show`/`tui`/`web` 的同一平台卡片内按套餐分块
显示各自的数据行（套餐名标头）；历史快照记录套餐列；JSON 输出的每个 entry
带 `plan` 字段。同一平台的部分凭证失败时该平台整体仍显示成功部分并附提示
（不写历史快照）；全部失败才显示错误。没有 `credentials` 数组的旧配置
（顶层单凭证）行为完全不变。

## 安全说明

- **文件权限**：`config.toml` 与 `history.db` 以 `0600` 创建（含明文 API 密钥、
  密码哈希与会话令牌）。勿放宽权限，勿提交到 git。
- **登录限流**：登录 / 注册 / 初始化接口按来源 IP 限流——60 秒内失败 5 次
  即锁定 5 分钟（HTTP 429）。计数在进程内存中，重启清零。
- **错误信息脱敏**：网络/内部异常只返回通用文案（如「网络错误」「内部错误」、`HTTP {状态码}`），异常细节仅写入服务端日志，不回显给浏览器。
- **会话令牌**：`history.db` 中会话令牌为明文存储——数据库泄露即等于会话可被
  劫持，请自行保管好该文件。
- **HTTPS 反代**：若经反向代理以 HTTPS 对外提供服务，设置环境变量
  `LLM_USAGE_SECURE_COOKIE=1` 为会话 Cookie 追加 `Secure` 标志。
- **单进程部署**：用量缓存与登录限流均在进程内生效，请勿使用多 worker 部署
  （默认单 worker 的 `llm-usage web` 无需任何配置）。
- **WAL 备份**：SQLite 启用 WAL 模式；备份时需一并包含 `history.db-wal` /
  `history.db-shm`，或先执行 checkpoint。

## Web 仪表盘

`llm-usage web` 启动一个 FastAPI + Uvicorn 服务，默认监听 `127.0.0.1:8765`，
浏览器打开 `http://127.0.0.1:8765/` 即可查看用量总览。Web 仪表盘与 `llm-usage show`
输出等价，按平台分卡片展示：每张卡片标出平台名与状态，内含该平台的
窗口/已用/限额/剩余/重置时间/重置倒计时列和进度条。

后端以 `interval` 秒（默认 60，最小 5，最大 3600）为 TTL 缓存 provider 结果，
缓存过期前不会重复调用外部 API；前端按服务端返回的 `interval` 轮询。
数据为实时仪表盘，不写入历史快照。

页面右上角可切换浅色/深色主题；选择持久化在浏览器 `localStorage`，
未手动选择时跟随系统 `prefers-color-scheme`（默认深色）。

仪表盘是自适应布局：视口高度放不下全部平台卡片时，前端自动按 normal → compact →
ultra 三档密度试排（只压缩留白与次要列，不缩小正文），窗口尺寸变化后自动重新试排；
每张卡片同时是独立的容器查询上下文，窄卡隐藏「限额/重置时间」次要列并把数字缩写为
k/M/B（完整数值悬停可见）。entry 较多的平台默认精简展示：「+N」一键展开全部、再点收起；
周期窗口平台折叠时优先保留 5小时/每周 等核心窗口；同一计费单位的平台改为 5 秒轮播
切换（悬停、聚焦或切走标签页即暂停），并在尾部显示客户端合计的「全部合计」行
（各条限额不完整时只合计已用，不虚构总进度）。
展开的卡片内容按外层卡片网格列数重排为多列；每个平台的展开状态保存在
`sessionStorage`（仅当前标签页会话，跨数据刷新与页面刷新保留，退出登录时清除）。

### PWA（安装到桌面 / 离线查看）

仪表盘是一个 Progressive Web App：在浏览器地址栏安装（Chrome/Edge 安装图标）或
iOS Safari「添加到主屏幕」后，以独立窗口（standalone）运行。四个页面均内联注册
Service Worker（`/sw.js`，根作用域）：

- **离线兜底**：页面导航网络优先、失败回退缓存；`/api/usage` 等只读 GET 会缓存
  最近一次成功响应——断网时刷新仍能看到上次拉取的完整数据（状态栏「上次获取」
  保持不变），恢复联网后自动继续轮询。
- **免登录资产**：`/manifest.webmanifest`（应用名/图标/standalone）、
  `/icons/icon-192.png`、`/icons/icon-512.png`、`/icons/apple-touch-icon.png`
  无需登录即可获取（登录页也要能安装）；主题色固定深色 `#16181d`。
- **缓存版本**：修改 `static/sw.js` 后必须递增文件头部的 `CACHE` 版本号，
  客户端才会淘汰旧缓存。

```bash
llm-usage web --host 127.0.0.1 --port 8765 --interval 60
```

未安装 web extra 时执行 `llm-usage web` 会提示安装 `pip install llm-usage[web]`。

### 登录与用户管理

Web 端所有页面和 API 都需要登录。首次启动（尚无用户）访问任意页面会进入
初始化流程，创建首个**管理员**账号并自动登录；之后访问 `/login` 登录。
会话 Cookie 有效期 7 天（`httponly`、`samesite=lax`），重启服务不失效。

- **管理员**：仪表盘头部的「用户管理」可列出/新增/删除用户、重置他人密码
  （不能删除自己；系统**只存在一个管理员**——「新增用户」一律创建普通用户，
  管理员账号只能由首次初始化创建）；「供应商配置」见下文。
- **普通用户**：仪表盘只显示自己启用的平台（见下文「多用户用量隔离」）；
  「供应商配置」页与管理员同构（凭证槽 + gateway base_url/组 + 可见性），
  写入自己的 `user_configs`（存 `history.db`），无「用户管理」入口。
- **用户配置开关**：管理员可在「用户管理」页关闭**允许普通用户自行添加/编辑
  自己的供应商**（站点开关，默认开启；状态持久化在 `history.db` 的
  `settings` 表）；关闭后普通用户的「供应商配置」页显示提示、`/api/my/*`
  API 返回 403，只读仪表盘不受影响。
- **开放注册**：管理员在「用户管理」页勾选「允许访客在登录页注册账号」后，
  登录页出现「没有账号?注册」入口；自助注册的账号永远是**普通用户**，
  注册成功即自动登录。开关默认关闭，状态持久化在 `history.db` 的
  `settings` 表（重启不丢失）；关闭后注册入口消失，注册 API 返回 403。
- **用户名规则**：注册 / 创建账号的用户名限 1–64 位字母、数字、下划线、点或连字符（`[A-Za-z0-9_.-]`）。
- 修改或重置密码后，该用户的其他登录会话立即失效。

用户与会话存储在 `./history.db`（`users`/`sessions` 表）中，密码以
PBKDF2-SHA256（60 万次迭代、随机盐）哈希存储，不落明文；站点级开关
（如开放注册）存同一数据库的 `settings` 表。
忘记全部管理员密码时，删除 `history.db` 中 `users` 表的行即可重新初始化。

### 供应商配置

管理员在「供应商配置」页可直接编辑各平台的启用开关与凭证
（kimi/ollama/opencode-go/clinepass 为 `api_key`；火山两个平台为 `access_key` + `secret_key`；
llm-gateway 无顶层凭证字段，改为分组配置）。kimi/火山×2/ollama/opencode-go/clinepass
以**凭证槽**为单位编辑：每个槽 = 一个独立计费套餐（「套餐名」可选，留空自动
命名 `套餐N`），槽内一个凭证输入框（火山为 AK+SK 两个），点「添加凭证」
（火山「添加 AK/SK」）追加新套餐槽。LLM Gateway 以**组**为单位编辑：
默认一组「组1」，每组一个 API key 输入框 + 一个每日限额输入框（留空 = 不设限），
组内可点「添加共享 Key」追加共享同一额度的 key，底部「添加组」新增分组；
每个 Key 行还有一个可选的「Key 名称」输入框，名称会显示在仪表盘分组行的
悬浮明细与 `show --keys` 的 Key 明细中（留空 = 保留现有名称）；
同组 Key 的今日用量会合并计算。凭证永远不回显明文：已保存的值显示为掩码
（如 `已设置 (sk-k…ey)`），`env:VARNAME` 引用原样显示；凭证值留空表示保留
已保存的值，删除整个槽才会移除该凭证。保存立即写回 `./config.toml` 并使服务端
用量缓存失效（下次刷新生效）：槽式保存写 `credentials` 数组并清除顶层凭证
字段；LLM Gateway 的分组保存写 `groups` 并置 `use_groups = true`。LLM Gateway
的 `base_url` 也在同一页直接编辑（留空 = 不修改）；`usage_path` 等高级字段
仍需手工编辑 `config.toml`。

### 同类型供应商实例（多账号卡片）

同一类型可以添加多个**独立实例**（如第二个 Kimi 账号）：「供应商配置」页顶部
的添加栏选类型后点「添加供应商」，即生成一张新卡（实例键形如 `kimi#2`，首个
实例为裸基础键不动）。每个实例有独立凭证、独立显示名、独立可见性、独立仪表盘
卡片；新实例默认**停用**，配好凭证后再勾选启用。管理员添加写入
`./config.toml`（`POST /api/config/providers`），普通用户写入自己的
`user_configs`（`POST /api/my/providers`）。删除走卡片上的「删除此供应商」
按钮（确认框），该实例的共享设置一并级联移除；内置的 7 个基础平台不可删除。
实例编号单调递增不复用（删除 `kimi#2` 后再添加得到 `kimi#3`），避免历史快照
歧义。终端侧无需配置：实例随所在平台的配置自动抓取并显示。

### 多用户用量隔离与可见性

用量信息默认**私有**：每个用户（含管理员）只在自己的仪表盘看到自己的平台配置。
系统为**单管理员模型**——管理员（首个 setup 创建的账号）的平台配置存在
`./config.toml`；普通用户（含自助注册）的平台配置存在 `./history.db` 的
`user_configs` 表（JSON），与 `config.toml` 完全隔离。粒度是**平台级**
（platform key）。

- **普通用户与管理员同样可配置全部供应商信息**：「供应商配置」页两模式同构
  ——普通用户同样有凭证槽编辑器（kimi/ollama/opencode-go 的 `api_key`、火山×2
  的 AK/SK，每槽一个计费套餐）与 LLM Gateway 的 `base_url` + 分组编辑器
  （组、每日限额、共享 Key、Key 名称），字段级校验与 admin `/api/config` 完全
  一致（槽式保存写 `credentials` 数组、清除顶层凭证字段、`use_groups = true`）。
  唯一差异：普通用户保存写入自己的 `user_configs`（`/api/my/platforms`，
  存 `history.db`），admin 写 `config.toml`（`/api/config`）；凭证一律不回显
  明文（掩码/`env:`/未设置三态）。
- **可见性三态**（「供应商配置」页每张平台卡上的下拉框）：
  - **私有**（默认）：仅自己可见；
  - **公开**：所有登录用户可见；
  - **共享给指定用户**：勾选用户名后仅这些用户可见（不能共享给自己；
    目标用户必须存在；重复勾选自动去重）。
- **来源标注**：共享来的平台在卡片标题上追加 `(用户名)` 后缀
  （如 `Kimi Code(alice)`），管理员公开的平台同样显示 `LLM Gateway(admin)`
  （共享即标注来源，无例外）。
- **自定义显示名称**：「供应商配置」页每张平台卡都有「显示名称」输入框
  （全平台可用，留空 = 保留当前值，最长 64 字符）；改名后自己的仪表盘立即使用
  新名称（终端 `show`/`tui` 同样生效），共享给他人时对方看到
  `自定义名(用户名)`（如 `我的K(alice)`）。
- **同名冲突（多来源并存）**：自己的平台保留裸名（自身来源，不参与冲突）；
  共享来的同名平台每个来源都保留展示——`Kimi Code(admin)` 与
  `Kimi Code(alice)` 是两张不同的卡，不覆盖、不丢弃。
- **管理员仪表盘** = `config.toml` 全部平台 + 其他用户公开/共享给 admin 的平台
  （共享给 admin 合法但无实际效果）；**普通用户仪表盘** = 自己的 DB 配置平台 +
  admin 公开/共享给他的平台 + 其他用户公开/共享给他的平台。
- **可见性存储**：写入 `history.db` 的 `user_shares` 表（owner/platform/target）；
  删除用户时其配置与所有共享关系（作为 owner 或 target）一并级联删除；
  删除一个供应商实例同样级联清掉它的全部共享行。
- 后端按**配置内容哈希**为每个不同配置建立独立用量缓存条目（最多 32 个，
  超出淘汰最旧），相同配置的用户共享同一条缓存，不会重复调用外部 API。


## 开发

```bash
pip install -e ".[test,dev]"
python -m pytest tests/ -q          # 全量测试(无网络)
ruff check src tests && mypy src    # lint + 类型检查
```

注：`show --plain` 参数当前未实现（声明保留，行为等同默认渲染）。

## 时间显示

`重置时间`（额度窗口重置时刻）、总览标题和历史时间戳均显示为机器**本地时区**。
各平台返回的是 UTC 时间戳；存储值（`reset_at`、快照 `ts`）与 `--json` 输出保持
原始 UTC ISO 8601 格式。机器切换时区时，显示会随之变化。

`重置倒计时` 列显示距下一次重置的剩余时间（如 `2天3小时`、`5小时20分`、`32分`），
基于**当前系统时钟**在每次渲染时计算——`llm-usage tui` 中每秒刷新；重置时刻已过
显示 `已重置`，无重置时间显示 `-`。该列与进度条百分比标签右对齐。

Web 仪表盘同理：服务端 API 返回 UTC ISO 时间戳，浏览器端转换为本地时区显示，
重置倒计时由前端每秒在客户端重算。
