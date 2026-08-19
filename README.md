# llm-usage

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

## 支持的平台

| 平台 | 方式 | 说明 |
|------|------|------|
| Kimi Code (Moonshot Coding Plan) | 自动 API | `GET /coding/v1/usages`，Bearer |
| 火山方舟 Coding Plan | 自动 API | Volcengine OpenAPI `GetCodingPlanUsage`，AK/SK + V4 签名 |
| 火山方舟 Agent Plan | 自动 API | Volcengine OpenAPI `GetAFPUsage`，AK/SK + V4 签名 |
| Ollama Cloud | 自动 API | `GET /api/usage`，Bearer |
| OpenCode Go | 自动 API | `GET /zen/go/v1/usage`，Bearer |

## 配置

配置文件位于 `./config.toml`（当前目录）。API 密钥支持 `env:VARNAME` 前缀，
从环境变量读取，避免明文存储在磁盘上。运行 `llm-usage config --init` 生成模板。
可用 `LLM_USAGE_CONFIG` 环境变量覆盖路径（历史数据库用 `LLM_USAGE_DB`）。

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

## 时间显示

`重置时间`（额度窗口重置时刻）、总览标题和历史时间戳均显示为机器**本地时区**。
各平台返回的是 UTC 时间戳；存储值（`reset_at`、快照 `ts`）与 `--json` 输出保持
原始 UTC ISO 8601 格式。机器切换时区时，显示会随之变化。

`重置倒计时` 列显示距下一次重置的剩余时间（如 `2天3小时`、`5小时20分`、`32分`），
基于**当前系统时钟**在每次渲染时计算——`llm-usage tui` 中每秒刷新；重置时刻已过
显示 `已重置`，无重置时间显示 `-`。该列与进度条百分比标签右对齐。

Web 仪表盘同理：服务端 API 返回 UTC ISO 时间戳，浏览器端转换为本地时区显示，
重置倒计时由前端每秒在客户端重算。
