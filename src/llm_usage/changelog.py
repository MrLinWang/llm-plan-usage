"""User-facing release notes rendered by the dashboard's 关于 panel.

The log ships with the package (Docker copies ``src/`` and installs it), so it
lives here rather than in a repository-root changelog file.  Release a new
version by prepending a record to :data:`CHANGELOG` and bumping ``version`` in
``pyproject.toml`` and ``llm_usage.__version__`` together; the test suite
asserts the newest record matches the installed version.

``kind`` drives the badge shown next to each entry: ``feature`` (新功能),
``fix`` (修复) or ``breaking`` (不兼容).
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

# Newest version first.
CHANGELOG: list[dict[str, Any]] = [
    {
        "version": "0.2.0",
        "date": "2026-10-22",
        "entries": [
            {"kind": "feature", "text": "仪表盘 ⋯ 菜单新增「关于」，显示当前版本号"},
            {"kind": "feature", "text": "「关于」面板展示本功能更新日志"},
            {
                "kind": "fix",
                "text": "畸形的 platforms 配置不再被静默清空"
                        "（此前可把 config.toml 写成 0 字节）",
            },
        ],
    },
    {
        "version": "0.1.0",
        "date": "2026-10-16",
        "entries": [
            {
                "kind": "feature",
                "text": "统一供应商配置：每个供应商一个 [platforms.<key>] 表，"
                        "账号写在 accounts 数组内",
            },
            {"kind": "feature", "text": "新增 OpenDesign 用量监控与浏览器登录授权"},
            {"kind": "feature", "text": "新增 Sub2API（原 llm-gateway）共享 Key 分组用量"},
            {"kind": "feature", "text": "多计费套餐、同类型供应商实例、自定义显示名"},
            {"kind": "feature", "text": "多用户隔离、可见性三态、开放注册与站点开关"},
            {
                "kind": "feature",
                "text": "支持供应商：Kimi、火山 Coding/Agent、Ollama、OpenCode Go、"
                        "ClinePass、Command Code、OpenDesign、Sub2API",
            },
            {
                "kind": "feature",
                "text": "Web 仪表盘：自适应密度、卡片轮播、PWA 离线外壳、"
                        "分组 Key 明细浮层",
            },
            {"kind": "feature", "text": "一键导入 config.toml、拖拽调整平台顺序"},
            {"kind": "feature", "text": "瞬时故障自动重试、统一运行日志、Docker 多架构镜像"},
        ],
    },
]

RELEASE_KINDS = ("feature", "fix", "breaking")


def changelog() -> list[dict[str, Any]]:
    """Return the log newest-first as a deep copy, so callers cannot mutate it.

    ``deepcopy`` (rather than rebuilding known keys) keeps any future
    per-release field intact instead of silently dropping it.
    """
    return deepcopy(CHANGELOG)
