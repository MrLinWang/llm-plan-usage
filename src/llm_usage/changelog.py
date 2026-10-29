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
            {"kind": "feature", "text": "仪表盘 ⋯ 菜单新增「关于」，显示当前版本号",
                "text_en": "Added “About” to the dashboard ⋯ menu with the current version"},
            {"kind": "feature", "text": "「关于」面板展示本功能更新日志",
                "text_en": "The About panel now shows these release notes"},
            {
                "kind": "fix",
                "text": "畸形的 platforms 配置不再被静默清空"
                        "（此前可把 config.toml 写成 0 字节）",
                "text_en": "Malformed platforms config is no longer silently "
                           "truncated (previously config.toml could become 0 bytes)",
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
                "text_en": "Unified provider config: one [platforms.<key>] table "
                           "per provider, with accounts in an array",
            },
            {"kind": "feature", "text": "新增 OpenDesign 用量监控与浏览器登录授权",
                "text_en": "Added OpenDesign usage monitoring and browser login authorization"},
            {"kind": "feature", "text": "新增 Sub2API（原 llm-gateway）共享 Key 分组用量",
                "text_en": "Added Sub2API (formerly llm-gateway) shared-key group usage"},
            {"kind": "feature", "text": "多计费套餐、同类型供应商实例、自定义显示名",
                "text_en": "Multiple billing plans, same-type provider instances, "
                           "and custom display names"},
            {"kind": "feature", "text": "多用户隔离、可见性三态、开放注册与站点开关",
                "text_en": "Multi-user isolation, 3-state visibility, open registration, "
                           "and site toggles"},
            {
                "kind": "feature",
                "text": "支持供应商：Kimi、火山 Coding/Agent、Ollama、OpenCode Go、"
                        "ClinePass、Command Code、OpenDesign、Sub2API",
                "text_en": "Supported providers: Kimi, Volcengine Coding/Agent, "
                           "Ollama, OpenCode Go, ClinePass, Command Code, OpenDesign, Sub2API",
            },
            {
                "kind": "feature",
                "text": "Web 仪表盘：自适应密度、卡片轮播、PWA 离线外壳、"
                        "分组 Key 明细浮层",
                "text_en": "Web dashboard: responsive density, card rotation, "
                           "PWA offline shell, and key-group detail popovers",
            },
            {"kind": "feature", "text": "一键导入 config.toml、拖拽调整平台顺序",
                "text_en": "One-click config.toml import and drag-to-reorder platforms"},
            {"kind": "feature", "text": "瞬时故障自动重试、统一运行日志、Docker 多架构镜像",
                "text_en": "Automatic retry for transient failures, unified logging, "
                           "and multi-arch Docker images"},
        ],
    },
]

RELEASE_KINDS = ("feature", "fix", "breaking")


def changelog() -> list[dict[str, Any]]:
    """Return the log newest-first, localized for the current locale.

    ``text`` is the canonical Chinese text; English callers receive
    ``text_en`` when present.  The returned structure remains a deep copy.
    """
    from llm_usage import i18n

    data = deepcopy(CHANGELOG)
    if i18n.current_locale() == "en":
        for release in data:
            for entry in release.get("entries", []):
                if entry.get("text_en"):
                    entry["text"] = entry["text_en"]
    return data
