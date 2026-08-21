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
| LLM Gateway（本地 Sub2API 网关） | 自动 API | `GET /v1/usage`，Bearer；显示今日 `actual_cost` USD |

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

Key 直接写在 config.toml 即可，也支持 `env:VARNAME` 引用环境变量。组内部分 Key 请求失败时，成功 Key 仍会
聚合，但结果会带提示；带提示的部分聚合不会写入历史快照，以免把不完整数据当成完整用量。
同一个 Key 不应配置到多个分组。可用
`LLM_USAGE_CONFIG` 环境变量覆盖路径（历史数据库用 `LLM_USAGE_DB`）。

旧的顶层单 Key 形式（`api_key` + 可选 `daily_limit` + `use_groups`）仍被
provider 兼容，可手工编辑 `config.toml` 使用；Web 端会把这种配置当作一组展示，
保存时自动迁移为 `groups` 并置 `use_groups = true`。

### 多计费套餐（kimi / 火山 ×2 / ollama / opencode-go）

除 LLM Gateway 外，其余平台也支持**多凭证**：同一平台下不同凭证 = 该供应商的
**不同计费套餐**。每个凭证彼此完全独立——各自 fetch、各自展示、无共享限额、
不合并数值；仪表盘按套餐分区展示同一平台的用量（`show`/`tui`/`web` 一致）。
与 Gateway 的 groups（共享额度、聚合求和）不同。

Web「供应商配置」页每个平台默认一个凭证槽（kimi/ollama/opencode-go 为
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

```bash
llm-usage web --host 127.0.0.1 --port 8765 --interval 60
```

未安装 web extra 时执行 `llm-usage web` 会提示安装 `pip install llm-usage[web]`。

### 登录与用户管理

Web 端所有页面和 API 都需要登录。首次启动（尚无用户）访问任意页面会进入
初始化流程，创建首个**管理员**账号并自动登录；之后访问 `/login` 登录。
会话 Cookie 有效期 7 天（`httponly`、`samesite=lax`），重启服务不失效。

- **管理员**：仪表盘头部的「用户管理」可列出/新增/删除用户、重置他人密码
  （不能删除自己，系统保证至少保留一个管理员）；「供应商配置」见下文。
- **普通用户**：仅可查看仪表盘，访问管理页面会被弹回仪表盘。
- **开放注册**：管理员在「用户管理」页勾选「允许访客在登录页注册账号」后，
  登录页出现「没有账号?注册」入口；自助注册的账号永远是**普通用户**，
  注册成功即自动登录。开关默认关闭，状态持久化在 `history.db` 的
  `settings` 表（重启不丢失）；关闭后注册入口消失，注册 API 返回 403。
- 所有用户都可以在「用户管理」页修改自己的密码；修改或重置密码后，
  该用户的其他登录会话立即失效。

用户与会话存储在 `./history.db`（`users`/`sessions` 表）中，密码以
PBKDF2-SHA256（60 万次迭代、随机盐）哈希存储，不落明文；站点级开关
（如开放注册）存同一数据库的 `settings` 表。
忘记全部管理员密码时，删除 `history.db` 中 `users` 表的行即可重新初始化。

### 供应商配置

管理员在「供应商配置」页可直接编辑各平台的启用开关与凭证
（kimi/ollama/opencode-go 为 `api_key`；火山两个平台为 `access_key` + `secret_key`；
llm-gateway 无顶层凭证字段，改为分组配置）。kimi/火山×2/ollama/opencode-go
以**凭证槽**为单位编辑：每个槽 = 一个独立计费套餐（「套餐名」可选，留空自动
命名 `套餐N`），槽内一个凭证输入框（火山为 AK+SK 两个），点「添加凭证」
（火山「添加 AK/SK」）追加新套餐槽。LLM Gateway 以**组**为单位编辑：
默认一组「组1」，每组一个 API key 输入框 + 一个每日限额输入框（留空 = 不设限），
组内可点「添加共享 Key」追加共享同一额度的 key，底部「添加组」新增分组；
同组 Key 的今日用量会合并计算。凭证永远不回显明文：已保存的值显示为掩码
（如 `已设置 (sk-k…ey)`），`env:VARNAME` 引用原样显示；凭证值留空表示保留
已保存的值，删除整个槽才会移除该凭证。保存立即写回 `./config.toml` 并使服务端
用量缓存失效（下次刷新生效）：槽式保存写 `credentials` 数组并清除顶层凭证
字段；LLM Gateway 的分组保存写 `groups` 并置 `use_groups = true`。LLM Gateway
的 `base_url` 也在同一页直接编辑（留空 = 不修改）；`usage_path` 等高级字段
仍需手工编辑 `config.toml`。

## 时间显示

`重置时间`（额度窗口重置时刻）、总览标题和历史时间戳均显示为机器**本地时区**。
各平台返回的是 UTC 时间戳；存储值（`reset_at`、快照 `ts`）与 `--json` 输出保持
原始 UTC ISO 8601 格式。机器切换时区时，显示会随之变化。

`重置倒计时` 列显示距下一次重置的剩余时间（如 `2天3小时`、`5小时20分`、`32分`），
基于**当前系统时钟**在每次渲染时计算——`llm-usage tui` 中每秒刷新；重置时刻已过
显示 `已重置`，无重置时间显示 `-`。该列与进度条百分比标签右对齐。

Web 仪表盘同理：服务端 API 返回 UTC ISO 时间戳，浏览器端转换为本地时区显示，
重置倒计时由前端每秒在客户端重算。
