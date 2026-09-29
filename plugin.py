"""MaiBot plugin for Helldivers 2 update announcements.

The module deliberately keeps collection, filtering and persistence independent
from MaiBot.  That makes the data path testable without a running bot and also
allows the plugin to run with either the 2.x SDK or a light-weight host shim.
"""
from __future__ import annotations

import asyncio
import hashlib
import html
import json
import random
import re
import sqlite3
import time
import unicodedata
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

try:  # maibot-plugin-sdk 2.x
    from maibot_sdk import (  # type: ignore
        CONFIG_RELOAD_SCOPE_SELF,
        Command,
        Field,
        MaiBotPlugin,
        PluginConfigBase,
    )
    from pydantic import ConfigDict  # type: ignore
except ImportError:  # importing the module should work in a test environment
    CONFIG_RELOAD_SCOPE_SELF = "self"

    class PluginConfigBase:
        pass

    def ConfigDict(**kwargs: Any) -> Any:
        return dict(kwargs)

    def Field(default: Any = None, default_factory: Any = None, **_: Any) -> Any:
        return default_factory() if default_factory else default

    class MaiBotPlugin:
        ctx: Any

    def Command(
        name: str = "",
        description: str = "",
        pattern: str = "",
        aliases: Any = None,
        **metadata: Any,
    ):
        """Stand-in mirroring the SDK decorator so tests can inspect the metadata.

        Mirrors the real signature (``name`` / ``description`` / ``pattern`` /
        ``aliases`` plus free-form metadata) so a test assertion behaves the same
        with or without the SDK installed.
        """

        def decorate(fn: Any) -> Any:
            setattr(
                fn,
                "__maibot_component_info__",
                {
                    "name": name,
                    "description": description,
                    "command_pattern": pattern,
                    "aliases": list(aliases or []),
                    **metadata,
                },
            )
            return fn

        return decorate


APP_ID = 553850
PROJECT_URL = "https://github.com/TouristH/HelldiversPatchFeed"
USER_AGENT = f"HelldiversPatchFeed/0.1.1 (+{PROJECT_URL})"
STEAM_NEWS_URL = (
    "https://api.steampowered.com/ISteamNews/GetNewsForApp/v0002/"
    f"?appid={APP_ID}&count=50&maxlength=10000"
)
WIKI_API_URL = "https://helldivers.wiki.gg/api.php"
WIKI_BASE_URL = "https://helldivers.wiki.gg/wiki/"
# The wiki ships a Chinese sub-wiki; it is preferred but lags behind the English one.
WIKI_ZH_API_URL = "https://helldivers.wiki.gg/zh/api.php"
WIKI_ZH_BASE_URL = "https://helldivers.wiki.gg/zh/wiki/"
WIKI_SOURCES = ((WIKI_ZH_API_URL, WIKI_ZH_BASE_URL), (WIKI_API_URL, WIKI_BASE_URL))
DEFAULT_INTERVAL_SECONDS = 12 * 60 * 60
# Minimum gap between two on-demand source checks triggered by ``/helldivers push``.
# Within the gap the command still answers, but from the newest stored entry only.
FORCE_PUSH_MIN_INTERVAL_SECONDS = 60
# Steam marks developer announcements with this feed; everything else in the news
# payload is third-party press (Rock Paper Shotgun, SteamDB, ...) and is dropped.
OFFICIAL_STEAM_FEED = "steam_community_announcements"
# "Devoid of Liberty: 7.1.0" — release announcements carry the build number in the
# title and rarely contain the word "update", so a version number is the real signal.
VERSION_RE = re.compile(r"\b\d+\.\d+\.\d+\b")
PUSH_SCAN_LIMIT = 200
# Steam's news RSS honours a language parameter while GetNewsForApp ignores it, so
# localized titles and bodies are pulled from here and matched by build number.
STEAM_NEWS_RSS_URL = "https://store.steampowered.com/feeds/news/app/{app_id}/"
DEFAULT_STEAM_LOCALE = "schinese"
CHINESE_RE = re.compile(r"[\u4e00-\u9fff]")
CATEGORY_LABELS = {"patch": "补丁", "hotfix": "热修复", "update": "更新", "warbond": "战争债券"}
BEIJING_TZ = timezone(timedelta(hours=8))
# Heading wording differs per release, hence several keys across both languages.
WIKI_ZH_ATTRIBUTION = "来源：helldivers.wiki.gg 中文站（CC BY-SA 4.0）"
WIKI_EN_ATTRIBUTION = "来源：helldivers.wiki.gg 英文站（CC BY-NC-SA 4.0）"
WIKI_SECTION_KEYWORDS = ("平衡性", "数值", "平衡", "balancing", "balance")
WIKI_DIGEST_LIMIT = 1200
WIKI_RECENT_LIMIT = 10
# The detail link is only resolved for fresh releases: the wiki search endpoint is
# rate limited, and an old announcement does not need a link on a backfill.
WIKI_DETAIL_MAX_AGE_DAYS = 30

# ── random loadout generator ────────────────────────────────────────────────
# Item names live in a data file next to this module so the game data can be
# refreshed without touching the code. Icons are baked into a second file as
# data URIs because ``render.html2png`` defaults to ``allow_network=False``, so
# the card HTML must not reference anything outside itself.
LOADOUT_DATA_FILE = Path(__file__).with_name("loadout_data.json")
LOADOUT_ICONS_FILE = Path(__file__).with_name("loadout_icons.json")
LOADOUT_SLOTS = ("primary", "secondary", "grenade", "booster")
LOADOUT_SLOT_LABELS = {
    "primary": "主武器",
    "secondary": "副武器",
    "grenade": "投掷物",
    "booster": "强化资源",
    "stratagem": "战略配备",
}
LOADOUT_STRATAGEM_COUNT = 4
LOADOUT_CARD_WIDTH = 1080
# Taller than the icon-less mock-up: every slot now reserves room for its art.
LOADOUT_CARD_HEIGHT = 1880
LOADOUT_MOTTOS = (
    "装备全随机，超级地球认可你的勇气。",
    "这套配置由超级地球武装部随机配发。",
    "相信指挥部的判断，潜兵。",
    "随机也是一种信仰，民主不需要最优解。",
)
LOADOUT_FOOTER = "绝地潜兵 2 随机配装 · 由 HelldiversPatchFeed 生成"
# Lazily filled by ``load_loadout_icons``; ``None`` means "not read yet".
_LOADOUT_ICONS_CACHE: dict[str, str] | None = None
# ``/helldivers loadout <1-8>`` re-draws a single card of the loadout that was
# generated within this many seconds. The window slides on every reroll so a
# series of rerolls cannot be cut off halfway.
LOADOUT_REROLL_TTL_SECONDS = 120
# Card order as drawn on the image (two 2x2 grids, read left to right, top to
# bottom), which is the numbering the user sees and counts.
LOADOUT_SLOT_CHOICES: dict[int, str] = {
    1: "primary",
    2: "secondary",
    3: "grenade",
    4: "booster",
    5: "stratagem_0",
    6: "stratagem_1",
    7: "stratagem_2",
    8: "stratagem_3",
}
LOADOUT_INDEX_WORDS = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8}
LOADOUT_SLOT_HINT = "1 主武器 / 2 副武器 / 3 投掷物 / 4 强化资源 / 5-8 战略配备"
# Tiny self-contained page for ``/helldivers diag``: it exercises the exact same
# render call the real card uses, so a failure here explains a text-only fallback.
DIAG_HTML = (
    '<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8"><style>'
    f"body {{ width:{LOADOUT_CARD_WIDTH}px; min-height:{LOADOUT_CARD_HEIGHT}px; margin:0;"
    "display:flex; flex-direction:column; align-items:center; justify-content:center; gap:26px;"
    "background:linear-gradient(180deg,#101720,#05080c); color:#f5cb3d;"
    'font-family:"Microsoft YaHei","PingFang SC","Noto Sans CJK SC",sans-serif; }'
    ".big { font-size:78px; font-weight:900; letter-spacing:4px; }"
    ".small { color:#a7afb8; font-size:31px; }"
    '</style></head><body><div class="big">渲染自检 OK</div>'
    '<div class="small">这条消息是图片，说明渲染链路正常</div></body></html>'
)
# The host caps ``cap.call`` at 30 s (``DEFAULT_COMPONENT_RPC_TIMEOUT_MS``), which a
# cold browser start -- and a possible Chromium download -- blows straight through,
# so the very first render always timed out. Both budgets are raised explicitly:
# ``call_capability(timeout_ms=...)`` sets the RPC timeout separately from the
# capability arguments, and ``@Command(..., timeout_ms=...)`` raises the command's
# own RPC budget above it.
RENDER_RPC_TIMEOUT_MS = 150_000
COMMAND_RPC_TIMEOUT_MS = 180_000
RELEVANT_RE = re.compile(
    r"(?:\bpatch\b|\bupdate\b|\bwarbond\b|\bhotfix\b|\bbalance\b|"
    r"\bversion\b|补丁|更新|战争债券|热修复|平衡)",
    re.IGNORECASE,
)

# The subcommand is a *named* group so the host exposes it as
# ``matched_groups["action"]``; the same pattern is reused to re-parse the raw
# text when a host only provides unnamed groups.
# Trigger words. The host resolves a command by ``pattern.search(text)`` first and
# falls back to ``text.startswith(alias)`` (empty named groups), so both the regex
# and the registered aliases have to be generated from one list -- otherwise a
# short form would reach the handler without ever matching the pattern.
COMMAND_NAME = "helldivers"
COMMAND_TRIGGERS = (COMMAND_NAME, "hd", "潜兵", "绝地潜兵")
# Slash-prefixed: the host prefix-matches every incoming message against these, so
# a bare word like ``hd`` would swallow ordinary chat.
COMMAND_ALIASES = tuple(f"/{name}" for name in COMMAND_TRIGGERS if name != COMMAND_NAME)
TRIGGER_ALTERNATION = "|".join(re.escape(name) for name in COMMAND_TRIGGERS)
COMMAND_PATTERN = rf"^/(?:{TRIGGER_ALTERNATION})(?:\s+(?P<action>\w+))?(?:\s+(?P<arg>\w+))?\s*$"
COMMAND_RE = re.compile(COMMAND_PATTERN, re.IGNORECASE)
# Shortcut tables are the single source of truth: ``ACTION_ALIASES`` is extended
# from them and ``topic_shortcut`` renders them, so the help cannot drift from
# what the parser actually accepts.
ACTION_LABELS = {
    "subscribe": "订阅",
    "unsubscribe": "取消订阅",
    "push": "推送",
    "list": "列表",
    "loadout": "配装",
    "status": "状态",
    "diag": "自检",
    "help": "帮助",
}
LETTER_SHORTCUTS = (
    ("sub", "subscribe"),
    ("unsub", "unsubscribe"),
    ("p", "push"),
    ("ls", "list"),
    ("lo", "loadout"),
    ("st", "status"),
    ("diag", "diag"),
    ("h", "help"),
)
SINGLE_CHAR_SHORTCUTS = (
    ("订", "subscribe"),
    ("退", "unsubscribe"),
    ("推", "push"),
    ("列", "list"),
    ("装", "loadout"),
    ("态", "status"),
    ("诊", "diag"),
    ("助", "help"),
)
# ``all`` / ``warbond`` keep their English short form because a single letter would
# be ambiguous (``w`` -- wiki or warbond).
CATEGORY_SHORTCUTS = (
    ("v", "version"),
    ("pt", "patch"),
    ("hf", "hotfix"),
    ("wb", "warbond"),
    ("all", "all"),
    ("wk", "wiki"),
    ("bl", "balance"),
)
ACTION_ALIASES = {
    # Full Chinese names.
    "订阅": "subscribe",
    "取消订阅": "unsubscribe",
    "取消": "unsubscribe",
    "列表": "list",
    "状态": "status",
    "帮助": "help",
    "推送": "push",
    "强制推送": "push",
    "配装": "loadout",
    "随机配装": "loadout",
    "配装生成": "loadout",
    "诊断": "diag",
    "自检": "diag",
    "检测": "diag",
    "测试": "diag",
    # Letter and single-character short forms.
    **dict(LETTER_SHORTCUTS),
    **dict(SINGLE_CHAR_SHORTCUTS),
}
COMMAND_ACTIONS = frozenset({"subscribe", "unsubscribe", "push", "loadout", "diag", "list", "status", "help"})
# Actions a group may still use before it subscribes. Everything else is refused
# with a hint to run ``subscribe`` first (see ``require_subscription`` config).
UNSUBSCRIBED_ALLOWED_ACTIONS = frozenset({"subscribe"})
# Functional commands: open to everyone in an eligible group, but a user can be
# blacklisted per command or wholesale (``command_blacklist``).
FUNCTIONAL_ACTIONS = frozenset({"subscribe", "unsubscribe", "push", "loadout", "status", "help"})
# Management commands: whitelist-only (``admin_users``); the host operator always
# counts as an admin. These expose other groups' numbers or the plugin's internals.
ADMIN_ACTIONS = frozenset({"list", "diag"})
# Optional suffix of ``/helldivers push``: which kind of announcement to send.
PUSH_CATEGORY_ALIASES = {
    "version": "version",
    "版本": "version",
    "版本更新": "version",
    "更新": "version",
    "patch": "patch",
    "补丁": "patch",
    "hotfix": "hotfix",
    "热修复": "hotfix",
    "warbond": "warbond",
    "债券": "warbond",
    "战争债券": "warbond",
    "all": "all",
    "全部": "all",
    "所有": "all",
    "公告": "all",
    "wiki": "wiki",
    "最近更改": "wiki",
    "最近": "wiki",
    "更改": "wiki",
    "编辑": "wiki",
    "balance": "balance",
    "数值": "balance",
    "数值调整": "balance",
    "平衡": "balance",
    "详细": "balance",
    "详情": "balance",
    # Letter short forms (v / pt / hf / wb / all / wk / bl).
    **dict(CATEGORY_SHORTCUTS),
}
PUSH_CATEGORY_LABELS = {
    "version": "版本更新",
    "patch": "补丁",
    "hotfix": "热修复",
    "warbond": "战争债券",
    "all": "最新公告",
    "wiki": "最近更改",
    "balance": "数值调整",
}
# Categories served straight from the wiki with nothing stored locally, so an
# on-demand request for them always costs an upstream call (see ``_force_push``).
LIVE_ONLY_PUSH_CATEGORIES = frozenset({"wiki", "balance"})
PUSH_USAGE_HINT = (
    "推送类型：version(版本更新，默认) patch(补丁) hotfix(热修复) warbond(战争债券) "
    "all(最新公告) wiki(Wiki 最近更改) balance(Wiki 数值调整)"
)
def _endpoint_or_default(value: Any, default: str) -> tuple[str, str]:
    """Validate a configurable endpoint, returning ``(url, rejected value)``.

    Only ``http``/``https`` are accepted: ``urllib`` would happily open a
    ``file://`` URL, which turns a configured endpoint into a local file read.
    A rejected value falls back to the shipped default instead of breaking the
    feed outright.
    """
    text = str(value or "").strip()
    if not text:
        return default, ""
    if text.lower().startswith(("http://", "https://")):
        return text, ""
    return default, text


def pad_display(text: str, width: int) -> str:
    """Left-align ``text`` in ``width`` terminal columns.

    ``str.ljust`` counts characters, but CJK glyphs occupy two columns, so
    ``格式化`` tables built with ``:<10`` end up ragged.
    """
    columns = sum(2 if unicodedata.east_asian_width(ch) in {"W", "F"} else 1 for ch in text)
    return text + " " * max(0, width - columns)


def human_seconds(seconds: int) -> str:
    """``43200`` -> ``43200 秒（12 小时）``, so help text can show the configured value."""
    if seconds >= 3600 and seconds % 3600 == 0:
        return f"{seconds} 秒（{seconds // 3600} 小时）"
    if seconds >= 60 and seconds % 60 == 0:
        return f"{seconds} 秒（{seconds // 60} 分钟）"
    return f"{seconds} 秒"


def usage_hint(
    *,
    poll_interval: int = DEFAULT_INTERVAL_SECONDS,
    push_cooldown: int = FORCE_PUSH_MIN_INTERVAL_SECONDS,
    reroll: int = LOADOUT_REROLL_TTL_SECONDS,
    subscribed_only: bool = True,
) -> str:
    """Full usage list.

    The tunables are rendered from the live config so the help can never disagree
    with what the plugin actually does.
    """
    tail = [
        "动作简写："
        + " / ".join(f"{shortcut} {ACTION_LABELS[action]}" for shortcut, action in LETTER_SHORTCUTS),
        "         也可以只写一个字："
        + " / ".join(shortcut for shortcut, _ in SINGLE_CHAR_SHORTCUTS),
        "类型简写："
        + " / ".join(f"{shortcut} {PUSH_CATEGORY_LABELS[category]}" for shortcut, category in CATEGORY_SHORTCUTS),
        f"管理指令：{'、'.join('/helldivers ' + name for name in sorted(ADMIN_ACTIONS))}"
        "（只有管理员 admin_users 能用；其余指令所有人可用，可单独加黑名单）",
        "可用主题：subscribe / unsubscribe / push / loadout / diag / list / status / help / shortcut",
        "          都支持中文，如 /helldivers 订阅、/helldivers 帮助 push",
        "也可以只写推送类型名，等价于 push 该类型，例如 /helldivers 补丁",
        f"自动推送只发送版本更新，每 {human_seconds(poll_interval)}检查一次；"
        f"push 的上游冷却为 {push_cooldown} 秒",
        f"触发词：{' / '.join('/' + name for name in COMMAND_TRIGGERS)}（完全等价）",
    ]
    if subscribed_only:
        tail.append("未订阅的群只能发 subscribe，订阅本群后才能使用其余指令")
    return (
        "用法：\n"
        "/helldivers subscribe   订阅本群\n"
        "/helldivers unsubscribe 取消订阅本群\n"
        "/helldivers push [类型] 立即检查并推送最近一期日志\n"
        "                        类型默认 version，可选 patch / hotfix / warbond / all / wiki / balance\n"
        "/helldivers loadout     生成一套随机配装并以图片发送\n"
        f"                        抽完 {reroll} 秒内可加序号 1-8 单独重抽该格\n"
        "/helldivers list        列出订阅群\n"
        "/helldivers status      查看公告与投递统计\n"
        "/helldivers diag        自检：渲染一张测试图，验证出图链路\n"
        "/helldivers help [主题] 显示本说明，带主题看单条命令详情\n" + "\n".join(tail)
    )


def topic_subscribe(plugin: Any) -> str:
    interval = plugin._cfg_int("poll_interval_seconds", DEFAULT_INTERVAL_SECONDS)
    lines = [
        "【/helldivers subscribe】订阅本群",
        f"订阅后本群会收到自动推送的版本更新，插件默认每 {human_seconds(interval)}检查一次更新源"
        "（在 config.toml 的 plugin.poll_interval_seconds 里调）。",
        "中文写法：/helldivers 订阅",
        "取消订阅：/helldivers unsubscribe",
    ]
    if plugin._cfg_bool("require_subscription", True):
        lines.append("未订阅的群只能发 subscribe，订阅后才能使用其余指令。")
    lines.append("也可以在 config.toml 的 plugin.default_groups 里直接填群号。")
    return "\n".join(lines)


def topic_shortcut(plugin: Any) -> str:
    letter_lines = [
        f"  {pad_display(shortcut, 8)}{pad_display(ACTION_LABELS[action], 12)}/hd {shortcut}"
        for shortcut, action in LETTER_SHORTCUTS
    ]
    single_line = " / ".join(f"{shortcut}={ACTION_LABELS[action]}" for shortcut, action in SINGLE_CHAR_SHORTCUTS)
    category_line = " ".join(f"{shortcut}={PUSH_CATEGORY_LABELS[category]}" for shortcut, category in CATEGORY_SHORTCUTS)
    lines = [
        "【/helldivers 简写】触发词与简写（怎么少打字）",
        f"触发词：{' / '.join('/' + name for name in COMMAND_TRIGGERS)} 完全等价，",
        "        例如 /hd 状态 与 /helldivers 状态 是同一个命令。",
        "字母简写：",
        *letter_lines,
        f"单字中文：{single_line}",
        f"推送类型简写：{category_line}",
        "  例：/hd 推 hf 等价于 /helldivers push hotfix",
        "中文写法：/helldivers 简写（也可写 shortcut / 快捷 / 缩写）",
    ]
    if plugin._cfg_bool("require_subscription", True):
        lines.append("注意：未订阅的群只能发 subscribe（简写 sub / 订）。")
    return "\n".join(lines)


def topic_push(plugin: Any) -> str:
    cooldown = plugin._push_cooldown_seconds()
    interval = plugin._cfg_int("poll_interval_seconds", DEFAULT_INTERVAL_SECONDS)
    recent = plugin._cfg_int("wiki_recent_limit", WIKI_RECENT_LIMIT)
    return (
        "【/helldivers push [类型]】立即推送，跳过定时轮询\n"
        f"不带类型时推送最近一期版本更新，并把本轮新出现的公告一并推给本群"
        f"（正常轮询是每 {human_seconds(interval)}一次）。\n"
        "指定类型时只发这一条（其余新公告留给下一次定时检查），可选类型：\n"
        "  version  版本更新（默认）标题带版本号，或官方标记为 patch notes\n"
        "  patch    补丁\n"
        "  hotfix   热修复\n"
        "  warbond  战争债券\n"
        "  all      最新公告（不限类型）\n"
        f"  wiki     最近更改（Wiki 中文站优先，最新 {recent} 条编辑）\n"
        "  balance  数值调整（该期补丁页的平衡性段落）\n"
        "中文写法：版本 / 补丁 / 热修复 / 战争债券 / 全部 / 最近更改 / 数值\n"
        "例：/helldivers push hotfix  或  /helldivers 数值\n"
        f"本群未订阅也可以用它查看；两次「立即检查」之间至少间隔 {cooldown} 秒，\n"
        "间隔内不再请求上游：版本/补丁等改用已存库的最新一期回复，\n"
        "wiki / 数值因为内容只能现取，会提示冷却中（操作员不受限）。"
    )


def topic_loadout(plugin: Any) -> str:
    reroll = plugin._loadout_reroll_seconds()
    return (
        "【/helldivers loadout】随机生成一套配装并发图\n"
        "从主武器、副武器、投掷物、强化资源各随机一件，再随机四件互不重复的战略配备，\n"
        "渲染成一张配装卡片图发送；宿主不支持出图时自动回退为文字。\n"
        f"卡片左上角的编号就是槽位号，抽完之后 {reroll} 秒内可以只重抽其中一格：\n"
        f"  /helldivers loadout 3   只重抽第 3 格（也可以在 {reroll} 秒内一直重抽下去）\n"
        f"槽位号：{LOADOUT_SLOT_HINT}\n"
        f"每重抽一次倒计时重新开始；时间窗可在 config.toml 的 plugin.loadout_reroll_seconds 调整。\n"
        "超过时间或还没抽过时，重抽会提示你先抽一套。\n"
        "中文写法：/helldivers 配装 或 /helldivers 随机配装\n"
        "装备清单来自项目根目录的 loadout_data.json，可自行增删。"
    )

HELP_TOPICS = {
    "subscribe": topic_subscribe,
    "unsubscribe": (
        "【/helldivers unsubscribe】取消订阅本群\n"
        "取消后本群不再收到自动推送，该群尚未发出的投递记录会一并释放；\n"
        "重新订阅不会补发中断期间的旧公告。\n"
        "中文写法：/helldivers 取消订阅"
    ),
    "push": topic_push,
    "loadout": topic_loadout,
    "diag": (
        "【/helldivers diag】自检出图链路（管理指令）\n"
        "依次检查配装数据、装备图标是否加载，然后真的渲染一张测试图并尝试发送。\n"
        "渲染成功就说明配装卡片也能正常出图；失败会直接把宿主返回的原因打出来，"
        "常见的有「浏览器渲染能力已禁用」（MaiBot 配置 plugin_runtime.render.enabled）"
        "和「未安装 Playwright」。\n"
        "会暴露插件配置与出图链路细节，所以只有管理员（admin_users）能用。\n"
        "中文写法：/helldivers 诊断 / 自检 / 测试"
    ),
    "list": (
        "【/helldivers list】列出接收推送的群（管理指令）\n"
        "标记（本群）的就是当前群，（配置）表示来自 config.toml 的 default_groups。\n"
        "会显示全部群号，所以只有管理员（admin_users）能用。\n"
        "中文写法：/helldivers 列表"
    ),
    "status": (
        "【/helldivers status】查看运行状态\n"
        "显示本群是否已订阅、订阅群数量，以及公告总数与投递情况：\n"
        "已发送 / 待发送 / 失败 / 无需推送。\n"
        "「无需推送」包含首轮建立的历史基线、没有订阅群时采集到的公告，\n"
        "以及按规则不自动推送的非版本公告（它们仍可用 push 手动取出）。\n"
        "中文写法：/helldivers 状态"
    ),
    "help": (
        "【/helldivers help [主题]】显示用法\n"
        "不带主题显示全部命令；带主题查看单条命令详情，例如：\n"
        "  /helldivers help push\n"
        "  /helldivers help loadout\n"
        "可用的主题：subscribe / unsubscribe / push / loadout / diag / list / status / help / shortcut\n"
        "中文写法：/helldivers 帮助，主题也可以是中文（如 /helldivers help 配装）\n"
        "推送类型名会归到 push 主题，例如 /helldivers help 补丁\n"
        "简写清单见 /helldivers help 简写（也可写 shortcut / 快捷 / 缩写）"
    ),
    "shortcut": topic_shortcut,
}
# Topic aliases that are *not* actions: ``help 简写`` should reach the shortcut
# topic without registering 简写 as a runnable sub-command.
HELP_ONLY_ALIASES = {
    "简写": "shortcut",
    "快捷": "shortcut",
    "缩写": "shortcut",
    "快捷方式": "shortcut",
    "简称": "shortcut",
}


@dataclass(frozen=True)
class UpdateEntry:
    source: str
    source_id: str
    title: str
    summary: str
    url: str
    published_at: datetime
    category: str = "update"
    content_hash: str = ""
    source_feed: str = ""
    tags: tuple[str, ...] = ()
    # Localized text shown instead of title/summary when the developer published one.
    # Deliberately excluded from content_hash so enriched and plain collections of the
    # same announcement keep deduplicating to a single row.
    title_zh: str = ""
    summary_zh: str = ""

    def __post_init__(self) -> None:
        if not self.content_hash:
            body = f"{self.title}\n{self.summary}".encode("utf-8")
            object.__setattr__(self, "content_hash", hashlib.sha256(body).hexdigest())

    @property
    def key(self) -> str:
        return f"{self.source}:{self.source_id}:{self.content_hash}"


def _to_datetime(value: Any) -> datetime:
    """Parse a source timestamp and normalise it to UTC.

    Normalising keeps the stored ISO strings sortable, which ``SQLiteStore``
    relies on to pick the newest entry.
    """
    if isinstance(value, datetime):
        moment = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return moment.astimezone(timezone.utc)
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, timezone.utc)
    text = str(value or "").strip()
    if text:
        try:
            moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
            return moment.astimezone(timezone.utc) if moment.tzinfo else moment.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return datetime.now(timezone.utc)


def clean_summary(value: Any, limit: int = 320) -> str:
    """Remove Steam markup/HTML and collapse whitespace for chat messages."""
    text = html.unescape(str(value or ""))
    text = re.sub(r"\[(?:/?(?:b|i|u|url(?:=[^\]]+)?|h[1-6]))\]", "", text, flags=re.I)
    text = re.sub(r"\[[^\]]+\]", "", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > limit:
        text = text[: max(0, limit - 1)].rstrip() + "…"
    return text


def _category(title: str, text: str = "") -> str:
    lowered = f"{title} {text}".lower()
    if "warbond" in lowered or "战争债券" in lowered:
        return "warbond"
    if "hotfix" in lowered or "热修复" in lowered:
        return "hotfix"
    if "patch" in lowered or "补丁" in lowered:
        return "patch"
    return "update"


def is_relevant(entry: UpdateEntry) -> bool:
    return bool(RELEVANT_RE.search(f"{entry.title} {entry.summary}"))


def is_steam_media(entry: UpdateEntry) -> bool:
    """Third-party press bundled into the Steam news payload."""
    return entry.source == "steam" and bool(entry.source_feed) and entry.source_feed != OFFICIAL_STEAM_FEED


def is_official_announcement(entry: UpdateEntry) -> bool:
    """Developer-published announcement (or a caller that predates feed tracking)."""
    return not is_steam_media(entry)


def is_version_update(entry: UpdateEntry) -> bool:
    """Release announcement: carries a build number or is tagged as patch notes."""
    if not is_official_announcement(entry) or entry.source != "steam":
        return False
    return bool(VERSION_RE.search(entry.title)) or "patchnotes" in entry.tags


def parse_steam_news(payload: Mapping[str, Any]) -> list[UpdateEntry]:
    """Parse ``GetNewsForApp`` JSON, tolerating missing optional fields."""
    root = payload.get("appnews", payload)
    items = root.get("newsitems", []) if isinstance(root, Mapping) else []
    result: list[UpdateEntry] = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, Mapping):
            continue
        title = str(item.get("title") or "").strip()
        if not title:
            continue
        source_id = str(item.get("gid") or item.get("id") or item.get("url") or title)
        summary = clean_summary(item.get("contents") or item.get("feedlabel") or "")
        url = str(item.get("url") or "")
        raw_tags = item.get("tags")
        tags = tuple(str(tag) for tag in raw_tags) if isinstance(raw_tags, (list, tuple)) else ()
        result.append(
            UpdateEntry(
                "steam",
                source_id,
                title,
                summary,
                url,
                _to_datetime(item.get("date")),
                _category(title, summary),
                source_feed=str(item.get("feedname") or ""),
                tags=tags,
            )
        )
    return result


def _wiki_url(title: str) -> str:
    return WIKI_BASE_URL + urllib.parse.quote(title.replace(" ", "_"), safe="()/_-")


def parse_wiki_entries(payload: Mapping[str, Any]) -> list[UpdateEntry]:
    """Parse common MediaWiki ``recentchanges`` and ``query.pages`` shapes."""
    query = payload.get("query", payload)
    raw: Any = query.get("recentchanges", []) if isinstance(query, Mapping) else []
    if not raw and isinstance(query, Mapping):
        pages = query.get("pages", {})
        if isinstance(pages, Mapping):
            raw = list(pages.values())
    result: list[UpdateEntry] = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, Mapping):
            continue
        title = str(item.get("title") or "").strip()
        if not title:
            continue
        source_id = str(item.get("rcid") or item.get("revid") or item.get("pageid") or title)
        revisions = item.get("revisions")
        text = item.get("comment") or item.get("summary") or ""
        if isinstance(revisions, list) and revisions and isinstance(revisions[0], Mapping):
            rev = revisions[0]
            slots = rev.get("slots", {})
            main = slots.get("main", {}) if isinstance(slots, Mapping) else {}
            text = rev.get("content") or rev.get("*" ) or main.get("*") or main.get("content") or text
        summary = clean_summary(text)
        timestamp = item.get("timestamp") or item.get("touched")
        result.append(
            UpdateEntry(
                "wiki",
                source_id,
                title,
                summary,
                str(item.get("url") or _wiki_url(title)),
                _to_datetime(timestamp),
                _category(title, summary),
                source_feed="wiki",
            )
        )
    return result


def parse_localized_steam_feed(xml_text: str) -> dict[str, tuple[str, str]]:
    """Map build number -> (localized title, localized summary) from the Steam news RSS.

    Only entries the developer actually translated are kept; the remaining items
    mirror the English text and are ignored. Matching is by build number because the
    RSS guids and the GetNewsForApp gids live in different id spaces.
    """
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return {}
    localized: dict[str, tuple[str, str]] = {}
    for item in root.iter("item"):
        title = str(item.findtext("title") or "").strip()
        version = VERSION_RE.search(title)
        if version is None or not CHINESE_RE.search(title):
            continue
        localized.setdefault(version.group(0), (title, clean_summary(item.findtext("description") or "")))
    return localized


def _with_localized_text(entry: UpdateEntry, localized: Mapping[str, tuple[str, str]]) -> UpdateEntry:
    """Attach the developer-published localized title/body when one exists."""
    if entry.source != "steam" or (entry.title_zh and entry.summary_zh):
        return entry
    version = VERSION_RE.search(entry.title)
    if version is None:
        return entry
    found = localized.get(version.group(0))
    if found is None:
        return entry
    return replace(entry, title_zh=found[0], summary_zh=found[1])


def deduplicate_entries(entries: Iterable[UpdateEntry]) -> list[UpdateEntry]:
    """Deduplicate identical source records while retaining content changes."""
    selected: dict[tuple[str, str, str], UpdateEntry] = {}
    for entry in entries:
        # A stable source ID may be edited in place; a changed hash is a new
        # update event and must remain deliverable.
        key = (entry.source, entry.source_id, entry.content_hash)
        old = selected.get(key)
        if old is None or entry.published_at >= old.published_at:
            selected[key] = entry
    return sorted(selected.values(), key=lambda e: e.published_at)


class SQLiteStore:
    """Persistent entries, subscriptions and per-group delivery state."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS entries (
              entry_key TEXT PRIMARY KEY, source TEXT NOT NULL, source_id TEXT NOT NULL,
              content_hash TEXT NOT NULL, title TEXT NOT NULL, summary TEXT NOT NULL,
              url TEXT NOT NULL, published_at TEXT NOT NULL, category TEXT NOT NULL,
              status TEXT NOT NULL DEFAULT 'pending', first_seen TEXT NOT NULL,
              source_feed TEXT NOT NULL DEFAULT '', tags TEXT NOT NULL DEFAULT '',
              title_zh TEXT NOT NULL DEFAULT '', summary_zh TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_entries_source ON entries(source, source_id);
            CREATE INDEX IF NOT EXISTS idx_entries_published ON entries(published_at DESC);
            CREATE TABLE IF NOT EXISTS subscriptions (group_id TEXT PRIMARY KEY, created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS deliveries (
              entry_key TEXT NOT NULL, group_id TEXT NOT NULL, status TEXT NOT NULL,
              attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT NOT NULL DEFAULT '',
              PRIMARY KEY(entry_key, group_id), FOREIGN KEY(entry_key) REFERENCES entries(entry_key)
            );
            """
        )
        self.conn.commit()
        self._migrate()

    def _migrate(self) -> None:
        """Bring a database created by an older version up to the current schema."""
        columns = {str(row[1]) for row in self.conn.execute("PRAGMA table_info(entries)")}
        if "source_feed" not in columns:
            self.conn.execute("ALTER TABLE entries ADD COLUMN source_feed TEXT NOT NULL DEFAULT ''")
            # Steam carries the origin feed in the URL, so legacy rows can still be
            # split into developer announcements and third-party press.
            self.conn.execute(
                "UPDATE entries SET source_feed = CASE WHEN url LIKE ? THEN ? ELSE 'steam_third_party' END "
                "WHERE source='steam'",
                (f"%{OFFICIAL_STEAM_FEED}%", OFFICIAL_STEAM_FEED),
            )
        if "tags" not in columns:
            self.conn.execute("ALTER TABLE entries ADD COLUMN tags TEXT NOT NULL DEFAULT ''")
        if "title_zh" not in columns:
            self.conn.execute("ALTER TABLE entries ADD COLUMN title_zh TEXT NOT NULL DEFAULT ''")
        if "summary_zh" not in columns:
            self.conn.execute("ALTER TABLE entries ADD COLUMN summary_zh TEXT NOT NULL DEFAULT ''")
        self.conn.commit()

    def has_entries(self) -> bool:
        return self.conn.execute("SELECT 1 FROM entries LIMIT 1").fetchone() is not None

    def upsert(self, entry: UpdateEntry, status: str = "pending") -> bool:
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO entries(entry_key,source,source_id,content_hash,title,summary,url,published_at,category,status,first_seen,source_feed,tags,title_zh,summary_zh) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,datetime('now'),?,?,?,?)",
            (
                entry.key,
                entry.source,
                entry.source_id,
                entry.content_hash,
                entry.title,
                entry.summary,
                entry.url,
                entry.published_at.isoformat(),
                entry.category,
                status,
                entry.source_feed,
                ",".join(entry.tags),
                entry.title_zh,
                entry.summary_zh,
            ),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def ensure_deliveries(self, entry_key: str, groups: Iterable[str]) -> None:
        """Open delivery rows for a freshly stored entry.

        Only called when an entry is first seen: groups that subscribe later
        deliberately do not receive the backlog, so their rows are never created.
        """
        for gid in groups:
            self.conn.execute("INSERT OR IGNORE INTO deliveries(entry_key,group_id,status) VALUES(?,?,?)", (entry_key, str(gid), "pending"))
        self.conn.commit()

    def delivery(self, entry_key: str, group_id: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM deliveries WHERE entry_key=? AND group_id=?", (entry_key, str(group_id))).fetchone()

    def group_delivery_attempts(self, group_id: str) -> dict[str, int]:
        """Snapshot per-entry attempt counts so a caller can spot fresh sends."""
        return {
            str(row["entry_key"]): int(row["attempts"])
            for row in self.conn.execute(
                "SELECT entry_key, attempts FROM deliveries WHERE group_id=?", (str(group_id),)
            )
        }

    def latest_entries(self, limit: int = PUSH_SCAN_LIMIT) -> list[sqlite3.Row]:
        """Stored announcements, newest first, for on-demand lookups."""
        return list(
            self.conn.execute(
                "SELECT * FROM entries ORDER BY published_at DESC, first_seen DESC LIMIT ?", (int(limit),)
            )
        )

    def _refresh_entry_status(self, entry_key: str) -> str:
        """Derive an entry's status from its delivery rows.

        ``skipped`` means the entry has no recipient left (it was collected while
        nobody was subscribed, or every pending group unsubscribed), so it must
        not keep counting as pending.
        """
        rows = self.conn.execute("SELECT status FROM deliveries WHERE entry_key=?", (entry_key,)).fetchall()
        statuses = [str(row[0]) for row in rows]
        if not statuses:
            status = "skipped"
        elif any(value == "failed" for value in statuses):
            status = "failed"
        elif any(value == "pending" for value in statuses):
            status = "pending"
        else:
            status = "sent"
        self.conn.execute("UPDATE entries SET status=? WHERE entry_key=?", (status, entry_key))
        return status

    def mark_delivery(self, entry_key: str, group_id: str, ok: bool, error: str = "") -> None:
        self.conn.execute(
            "UPDATE deliveries SET status=?,attempts=attempts+1,last_error=? WHERE entry_key=? AND group_id=?",
            ("sent" if ok else "failed", error[:500], entry_key, str(group_id)),
        )
        self._refresh_entry_status(entry_key)
        self.conn.commit()

    def reconcile_statuses(self) -> int:
        """Repair entries that carry a delivery status but no delivery rows.

        Databases written before delivery rows were tracked can hold entries stuck
        at ``pending``/``failed`` that can never be delivered; mark them skipped so
        the counters stay truthful. ``baseline`` entries are left untouched.
        """
        cur = self.conn.execute(
            "UPDATE entries SET status='skipped' WHERE status IN ('pending','failed') "
            "AND NOT EXISTS (SELECT 1 FROM deliveries d WHERE d.entry_key=entries.entry_key)"
        )
        self.conn.commit()
        return cur.rowcount

    def subscribe(self, group_id: str) -> None:
        self.conn.execute("INSERT OR IGNORE INTO subscriptions(group_id,created_at) VALUES(?,datetime('now'))", (str(group_id),))
        self.conn.commit()

    def unsubscribe(self, group_id: str) -> bool:
        """Drop a subscription and any delivery it still owes."""
        gid = str(group_id)
        cur = self.conn.execute("DELETE FROM subscriptions WHERE group_id=?", (gid,))
        removed = cur.rowcount > 0
        orphans = [
            str(row[0])
            for row in self.conn.execute(
                "SELECT DISTINCT entry_key FROM deliveries WHERE group_id=? AND status!='sent'", (gid,)
            )
        ]
        if orphans:
            self.conn.execute("DELETE FROM deliveries WHERE group_id=? AND status!='sent'", (gid,))
            for entry_key in orphans:
                self._refresh_entry_status(entry_key)
        self.conn.commit()
        return removed

    def groups(self) -> list[str]:
        return [str(r[0]) for r in self.conn.execute("SELECT group_id FROM subscriptions ORDER BY group_id")]

    def counts(self) -> dict[str, int]:
        row = self.conn.execute(
            "SELECT COUNT(*) entries, SUM(status='sent') sent, SUM(status='pending') pending, "
            "SUM(status='failed') failed, SUM(status='skipped') skipped, "
            "SUM(status='muted') muted, SUM(status='baseline') baseline FROM entries"
        ).fetchone()
        return {
            k: int(row[k] or 0)
            for k in ("entries", "sent", "pending", "failed", "skipped", "muted", "baseline")
        }

    def close(self) -> None:
        self.conn.close()


class PluginSection(PluginConfigBase):
    """绝地潜兵 2 更新推送的全部可调项。

    分五组：

    * **基础** —— 开关、轮询间隔、默认推送群。
    * **权限** —— 管理指令白名单与功能性指令黑名单。
    * **数据源** —— Steam / Wiki 的开关、接口地址与请求超时。
    * **推送与限流** —— 手动 push 的上游冷却、Wiki 内容长度与时效。
    * **随机配装** —— 单项重抽的时间窗。

    改完保存即可热生效（改轮询间隔会重启轮询任务）；`config_version` 由 Runner 维护，
    不要手工改小，否则新增字段不会被补齐。
    """

    # ``coerce_numbers_to_str`` so a QQ number written as a bare TOML integer
    # (``admin_users = [123456789]``) is accepted instead of failing validation
    # -- the SDK base only sets ``validate_assignment`` and ``extra``.
    model_config = ConfigDict(validate_assignment=True, extra="ignore", coerce_numbers_to_str=True)

    __ui_label__ = "Helldivers 2 更新推送"
    __ui_icon__ = "satellite"
    __ui_order__ = 10

    config_version: str = Field(
        default="1.0.0",
        description="配置结构版本号，由 Runner 生成与维护；升级插件时用于补齐新增字段。",
        json_schema_extra={"label": "配置版本", "group": "基础", "order": 0, "hidden": True},
    )
    enabled: bool = Field(
        default=True,
        description="总开关。关闭后不再启动轮询任务，但群内命令（订阅、推送、配装等）仍然可用。",
        json_schema_extra={"label": "启用插件", "group": "基础", "order": 1, "hint": "关闭后自动推送停止，命令不受影响"},
    )
    poll_interval_seconds: int = Field(
        default=DEFAULT_INTERVAL_SECONDS,
        ge=60,
        le=604800,
        description=(
            "自动检查更新源的间隔（秒）。默认 43200（12 小时）；"
            "Steam News 有速率限制，不建议低于 900（15 分钟）。"
        ),
        json_schema_extra={
            "label": "轮询间隔（秒）",
            "group": "基础",
            "order": 2,
            "placeholder": "43200",
            "example": 21600,
        },
    )
    default_groups: list[str] = Field(
        default_factory=list,
        description=(
            "始终接收自动推送的群号列表，与群内 /helldivers subscribe 的订阅表合并去重。"
            "适合管理员直接指定；留空则完全由群内自助订阅控制。"
        ),
        json_schema_extra={
            "label": "默认推送群",
            "group": "基础",
            "order": 3,
            "hint": "每行一个群号",
            "example": ["123456789"],
        },
    )
    require_subscription: bool = Field(
        default=True,
        description=(
            "只允许已订阅的群使用全部指令；未订阅的群只能发 subscribe。"
            "关闭后任何群都能使用所有指令（内容仍只推送给已订阅的群）。"
        ),
        json_schema_extra={
            "label": "仅订阅群可用指令",
            "group": "基础",
            "order": 4,
            "hint": "关闭后未订阅的群也能使用指令",
        },
    )
    admin_users: list[str] = Field(
        default_factory=list,
        description=(
            "管理指令（list / diag）的白名单 QQ 号。功能性指令（订阅/推送/配装/状态/帮助）"
            "默认所有人都能用，不需要写在这里。宿主操作员始终拥有管理权限。"
        ),
        json_schema_extra={
            "label": "管理员 QQ 号（白名单）",
            "group": "权限",
            "order": 5,
            "hint": "每行一个 QQ 号；只有这里的人能用管理指令",
            "example": ["123456789"],
        },
    )
    command_blacklist: dict[str, list[str]] = Field(
        default_factory=dict,
        description=(
            "功能性指令的黑名单：键是指令名（subscribe / unsubscribe / push / loadout / "
            "status / help，或 * 表示全部功能性指令），值是禁止使用该指令的 QQ 号列表。"
            "管理指令不受此表影响（它们本来就是白名单制）；管理员也不会被此表拦住。"
        ),
        json_schema_extra={
            "label": "指令黑名单",
            "group": "权限",
            "order": 6,
            "hint": '例如 {"*": ["10001"], "push": ["10002"]}',
            "example": {"push": ["10001"]},
        },
    )

    steam_enabled: bool = Field(
        default=True,
        description="启用 Steam 新闻来源（官方公告 + 官方中文 RSS 增强）。",
        json_schema_extra={"label": "Steam 来源", "group": "数据源", "order": 10},
    )
    steam_app_id: int = Field(
        default=APP_ID,
        ge=1,
        le=10_000_000,
        description="Steam App ID。默认 553850（HELLDIVERS™ 2），一般不需要改。",
        json_schema_extra={
            "label": "Steam App ID",
            "group": "数据源",
            "order": 11,
            "depends_on": "steam_enabled",
            "depends_value": True,
            "hint": "改错会采到别的游戏",
        },
    )
    steam_endpoint: str = Field(
        default=STEAM_NEWS_URL,
        description="Steam News 接口地址。可换成镜像或自建代理。",
        json_schema_extra={
            "label": "Steam 接口地址",
            "group": "数据源",
            "order": 12,
            "depends_on": "steam_enabled",
            "depends_value": True,
            "placeholder": STEAM_NEWS_URL,
            "rows": 2,
        },
    )
    steam_locale: str = Field(
        default=DEFAULT_STEAM_LOCALE,
        description=(
            "Steam 新闻 RSS 的语言，用于取官方中文标题与正文（GetNewsForApp 接口不支持本地化）。"
            "官方只给部分版本发了中文版，缺失时自动回退英文；置空字符串可关闭这个额外请求。"
        ),
        json_schema_extra={
            "label": "中文源语言",
            "group": "数据源",
            "order": 13,
            "depends_on": "steam_enabled",
            "depends_value": True,
            "placeholder": "schinese",
            "hint": "置空关闭中文增强",
        },
    )
    wiki_enabled: bool = Field(
        default=False,
        description=(
            "把 Wiki 的编辑动态（recentchanges）作为自动采集来源。实测它没有可用产出、"
            "还每轮多打一次接口，所以默认关闭；Wiki 内容改用 /helldivers push wiki 与 push balance 按需获取。"
        ),
        json_schema_extra={"label": "Wiki 自动来源", "group": "数据源", "order": 14},
    )
    wiki_endpoint: str = Field(
        default=WIKI_API_URL,
        description="Wiki API 地址（默认英文站；中文站是同一地址下的 /zh/api.php）。",
        json_schema_extra={
            "label": "Wiki 接口地址",
            "group": "数据源",
            "order": 15,
            "placeholder": WIKI_API_URL,
            "rows": 2,
        },
    )
    request_timeout_seconds: float = Field(
        default=20.0,
        ge=1,
        le=120,
        description="单次 HTTP 请求的超时（秒），对 Steam / Wiki 都生效。",
        json_schema_extra={"label": "请求超时（秒）", "group": "数据源", "order": 16, "step": 1},
    )

    force_push_cooldown_seconds: int = Field(
        default=FORCE_PUSH_MIN_INTERVAL_SECONDS,
        ge=0,
        le=3600,
        description=(
            "两次 /helldivers push 之间对上游的最小请求间隔（秒）。"
            "冷却期内命令仍然回复，但只重发库里已存的最新一期、不再请求上游；"
            "操作员（is_local_operator）不受此限制。"
        ),
        json_schema_extra={"label": "push 冷却（秒）", "group": "推送与限流", "order": 20, "example": 120},
    )
    wiki_detail_max_age_days: int = Field(
        default=WIKI_DETAIL_MAX_AGE_DAYS,
        ge=0,
        le=3650,
        description=(
            "自动推送时，为发布多久以内的版本公告去反查 Wiki 详情页链接（天）。"
            "wiki.gg 限流很严，这个窗口能避免为历史公告批量打接口；0 表示不附链接。"
        ),
        json_schema_extra={"label": "Wiki 详情链接时效（天）", "group": "推送与限流", "order": 21},
    )
    wiki_recent_limit: int = Field(
        default=WIKI_RECENT_LIMIT,
        ge=1,
        le=50,
        description=(
            "Wiki「最近更改」的拉取条数，/helldivers push wiki 与 Wiki 自动采集共用。"
            "条数越多消息越长，上限 50。"
        ),
        json_schema_extra={"label": "最近更改条数", "group": "推送与限流", "order": 22},
    )
    wiki_digest_max_chars: int = Field(
        default=WIKI_DIGEST_LIMIT,
        ge=100,
        le=4000,
        description=(
            "/helldivers push balance 输出的数值调整摘录长度上限（字符）。"
            "过长的内容会刷屏，建议不超过 2000。"
        ),
        json_schema_extra={"label": "数值摘录长度", "group": "推送与限流", "order": 23},
    )

    loadout_reroll_seconds: int = Field(
        default=LOADOUT_REROLL_TTL_SECONDS,
        ge=10,
        le=3600,
        description=(
            "抽完配装后还能用 /helldivers loadout <1-8> 单独重抽的时间窗（秒）。"
            "每重抽一次倒计时重新开始，可以连着抽到满意为止；超时后需要重新抽一整套。"
        ),
        json_schema_extra={"label": "配装重抽时间窗（秒）", "group": "随机配装", "order": 30, "example": 300},
    )


class PluginConfig(PluginConfigBase):
    plugin: PluginSection = Field(default_factory=PluginSection)


def _parse_config(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        return {}
    value = raw.get("plugin", raw)
    return dict(value) if isinstance(value, Mapping) else {}


def _load_local_config() -> dict[str, Any]:
    """Load a colocated config.toml when MaiBot has not injected settings."""
    path = Path(__file__).with_name("config.toml")
    if not path.exists():
        return {}
    try:
        import tomllib
        with path.open("rb") as handle:
            return _parse_config(tomllib.load(handle))
    except Exception:
        return {}


class HelldiversPlugin(MaiBotPlugin):
    config_model = PluginConfig

    def __init__(self, *args: Any, **kwargs: Any):
        try:
            super().__init__(*args, **kwargs)
        except TypeError:
            super().__init__()
        self._task: asyncio.Task[Any] | None = None
        self._store: SQLiteStore | None = None
        self._cfg: dict[str, Any] = {}
        self._first_run = True
        self._last_collect_had_source = False
        self._last_force_check = float("-inf")
        self._wiki_pages: dict[str, str] = {}
        # scope -> (loadout, motto, monotonic stamp); powers ``loadout <1-8>``.
        self._loadout_sessions: dict[str, tuple[dict[str, str], str, float]] = {}
        # Why the last card render / image send failed; empty after a success.
        self._last_render_error = ""

    async def on_load(self) -> None:
        raw: Any = {}
        try:
            raw = self.get_plugin_config_data()  # type: ignore[attr-defined]
        except Exception:
            pass
        self._cfg = _parse_config(raw) or _load_local_config()
        paths = getattr(getattr(self, "ctx", None), "paths", None)
        data_dir = Path(getattr(paths, "data_dir", Path(__file__).parent / "data"))
        self._store = SQLiteStore(data_dir / "helldivers_patch_feed.db")
        self._first_run = not self._store.has_entries()
        if self._cfg.get("enabled", True):
            self._start_poll_task()

    async def on_unload(self) -> None:
        task = self._task
        self._stop_poll_task()
        if task is not None and not task.done():
            try:
                await task
            except asyncio.CancelledError:
                pass
        if self._store:
            self._store.close()
            self._store = None

    async def on_config_update(self, scope: str = CONFIG_RELOAD_SCOPE_SELF, config_data: Any = None, version: str = "") -> None:
        del version
        if isinstance(scope, Mapping):
            config_data, scope = scope, CONFIG_RELOAD_SCOPE_SELF
        if scope not in (CONFIG_RELOAD_SCOPE_SELF, "self", ""):
            return
        self._cfg = _parse_config(config_data) or _load_local_config()
        old_task = self._task
        self._stop_poll_task()
        if old_task is not None and not old_task.done():
            try:
                await old_task
            except asyncio.CancelledError:
                pass
        if self._cfg.get("enabled", True):
            self._start_poll_task()

    def _start_poll_task(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._poll_loop())

    def _stop_poll_task(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
        self._task = None

    async def _poll_loop(self) -> None:
        while True:
            try:
                await self.poll_once()
                # Read through ``_cfg_int``: a hand-edited config.toml can hold a
                # non-numeric interval, and ``int()`` raising here would be caught
                # by the handler below -- turning the 12 h cycle into a 60 s retry
                # that hammers the upstream feeds.
                delay = self._cfg_int("poll_interval_seconds", DEFAULT_INTERVAL_SECONDS, 60)
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._log("exception", "Helldivers poll failed: %s", exc)
                await asyncio.sleep(60)

    async def _fetch_json(self, url: str, params: Mapping[str, Any] | None = None) -> Mapping[str, Any]:
        if params:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
        timeout = float(self._cfg.get("request_timeout_seconds", 20))
        try:
            import httpx  # type: ignore
            async with httpx.AsyncClient(timeout=timeout, headers={"User-Agent": USER_AGENT}) as client:
                response = await client.get(url)
                response.raise_for_status()
                data = response.json()
                return data if isinstance(data, Mapping) else {}
        except ImportError:
            def request() -> Mapping[str, Any]:
                with urllib.request.urlopen(url, timeout=timeout) as response:
                    return json.loads(response.read().decode("utf-8"))
            return await asyncio.to_thread(request)

    async def _fetch_text(self, url: str) -> str:
        timeout = float(self._cfg.get("request_timeout_seconds", 20))
        try:
            import httpx  # type: ignore
            async with httpx.AsyncClient(timeout=timeout, headers={"User-Agent": USER_AGENT}) as client:
                response = await client.get(url)
                response.raise_for_status()
                return response.text
        except ImportError:
            def request() -> str:
                with urllib.request.urlopen(url, timeout=timeout) as response:
                    return response.read().decode("utf-8", "replace")
            return await asyncio.to_thread(request)

    async def _localized_steam_titles(self) -> dict[str, tuple[str, str]]:
        """Localized title/body per build number; empty when disabled or unavailable."""
        locale = str(self._cfg.get("steam_locale", DEFAULT_STEAM_LOCALE) or "").strip()
        if not locale or not self._cfg.get("steam_enabled", True):
            return {}
        endpoint = STEAM_NEWS_RSS_URL.format(app_id=self._steam_app_id())
        url = f"{endpoint}?l={urllib.parse.quote(locale)}&cc=CN"
        try:
            return parse_localized_steam_feed(await self._fetch_text(url))
        except Exception as exc:  # localization is an enhancement, never a blocker
            self._log("warning", "Helldivers localized feed unavailable: %s", exc)
            return {}

    async def _wiki_page_for(self, title: str) -> tuple[str, str, str]:
        """``(page, api_url, base_url)`` for an announcement, Chinese wiki first.

        The Chinese sub-wiki is preferred but lags behind, so the English wiki is
        searched when it has no page yet.
        """
        cached = self._wiki_pages.get(title)
        if cached is not None:
            return cached
        for api_url, base_url in self._wiki_sources():
            for query in (f'"{title}"', title):
                try:
                    payload = await self._fetch_json(
                        api_url,
                        {"action": "query", "list": "search", "srsearch": query, "srlimit": 1, "format": "json"},
                    )
                except Exception as exc:
                    # Both wikis sit behind the same rate limiter, so a failure here
                    # means "stop asking", not "try the other one".
                    self._log("warning", "Helldivers wiki search failed: %s", exc)
                    return "", "", ""
                hits = payload.get("query", {}).get("search", []) if isinstance(payload, Mapping) else []
                if isinstance(hits, list) and hits and isinstance(hits[0], Mapping):
                    found = (str(hits[0].get("title") or ""), api_url, base_url)
                    # Only successes are cached: a transient 429 or timeout must not
                    # disable the detail link for that announcement for the rest of
                    # the process.
                    self._wiki_pages[title] = found
                    return found
        return "", "", ""

    async def _wiki_detail_url(self, entry: UpdateEntry) -> str:
        """Link to the wiki page carrying the full balance numbers, when one exists."""
        if not is_version_update(entry):
            return ""
        age = datetime.now(timezone.utc) - entry.published_at.astimezone(timezone.utc)
        if age > timedelta(days=self._cfg_int("wiki_detail_max_age_days", WIKI_DETAIL_MAX_AGE_DAYS, 0)):
            return ""
        page, _api_url, base_url = await self._wiki_page_for(entry.title)
        return _wiki_url(page, base_url) if page else ""

    async def _wiki_recent_changes_digest(self) -> str:
        """The wiki's recent-changes feed: what was edited lately, newest first."""
        for api_url, base_url in self._wiki_sources():
            try:
                payload = await self._fetch_json(
                    api_url,
                    {
                        "action": "query",
                        "list": "recentchanges",
                        "rcnamespace": 0,
                        "rclimit": self._cfg_int("wiki_recent_limit", WIKI_RECENT_LIMIT, 1),
                        "rctype": "edit|new",
                        "rcprop": "title|ids|timestamp|comment",
                        "format": "json",
                    },
                )
            except Exception as exc:
                # Same host behind the same limiter: stop rather than retry the other wiki.
                self._log("warning", "Helldivers wiki recent changes failed: %s", exc)
                break
            changes = payload.get("query", {}).get("recentchanges", []) if isinstance(payload, Mapping) else []
            if isinstance(changes, list) and changes:
                return _format_recent_changes([c for c in changes if isinstance(c, Mapping)], base_url)
        return "【绝地潜兵 2·Wiki 最近更改】\n暂时取不到 Wiki 的最近更改，请稍后再试"

    async def _wiki_balance_digest(self, entry: UpdateEntry) -> str:
        """Excerpt of a release's balancing section, from the Chinese wiki when possible."""
        page, api_url, base_url = await self._wiki_page_for(entry.title)
        heading = f"【绝地潜兵 2·数值调整】{entry.title_zh or entry.title}"
        if not page:
            search_url = WIKI_ZH_BASE_URL + "index.php?search=" + urllib.parse.quote(entry.title)
            return f"{heading}\n没有在 Wiki 上找到这一期的页面（可能是查询失败或尚未建档）\n{search_url}"
        url = _wiki_url(page, base_url)
        body = ""
        try:
            payload = await self._fetch_json(
                api_url,
                {
                    "action": "query",
                    "prop": "revisions",
                    "titles": page,
                    "rvprop": "content",
                    "rvslots": "main",
                    "format": "json",
                },
            )
            body = wikitext_to_plain(extract_wiki_section(_wiki_page_wikitext(payload), WIKI_SECTION_KEYWORDS))
        except Exception as exc:
            self._log("warning", "Helldivers wiki page failed: %s", exc)
        if not body:
            return f"{heading}\n这一期在 Wiki 上没有单独的数值调整章节，可打开页面查看\n{url}"
        limit = self._cfg_int("wiki_digest_max_chars", WIKI_DIGEST_LIMIT, 100)
        truncated = len(body) > limit
        lines = [heading, body[:limit].rstrip()]
        if truncated:
            lines.append("（已截断，完整内容见下方页面）")
        lines.extend([url, _wiki_attribution(base_url)])
        return "\n".join(lines)

    async def collect_entries(self) -> list[UpdateEntry]:
        jobs: list[Any] = []
        if self._cfg.get("steam_enabled", True):
            steam_endpoint, rejected = _endpoint_or_default(self._cfg.get("steam_endpoint"), STEAM_NEWS_URL)
            if rejected:
                self._log("warning", "Helldivers steam_endpoint is not an http(s) URL, using the default: %s", rejected)
            if "appid=" not in steam_endpoint:
                separator = "&" if "?" in steam_endpoint else "?"
                steam_endpoint += f"{separator}appid={self._steam_app_id()}&count=50&maxlength=10000"
            jobs.append(self._fetch_json(steam_endpoint))
        if self._cfg.get("wiki_enabled", False):
            wiki_endpoint, rejected = _endpoint_or_default(self._cfg.get("wiki_endpoint"), WIKI_API_URL)
            if rejected:
                self._log("warning", "Helldivers wiki_endpoint is not an http(s) URL, using the default: %s", rejected)
            jobs.append(
                self._fetch_json(
                    wiki_endpoint,
                    {
                        "action": "query",
                        "list": "recentchanges",
                        "rcnamespace": 0,
                        "rclimit": self._cfg_int("wiki_recent_limit", WIKI_RECENT_LIMIT, 1),
                        "rcprop": "title|ids|timestamp|comment",
                        "format": "json",
                    },
                )
            )
        localized_job = asyncio.create_task(self._localized_steam_titles())
        results = await asyncio.gather(*jobs, return_exceptions=True)
        localized = await localized_job
        entries: list[UpdateEntry] = []
        self._last_collect_had_source = False
        for index, value in enumerate(results):
            if isinstance(value, Exception):
                self._log("warning", "Helldivers source %s unavailable: %s", index, value)
                continue
            self._last_collect_had_source = True
            entries.extend(parse_steam_news(value) if index == 0 and self._cfg.get("steam_enabled", True) else parse_wiki_entries(value))
        entries = [_with_localized_text(entry, localized) for entry in entries]
        return deduplicate_entries(
            e for e in entries if is_official_announcement(e) and (is_relevant(e) or is_version_update(e))
        )

    async def poll_once(self, *, deliver: bool = True) -> int:
        """Collect, store, and (unless ``deliver`` is false) fan out deliveries.

        ``deliver=False`` is used by on-demand ``push`` for an explicit category:
        the store is refreshed so the answer is current, but the backlog is left
        for the scheduled poll instead of being blasted into the chat.
        """
        if not self._store:
            return 0
        entries = await self.collect_entries()
        groups = [str(g) for g in (self._cfg.get("default_groups") or [])] + self._store.groups()
        groups = list(dict.fromkeys(g for g in groups if g))
        # Treat a non-empty result as a successful first collection as well.
        # This keeps the baseline guarantee intact for alternate collectors and
        # test doubles that do not update the internal source-health flag.
        baseline = self._first_run and (self._last_collect_had_source or bool(entries))
        # Repair rows left behind by earlier versions before touching this batch.
        self._store.reconcile_statuses()
        sent = 0
        for entry in entries:
            if baseline:
                self._store.upsert(entry, "baseline")
                continue
            if not is_version_update(entry):
                # Kept for on-demand ``/helldivers push <type>``, but the automatic
                # feed only announces new builds.
                self._store.upsert(entry, "muted")
                continue
            # An entry collected while nobody is subscribed has no delivery target,
            # so store it as skipped rather than pending forever.
            inserted = self._store.upsert(entry, "pending" if groups else "skipped")
            if inserted:
                self._store.ensure_deliveries(entry.key, groups)
            if not deliver:
                # Stored as pending with delivery rows, so the scheduled poll picks
                # the announcement up on its own.
                continue
            detail_url: str | None = None
            for gid in groups:
                delivery = self._store.delivery(entry.key, gid)
                if not delivery or delivery["status"] == "sent":
                    continue
                if detail_url is None:  # resolved once, only when something is sent
                    detail_url = await self._wiki_detail_url(entry)
                ok, error = await self._send_to_group(gid, format_entry(entry, detail_url))
                self._store.mark_delivery(entry.key, gid, ok, error)
                sent += int(ok)
        if self._last_collect_had_source or entries:
            self._first_run = False
        return sent

    async def _send_to_group(self, group_id: str, message: str) -> tuple[bool, str]:
        try:
            ctx = self.ctx
            session = await ctx.chat.open_session(platform="qq", chat_type="group", group_id=str(group_id))
            stream_id = session.get("stream_id") if isinstance(session, Mapping) else session
            result = await ctx.send.text(text=message, stream_id=stream_id)
            if isinstance(result, Mapping):
                ok = bool(result.get("sent", result.get("success", False)))
            else:
                ok = result is not False
            return ok, "" if ok else str(result)
        except Exception as exc:
            return False, str(exc)

    def _group_from_kwargs(self, kwargs: Mapping[str, Any]) -> str:
        return str(kwargs.get("group_id") or kwargs.get("chat_id") or kwargs.get("target_id") or "").strip()

    def _cfg_int(self, key: str, default: int, minimum: int = 0) -> int:
        """Read an int tunable, tolerating a hand-edited config.toml.

        ``self._cfg`` is the raw dict, not a validated model instance, so a typo in
        the file must not raise inside a scheduled task.
        """
        try:
            return max(minimum, int(self._cfg.get(key, default)))
        except (TypeError, ValueError):
            self._log("warning", "Helldivers config %s is not a number, falling back to %s", key, default)
            return default

    def _cfg_bool(self, key: str, default: bool) -> bool:
        """Read a bool tunable, tolerating a hand-edited config.toml."""
        value = self._cfg.get(key, default)
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on", "是"}
        return bool(value)

    def _subscribed_groups(self) -> set[str]:
        """Every group that receives pushes: config ``default_groups`` plus the table."""
        configured = [str(g) for g in (self._cfg.get("default_groups") or [])]
        groups = configured + (self._store.groups() if self._store else [])
        return {g.strip() for g in groups if str(g).strip()}

    def _actor_id(self, kwargs: Mapping[str, Any]) -> str:
        return str(kwargs.get("user_id") or "").strip()

    def _cfg_id_list(self, key: str) -> set[str]:
        """Read a list of QQ numbers, tolerating a bare string or a TOML table."""
        value = self._cfg.get(key)
        if isinstance(value, str):
            rows: Iterable[Any] = [value]
        elif isinstance(value, Mapping):
            rows = list(value)  # a TOML inline table used as a plain set
        elif isinstance(value, (list, tuple, set)):
            rows = value
        else:
            return set()
        return {str(item).strip() for item in rows if str(item).strip()}

    def _command_blacklist(self) -> dict[str, set[str]]:
        """``{action or '*': {user_id, ...}}`` from the ``command_blacklist`` table."""
        value = self._cfg.get("command_blacklist")
        if not isinstance(value, Mapping):
            return {}
        result: dict[str, set[str]] = {}
        for raw_action, raw_users in value.items():
            if isinstance(raw_users, str):
                users: Iterable[Any] = [raw_users]
            elif isinstance(raw_users, (list, tuple, set)):
                users = raw_users
            else:
                continue
            blocked = {str(user).strip() for user in users if str(user).strip()}
            if not blocked:
                continue
            key = str(raw_action).strip()
            key = "*" if key in {"*", "all", "全部", "所有"} else ACTION_ALIASES.get(key, key)
            result.setdefault(key, set()).update(blocked)
        return result

    def _is_admin(self, kwargs: Mapping[str, Any]) -> bool:
        """Host operator, or a user named in the ``admin_users`` whitelist."""
        if kwargs.get("is_local_operator") is True:
            return True
        actor = self._actor_id(kwargs)
        return bool(actor) and actor in self._cfg_id_list("admin_users")

    def _command_permission(self, kwargs: Mapping[str, Any], action: str) -> str:
        """Reason this user may not run this action, or an empty string when allowed.

        Functional commands are open to everyone unless blacklisted; management
        commands are whitelist-only. The host operator and anyone in
        ``admin_users`` pass both, so a blacklist can never lock an admin out.
        """
        if not action or self._is_admin(kwargs):
            return ""
        if action in ADMIN_ACTIONS:
            return "这是管理指令，只有管理员可以使用"
        actor = self._actor_id(kwargs)
        if not actor:
            return ""
        blocked = self._command_blacklist()
        if actor in blocked.get("*", set()) or actor in blocked.get(action, set()):
            return "你已被禁止使用该指令"
        return ""

    def _command_gate(self, kwargs: Mapping[str, Any], gid: str, action: str) -> str:
        """Reason the command is refused, or an empty string when it may run.

        Unsubscribed groups may only run the actions in
        ``UNSUBSCRIBED_ALLOWED_ACTIONS``; operators, whitelisted admins and
        non-group contexts (private chat, no ``group_id``) always pass.
        """
        if not self._cfg_bool("require_subscription", True):
            return ""
        if not gid or self._is_admin(kwargs):
            return ""
        if action in UNSUBSCRIBED_ALLOWED_ACTIONS:
            return ""
        if gid in self._subscribed_groups():
            return ""
        return "本群尚未订阅更新推送"

    def _push_cooldown_seconds(self) -> int:
        return self._cfg_int("force_push_cooldown_seconds", FORCE_PUSH_MIN_INTERVAL_SECONDS, 0)

    def _loadout_reroll_seconds(self) -> int:
        return self._cfg_int("loadout_reroll_seconds", LOADOUT_REROLL_TTL_SECONDS, 1)

    def _steam_app_id(self) -> int:
        return self._cfg_int("steam_app_id", APP_ID, 1)

    def _wiki_sources(self) -> tuple[tuple[str, str], ...]:
        """Wiki API/base pairs with the configured endpoint as the fallback source."""
        endpoint, rejected = _endpoint_or_default(self._cfg.get("wiki_endpoint"), WIKI_API_URL)
        if rejected:
            self._log("warning", "Helldivers wiki_endpoint is not an http(s) URL, using the default: %s", rejected)
        if endpoint == WIKI_API_URL:
            return WIKI_SOURCES
        return (WIKI_SOURCES[0], (endpoint, WIKI_BASE_URL))

    def _resolve_command(self, kwargs: Mapping[str, Any]) -> tuple[str, str]:
        """Return ``(action, argument)``, preferring the host's named matches.

        An empty ``action`` means the text reached us only because the host's alias
        check is a bare ``text.startswith(alias)`` -- e.g. ``/hdx`` -- so it is not
        a real command and must not fall through to a default action.
        """
        matched = kwargs.get("matched_groups")
        action = str(matched.get("action") or "").strip().lower() if isinstance(matched, Mapping) else ""
        argument = str(matched.get("arg") or "").strip().lower() if isinstance(matched, Mapping) else ""
        if not action:
            result = COMMAND_RE.match(str(kwargs.get("text") or "").strip())
            if result is None:
                return "", ""
            action = str(result.group("action") or "").strip().lower()
            argument = str(result.group("arg") or "").strip().lower()
        action = ACTION_ALIASES.get(action, action)
        if action in HELP_ONLY_ALIASES:
            # ``/hd 简写`` is shorthand for ``help shortcut``.
            return "help", HELP_ONLY_ALIASES[action]
        if not action:
            return "status", ""
        if action == "push":
            return "push", PUSH_CATEGORY_ALIASES.get(argument, argument or "version")
        # A bare category name is shorthand for ``push <category>``.
        if action not in COMMAND_ACTIONS and action in PUSH_CATEGORY_ALIASES:
            return "push", PUSH_CATEGORY_ALIASES[action]
        return action, argument

    def _usage_hint(self) -> str:
        """Usage list rendered from the live config, so it can never go stale."""
        return usage_hint(
            poll_interval=self._cfg_int("poll_interval_seconds", DEFAULT_INTERVAL_SECONDS),
            push_cooldown=self._push_cooldown_seconds(),
            reroll=self._loadout_reroll_seconds(),
            subscribed_only=self._cfg_bool("require_subscription", True),
        )

    def _help_text(self, argument: str) -> str:
        """Full usage, or a single topic when an argument is given.

        Topics that mention a tunable are callables, so the help always shows the
        values the plugin is actually running with.
        """
        if not argument:
            return self._usage_hint()
        topic = HELP_ONLY_ALIASES.get(argument) or ACTION_ALIASES.get(argument, argument)
        if topic not in HELP_TOPICS and argument in PUSH_CATEGORY_ALIASES:
            # ``help patch`` / ``help 补丁`` both document the push categories.
            topic = "push"
        detail = HELP_TOPICS.get(topic)
        if detail is None:
            return f"没有「{argument}」这个主题\n\n{self._usage_hint()}"
        return detail(self) if callable(detail) else detail

    async def _send_image(self, kwargs: Mapping[str, Any], image_base64: str) -> bool:
        """Send a rendered card to the originating chat stream."""
        stream_id = str(kwargs.get("stream_id") or "").strip()
        if not stream_id:
            self._last_render_error = "命令上下文里没有 stream_id，无法定位会话"
            return False
        if not image_base64:
            self._last_render_error = "没有可发送的图片数据"
            return False
        try:
            result = await self.ctx.send.image(image_base64, stream_id)
        except Exception as exc:
            self._last_render_error = f"发送图片异常：{exc}"
            self._log("warning", "Helldivers image send failed: %s", exc)
            return False
        if isinstance(result, Mapping):
            ok = bool(result.get("sent", result.get("success", False)))
            if not ok:
                self._last_render_error = f"发送图片被拒绝：{dict(result)}"
        else:
            ok = result is not False
            if not ok:
                self._last_render_error = "发送图片返回 False"
        if not ok:
            self._log("warning", "Helldivers image send rejected: %s", self._last_render_error)
        return ok

    async def _call_render(self, args: Mapping[str, Any]) -> Any:
        """Render through the raw capability so the RPC timeout can be raised.

        ``ctx.render.html2png`` cannot set a timeout, and the host caps ``cap.call``
        at 30 s -- less than a cold browser start, so the first render always failed
        with ``[E_TIMEOUT]``. ``call_capability(timeout_ms=...)`` passes the timeout
        as the RPC budget instead of a capability argument.
        """
        call_capability = getattr(self.ctx, "call_capability", None)
        if callable(call_capability):
            return await call_capability("render.html2png", timeout_ms=RENDER_RPC_TIMEOUT_MS, **args)
        # Older SDK builds only expose the typed proxy: no way to raise the budget.
        return await self.ctx.render.html2png(
            str(args["html"]),
            viewport=dict(args["viewport"]),
            device_scale_factor=float(args["device_scale_factor"]),
            full_page=bool(args["full_page"]),
        )

    async def _render_card(self, card_html: str, *, label: str = "配装卡片") -> str:
        """Host-rendered PNG as base64; empty string when rendering is unavailable.

        The host answers a failed render with ``{"success": False, "error": ...}``
        instead of raising, so the reason has to be read out of the payload --
        otherwise the failure is invisible and the caller silently degrades.
        """
        try:
            result = await self._call_render(
                {
                    "html": card_html,
                    "selector": "body",
                    "viewport": {"width": LOADOUT_CARD_WIDTH, "height": LOADOUT_CARD_HEIGHT},
                    "device_scale_factor": 1.0,
                    "full_page": True,
                }
            )
        except Exception as exc:
            self._last_render_error = f"调用渲染能力异常：{exc}"
            self._log("warning", "Helldivers %s render raised: %s", label, exc)
            return ""
        if isinstance(result, Mapping):
            image = str(result.get("image_base64") or result.get("data") or "")
            if image:
                self._last_render_error = ""
                return image
            reason = str(result.get("error") or result.get("message") or "").strip()
            self._last_render_error = reason or "渲染服务没有返回图片数据"
            self._log(
                "warning",
                "Helldivers %s render returned no image (keys=%s): %s",
                label,
                sorted(result),
                self._last_render_error,
            )
            return ""
        text = str(result or "")
        if text:
            self._last_render_error = ""
            return text
        self._last_render_error = "渲染能力返回了空值"
        self._log("warning", "Helldivers %s render returned an empty value", label)
        return ""

    def _loadout_scope(self, kwargs: Mapping[str, Any]) -> str:
        """Per-user session key, so one member's reroll never touches another's."""
        stream = str(kwargs.get("stream_id") or "").strip()
        user = str(kwargs.get("user_id") or "").strip()
        return f"{stream}|{user}" if user else stream

    def _prune_loadout_sessions(self, now: float, ttl: int | None = None) -> None:
        window = self._loadout_reroll_seconds() if ttl is None else ttl
        expired = [key for key, item in self._loadout_sessions.items() if now - item[2] > window]
        for key in expired:
            del self._loadout_sessions[key]

    async def _diagnose(self, kwargs: Mapping[str, Any]) -> tuple[bool, str, int]:
        """Probe the image pipeline so a text-only loadout can be explained.

        A render failure used to be silent: the card quietly degraded to text and
        nothing said why. This command walks the same steps and reports the host's
        own error message.
        """
        pool = load_loadout_pool()
        icons = load_loadout_icons()
        total = sum(len(names) for names in pool.values())
        missing = [name for names in pool.values() for name in names if name not in icons]
        lines = [
            "【绝地潜兵 2·自检】",
            f"配装数据：{total} 件（{'、'.join(f'{slot} {len(names)}' for slot, names in pool.items())}）"
            if pool
            else "配装数据：未加载（缺少 loadout_data.json）",
            f"装备图标：{len(icons)} 个" + (f"，缺 {len(missing)} 件" if missing else "，全覆盖"),
            f"订阅群：{len(self._subscribed_groups())} 个",
            f"来源开关：steam={bool(self._cfg.get('steam_enabled', True))} "
            f"wiki={bool(self._cfg.get('wiki_enabled', False))}",
        ]
        started = time.monotonic()
        image = await self._render_card(DIAG_HTML, label="自检")
        elapsed = int((time.monotonic() - started) * 1000)
        if not image:
            lines.append(f"浏览器渲染：失败（{elapsed} ms）")
            reason = self._last_render_error or "未知"
            lines.append(f"失败原因：{reason}")
            if "E_TIMEOUT" in reason or "超时" in reason:
                lines.append(f"→ 渲染超时（本次上限 {RENDER_RPC_TIMEOUT_MS // 1000} 秒）。")
                lines.append("   首次渲染需要启动 Playwright 浏览器，必要时还会下载 Chromium，")
                lines.append("   比较慢；请等一两分钟后再发一次 /helldivers diag，就绪后通常几秒出图。")
            else:
                lines.append("→ 检查 MaiBot 配置 plugin_runtime.render.enabled 是否为 true；")
                lines.append("   以及宿主环境是否装了 playwright 和 Chromium。")
            await self._reply(kwargs, "\n".join(lines))
            return False, "渲染不可用", 2
        lines.append(f"浏览器渲染：成功（{elapsed} ms，PNG 约 {len(image) * 3 // 4 // 1024} KB）")
        sent = await self._send_image(kwargs, image)
        lines.append("图片发送：成功（上面那张就是测试图）" if sent else f"图片发送：失败（{self._last_render_error or '未知'}）")
        await self._reply(kwargs, "\n".join(lines))
        return sent, "自检通过" if sent else "图片发送失败", 2

    async def _send_loadout_card(self, kwargs: Mapping[str, Any], loadout: Mapping[str, str], motto: str) -> tuple[bool, str, int]:
        image = await self._render_card(render_loadout_html(loadout, motto=motto, icons=load_loadout_icons()))
        if image and await self._send_image(kwargs, image):
            return True, "已发送配装图", 2
        reason = self._last_render_error or "未知原因"
        self._log("warning", "Helldivers loadout fell back to text: %s", reason)
        text = format_loadout(loadout, motto=motto)
        # Surface the reason: a silent text fallback is impossible to debug from chat.
        await self._reply(kwargs, f"{text}\n（配装图生成失败，已回退文字：{reason}）")
        return True, "已发送配装文本", 2

    async def _reroll_loadout(
        self,
        kwargs: Mapping[str, Any],
        pool: Mapping[str, Sequence[str]],
        scope: str,
        argument: str,
        now: float,
    ) -> tuple[bool, str, int]:
        """Re-draw one card of the loadout drawn within the last 120 seconds."""
        index = parse_loadout_index(argument)
        if index not in LOADOUT_SLOT_CHOICES:
            await self._reply(
                kwargs,
                f"序号需要在 1-8 之间（{LOADOUT_SLOT_HINT}）\n"
                f"用法：先发 /helldivers loadout 抽一套，再在 {self._loadout_reroll_seconds()} 秒内发 "
                "/helldivers loadout <序号> 重抽该格",
            )
            return False, "配装序号无效", 2
        session = self._loadout_sessions.get(scope)
        if session is None:
            await self._reply(
                kwargs,
                f"没有可重抽的配装，请先发 /helldivers loadout 抽一套\n"
                f"（重抽只在抽完之后 {self._loadout_reroll_seconds()} 秒内可用）",
            )
            return False, "没有可重抽的配装", 2
        loadout, motto, _ = session
        key = LOADOUT_SLOT_CHOICES[index]
        replacement = reroll_loadout_slot(pool, loadout, key)
        if replacement is None:
            await self._reply(kwargs, f"{loadout_slot_title(index)} 没有别的可选装备了")
            return False, "没有可选装备", 2
        loadout = {**loadout, key: replacement}
        # Sliding window: the countdown restarts so consecutive rerolls keep working.
        self._loadout_sessions[scope] = (loadout, motto, now)
        return await self._send_loadout_card(kwargs, loadout, motto)

    async def _random_loadout(self, kwargs: Mapping[str, Any], argument: str = "") -> tuple[bool, str, int]:
        """Roll a loadout, or re-draw one card of the previous roll."""
        pool = load_loadout_pool()
        if not pool:
            await self._reply(kwargs, "没有找到配装数据（loadout_data.json），无法生成配装")
            return False, "缺少配装数据", 2
        scope = self._loadout_scope(kwargs)
        now = time.monotonic()
        self._prune_loadout_sessions(now)
        if argument:
            return await self._reroll_loadout(kwargs, pool, scope, argument, now)
        loadout = generate_loadout(pool)
        motto = random.choice(LOADOUT_MOTTOS)
        self._loadout_sessions[scope] = (loadout, motto, now)
        return await self._send_loadout_card(kwargs, loadout, motto)

    async def _reply(self, kwargs: Mapping[str, Any], message: str) -> bool:
        """Send command feedback to the originating chat stream.

        The host never delivers a command's returned ``response`` text to the
        chat, so every command must push its own reply through ``ctx.send.text``.
        """
        stream_id = str(kwargs.get("stream_id") or "").strip()
        if not stream_id:
            return False
        try:
            result = await self.ctx.send.text(text=message, stream_id=stream_id)
        except Exception as exc:  # a failed reply must not break the command
            self._log("warning", "Helldivers reply failed: %s", exc)
            return False
        if isinstance(result, Mapping):
            return bool(result.get("sent", result.get("success", False)))
        return result is not False

    def _latest_for_push(self, category: str) -> tuple[sqlite3.Row, UpdateEntry] | None:
        """Newest stored announcement matching a ``push`` category filter."""
        for row in self._store.latest_entries() if self._store else []:
            entry = _entry_from_row(row)
            if not is_official_announcement(entry):
                continue
            if category == "all":
                return row, entry
            if category == "version":
                if is_version_update(entry):
                    return row, entry
            elif entry.category == category:
                return row, entry
        return None

    def _mark_pushed(self, entry_key: str, gid: str) -> None:
        """Release a group's pending delivery for an entry the command just sent.

        ``push`` answers through its own reply rather than the delivery loop, so
        without this the scheduled poll would post the very same card again.
        """
        if not self._store or not gid:
            return
        delivery = self._store.delivery(entry_key, gid)
        if delivery is not None and delivery["status"] != "sent":
            self._store.mark_delivery(entry_key, gid, True, "")

    async def _force_push(self, kwargs: Mapping[str, Any], gid: str, category: str) -> tuple[bool, str, int]:
        """Send the newest announcement of one category, ignoring the poll timer.

        The on-demand refresh is rate limited: inside the cooldown the command
        still answers, but from the newest entry already stored. ``wiki`` /
        ``balance`` have no stored copy to fall back on, so they are refused
        instead of quietly hitting the upstream feed again.
        """
        if not self._store:
            await self._reply(kwargs, "Helldivers 插件尚未加载完成，请稍后再试")
            return False, "插件尚未加载", 2
        if category not in PUSH_CATEGORY_LABELS:
            await self._reply(kwargs, f"未知的推送类型：{category}\n{PUSH_USAGE_HINT}")
            return False, f"未知推送类型 {category}", 2
        label = PUSH_CATEGORY_LABELS[category]
        now = time.monotonic()
        refreshed = kwargs.get("is_local_operator") is True or (
            now - self._last_force_check >= self._push_cooldown_seconds()
        )
        if not refreshed and category in LIVE_ONLY_PUSH_CATEGORIES:
            # These read straight from the wiki: answering from the database is
            # impossible, so honour the cooldown instead of ignoring it.
            await self._reply(
                kwargs,
                f"{label}为冷却中：这类内容每次都向上游取最新，两次之间至少间隔 "
                f"{self._push_cooldown_seconds()} 秒（避免被 Wiki 限流），请稍后再试。",
            )
            return False, "推送冷却中", 2
        delivered_before: dict[str, int] = {}
        # The bare command mirrors the automatic feed, so it may announce this
        # round's new releases. An explicitly requested category must answer with
        # exactly one message: refresh the store, then send only what was asked
        # for and leave the rest to the scheduled poll.
        broadcast = refreshed and category == "version"
        if refreshed:
            self._last_force_check = now
            delivered_before = self._store.group_delivery_attempts(gid) if gid else {}
            # Skip the poll interval: collect now, deliver by the rules above.
            await self.poll_once(deliver=broadcast)
        if category == "wiki":
            # Site-wide feed, independent of any stored announcement.
            ok = await self._reply(kwargs, await self._wiki_recent_changes_digest())
            return ok, "已推送 Wiki 最近更改" if ok else "推送失败", 2
        found = self._latest_for_push("version" if category == "balance" else category)
        if found is None:
            await self._reply(kwargs, f"还没有采集到{label}类公告，请等一轮定时检查后再试")
            return False, "暂无公告", 2
        row, entry = found
        entry_key = str(row["entry_key"])
        # Only the default category mirrors the automatic feed, so only it can be a
        # duplicate of what this very command just delivered. An explicitly requested
        # category always gets its content, and a cached answer skips no poll at all.
        if category == "version" and refreshed and gid:
            attempts_now = self._store.group_delivery_attempts(gid).get(entry_key, 0)
            if attempts_now > delivered_before.get(entry_key, 0):
                await self._reply(kwargs, f"{label}已在本轮推送：{entry.title}")
                return True, "已推送", 2
        if category == "balance":
            ok = await self._reply(kwargs, await self._wiki_balance_digest(entry))
            return ok, "已推送 Wiki 数值" if ok else "推送失败", 2
        header = f"【强制推送·{label}】已即时检查更新源" if refreshed else f"【强制推送·{label}】最近一期"
        ok = await self._reply(kwargs, f"{header}\n{format_entry(entry, await self._wiki_detail_url(entry))}")
        if ok:
            self._mark_pushed(entry_key, gid)
        return ok, "已强制推送" if ok else "推送失败", 2

    @Command(
        COMMAND_NAME,
        pattern=COMMAND_PATTERN,
        aliases=list(COMMAND_ALIASES),
        timeout_ms=COMMAND_RPC_TIMEOUT_MS,
    )
    async def handle_command(self, **kwargs: Any) -> tuple[bool, str, int]:
        if not self._store:
            await self._reply(kwargs, "Helldivers 插件尚未加载完成，请稍后再试")
            return False, "插件尚未加载", 2
        gid = self._group_from_kwargs(kwargs)
        action, argument = self._resolve_command(kwargs)
        refusal = self._command_gate(kwargs, gid, action)
        if refusal:
            allowed = "、".join(f"/helldivers {name}" for name in sorted(UNSUBSCRIBED_ALLOWED_ACTIONS))
            await self._reply(
                kwargs,
                f"{refusal}，暂时只能使用 {allowed}。\n"
                "订阅本群后即可使用全部指令（push / loadout / status / help …）。",
            )
            return False, refusal, 2
        denial = self._command_permission(kwargs, action)
        if denial:
            await self._reply(kwargs, f"{denial}。")
            return False, denial, 2
        if not action:
            # The host only prefix-matches aliases, so this is text like ``/hdx`` that
            # the host claimed as a command but that our grammar cannot parse.
            text = str(kwargs.get("text") or "").strip()
            await self._reply(
                kwargs,
                f"没看懂「{text}」。触发词后面只接一个动作，例如 /hd st、/helldivers 状态。\n\n"
                f"{self._usage_hint()}",
            )
            return False, "命令格式无法解析", 2
        if action == "subscribe":
            if not gid:
                await self._reply(kwargs, "请在群聊中使用 /helldivers subscribe")
                return False, "缺少群号", 2
            self._store.subscribe(gid)
            message = (
                f"已订阅本群（{gid}），现在共有 {len(self._subscribed_groups())} 个群接收更新推送"
            )
            await self._reply(kwargs, message)
            return True, "已订阅", 2
        if action == "unsubscribe":
            if not gid:
                await self._reply(kwargs, "请在群聊中使用 /helldivers unsubscribe")
                return False, "缺少群号", 2
            removed = self._store.unsubscribe(gid)
            message = f"已取消本群（{gid}）的更新推送" if removed else f"本群（{gid}）尚未订阅更新推送"
            await self._reply(kwargs, message)
            return True, "已取消订阅" if removed else "未订阅", 2
        if action == "list":
            groups = self._subscribed_groups()
            configured = {str(g).strip() for g in (self._cfg.get("default_groups") or []) if str(g).strip()}
            if groups:
                lines = "\n".join(
                    f"- {group}"
                    + ("（配置）" if group in configured else "")
                    + ("（本群）" if group == gid else "")
                    for group in sorted(groups)
                )
                message = f"接收更新推送的群（{len(groups)}）：\n{lines}"
            else:
                message = "目前没有群订阅更新推送。发送 /helldivers subscribe 可以订阅本群"
            await self._reply(kwargs, message)
            return True, "已列出订阅群", 2
        if action == "push":
            return await self._force_push(kwargs, gid, argument)
        if action == "loadout":
            return await self._random_loadout(kwargs, argument)
        if action == "diag":
            return await self._diagnose(kwargs)
        if action == "help":
            await self._reply(kwargs, self._help_text(argument))
            return True, "已发送用法说明", 2
        if action != "status":
            message = f"未知子命令：{action}\n\n{self._usage_hint()}"
            await self._reply(kwargs, message)
            return False, f"未知子命令 {action}", 2
        counts = self._store.counts()
        unpushed = counts["skipped"] + counts["baseline"] + counts["muted"]
        subscribed = self._subscribed_groups()
        scope = (
            f"本群（{gid}）：{'已订阅' if gid in subscribed else '未订阅'}\n" if gid else ""
        )
        message = (
            f"绝地潜兵 2 更新推送状态\n{scope}"
            f"订阅群 {len(subscribed)}，公告 {counts['entries']}，已发送 {counts['sent']}，"
            f"待发送 {counts['pending']}，失败 {counts['failed']}，无需推送 {unpushed}"
        )
        if self._last_render_error:
            message += f"\n配装出图异常：{self._last_render_error}（可用 /helldivers diag 复现）"
        await self._reply(kwargs, message)
        return True, "已发送状态", 2

    def _log(self, level: str, message: str, *args: Any) -> None:
        try:
            context = self.ctx
        except (AttributeError, RuntimeError):
            context = None
        logger = getattr(context, "logger", None)
        if logger and hasattr(logger, level):
            getattr(logger, level)(message, *args)


def _entry_from_row(row: Mapping[str, Any]) -> UpdateEntry:
    """Rebuild an entry from a stored row so it can be formatted and re-sent."""
    raw_tags = str(row["tags"] or "")
    return UpdateEntry(
        source=str(row["source"]),
        source_id=str(row["source_id"]),
        title=str(row["title"]),
        summary=str(row["summary"]),
        url=str(row["url"]),
        published_at=_to_datetime(row["published_at"]),
        category=str(row["category"]),
        content_hash=str(row["content_hash"]),
        source_feed=str(row["source_feed"] or ""),
        tags=tuple(tag for tag in raw_tags.split(",") if tag),
        title_zh=str(row["title_zh"] or ""),
        summary_zh=str(row["summary_zh"] or ""),
    )


def extract_wiki_section(wikitext: str, keywords: Iterable[str]) -> str:
    """Body of the section whose heading matches one of ``keywords``.

    Section levels are honoured, so a ``===`` subsection does not terminate the
    ``==`` section that contains it.
    """
    lowered = tuple(keyword.lower() for keyword in keywords)
    lines = wikitext.split("\n")
    for index, line in enumerate(lines):
        heading = re.match(r"^(=+)\s*(.+?)\s*=+$", line)
        if heading is None or not any(key in heading.group(2).lower() for key in lowered):
            continue
        level = len(heading.group(1))
        body: list[str] = []
        for following in lines[index + 1:]:
            nested = re.match(r"^(=+)\s*.+?\s*=+$", following)
            if nested is not None and len(nested.group(1)) <= level:
                break
            body.append(following)
        return "\n".join(body).strip()
    return ""


def wikitext_to_plain(wikitext: str) -> str:
    """Flatten wiki markup into chat-readable text."""
    text = re.sub(r"\{\{[^{}]*\}\}", " ", wikitext)
    text = re.sub(r"\[\[(?:[^\]|]*\|)?([^\]]*)\]\]", r"\1", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\[https?://\S+\s*([^\]]*)\]", r"\1", text)
    text = re.sub(r"'{2,}", "", text)
    text = re.sub(r"^\s*[*#:;]+\s*", "· ", text, flags=re.M)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _wiki_page_wikitext(payload: Mapping[str, Any]) -> str:
    """Pull the wikitext out of a ``prop=revisions`` response."""
    pages = payload.get("query", {}).get("pages", {}) if isinstance(payload, Mapping) else {}
    if not isinstance(pages, Mapping):
        return ""
    for page in pages.values():
        if not isinstance(page, Mapping) or page.get("missing") is not None:
            continue
        revisions = page.get("revisions")
        if isinstance(revisions, list) and revisions and isinstance(revisions[0], Mapping):
            slots = revisions[0].get("slots", {})
            main = slots.get("main", {}) if isinstance(slots, Mapping) else {}
            if isinstance(main, Mapping):
                return str(main.get("*") or main.get("content") or "")
    return ""


def _wiki_url(page: str, base_url: str = WIKI_BASE_URL) -> str:
    return base_url + urllib.parse.quote(page.replace(" ", "_"), safe="()/_-")


def _wiki_attribution(base_url: str) -> str:
    """License notice matching the Chinese or English wiki that supplied a page."""
    return WIKI_ZH_ATTRIBUTION if "/zh/" in base_url else WIKI_EN_ATTRIBUTION


def _format_recent_changes(changes: list[Mapping[str, Any]], base_url: str) -> str:
    """Render a MediaWiki ``recentchanges`` batch for chat."""
    lines = [f"【绝地潜兵 2·Wiki 最近更改】共 {len(changes)} 条最新编辑"]
    related = 0
    for item in changes:
        title = str(item.get("title") or "?")
        stamp = _to_datetime(item.get("timestamp")).astimezone(BEIJING_TZ).strftime("%m-%d %H:%M")
        comment = clean_summary(item.get("comment") or "", 40)
        line = f"· {title} — {stamp}"
        if comment:
            line += f" — {comment}"
        if VERSION_RE.search(title) or RELEVANT_RE.search(title):
            related += 1
            line += "（补丁相关）"
        lines.append(line)
    if related:
        lines.append(f"其中 {related} 条与补丁/更新相关")
    lines.extend((f"完整列表：{base_url}Special:RecentChanges", _wiki_attribution(base_url)))
    return "\n".join(lines)


def parse_loadout_index(argument: str) -> int:
    """Card number from ``1``-``8``; ``0`` when the text is not a slot number."""
    text = str(argument or "").strip()
    if text.isdigit():
        return int(text)
    return LOADOUT_INDEX_WORDS.get(text, 0)


def loadout_slot_title(index: int) -> str:
    """Human name of a card number, e.g. ``5`` -> ``战略配备 1``."""
    key = LOADOUT_SLOT_CHOICES.get(index, "")
    if key.startswith("stratagem_"):
        return f"战略配备 {int(key.split('_')[1]) + 1}"
    return LOADOUT_SLOT_LABELS.get(key, key)


def reroll_loadout_slot(
    pool: Mapping[str, Sequence[str]],
    loadout: Mapping[str, str],
    key: str,
    rng: random.Random | None = None,
) -> str | None:
    """A different item for one card; ``None`` when the pool has no alternative."""
    picker = rng or random.Random()
    if key.startswith("stratagem_"):
        taken = {loadout.get(f"stratagem_{index}", "") for index in range(LOADOUT_STRATAGEM_COUNT)}
        candidates = [name for name in pool.get("stratagem", []) if name not in taken]
    else:
        current = loadout.get(key, "")
        candidates = [name for name in pool.get(key, []) if name != current]
    return picker.choice(candidates) if candidates else None


def load_loadout_pool(path: str | Path = LOADOUT_DATA_FILE) -> dict[str, list[str]]:
    """Slot -> item names. Returns an empty mapping when the data file is missing."""
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    items = payload.get("items") if isinstance(payload, Mapping) else None
    pool: dict[str, list[str]] = {}
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, Mapping):
            continue
        slot = str(item.get("slot") or "")
        name = str(item.get("name") or "").strip()
        if slot and name:
            pool.setdefault(slot, []).append(name)
    return pool


def load_loadout_icons(path: str | Path = LOADOUT_ICONS_FILE) -> dict[str, str]:
    """Item name -> self-contained data URI. Empty when the icon file is absent.

    Read once per process: the file is a few hundred kilobytes and the icon set
    does not change while the bot runs.
    """
    global _LOADOUT_ICONS_CACHE
    if _LOADOUT_ICONS_CACHE is None:
        icons: dict[str, str] = {}
        try:
            payload = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            payload = None
        raw = payload.get("icons") if isinstance(payload, Mapping) else None
        if isinstance(raw, Mapping):
            for name, uri in raw.items():
                text = str(uri or "")
                if text.startswith("data:image/"):
                    icons[str(name)] = text
        _LOADOUT_ICONS_CACHE = icons
    return _LOADOUT_ICONS_CACHE


def generate_loadout(
    pool: Mapping[str, Sequence[str]],
    rng: random.Random | None = None,
) -> dict[str, str]:
    """One item per ordinary slot plus four distinct stratagems."""
    picker = rng or random.Random()
    loadout: dict[str, str] = {}
    for slot in LOADOUT_SLOTS:
        choices = list(pool.get(slot) or [])
        if choices:
            loadout[slot] = picker.choice(choices)
    stratagems = list(pool.get("stratagem") or [])
    if stratagems:
        count = min(LOADOUT_STRATAGEM_COUNT, len(stratagems))
        for index, name in enumerate(picker.sample(stratagems, count)):
            loadout[f"stratagem_{index}"] = name
    return loadout


def render_loadout_html(
    loadout: Mapping[str, str],
    *,
    motto: str = "",
    footer: str = LOADOUT_FOOTER,
    icons: Mapping[str, str] | None = None,
) -> str:
    """Self-contained HTML card (no external assets) for ``ctx.render.html2png``.

    Icons are inlined as ``data:`` URIs; anything the icon file does not cover
    simply renders without a picture instead of breaking the card.
    """
    art = icons if icons is not None else {}

    def card(index: int, label: str, name: str) -> str:
        badge = f'<b class="idx">{index}</b>'
        if not name:
            return (
                f'<div class="card noicon"><div class="slot">{badge}{html.escape(label)}</div>'
                '<div class="name empty">没有可用装备</div></div>'
            )
        icon = art.get(name, "")
        # Escaped even though ``load_loadout_icons`` only admits ``data:image/``:
        # the value lands inside an HTML attribute, so it must not be able to
        # break out of it if the icon file is ever hand-edited.
        figure = f'<div class="icon"><img src="{html.escape(icon, quote=True)}" alt=""></div>' if icon else ""
        classes = "card" if icon else "card noicon"
        return (
            f'<div class="{classes}"><div class="slot">{badge}{html.escape(label)}</div>'
            f'{figure}<div class="name">{html.escape(name)}</div></div>'
        )

    ordinary = "".join(
        card(index, LOADOUT_SLOT_LABELS[slot], loadout.get(slot, ""))
        for index, slot in enumerate(LOADOUT_SLOTS, start=1)
    )
    stratagems = "".join(
        card(5 + index, "战略配备", loadout.get(f"stratagem_{index}", ""))
        for index in range(LOADOUT_STRATAGEM_COUNT)
    )
    return (
        '<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8"><style>'
        "* { margin:0; padding:0; box-sizing:border-box; }"
        f"body {{ width:{LOADOUT_CARD_WIDTH}px; min-height:{LOADOUT_CARD_HEIGHT}px; padding:56px 55px; color:#f3f5f7;"
        "background:linear-gradient(180deg,#101720 0%,#090d13 55%,#05080c 100%);"
        'font-family:"Microsoft YaHei","PingFang SC","Noto Sans CJK SC",sans-serif; }'
        ".kicker { text-align:center; color:#f5cb3d; font-size:26px; font-weight:700; letter-spacing:4px; }"
        ".title { text-align:center; font-size:56px; font-weight:900; margin-top:10px; }"
        ".rule { height:3px; margin:32px 0; background:linear-gradient(90deg,transparent,#f5cb3d,transparent); }"
        ".section { color:#f5cb3d; font-size:32px; font-weight:800; margin:24px 0 18px; }"
        ".grid { display:grid; grid-template-columns:1fr 1fr; gap:26px; }"
        ".card { min-height:310px; padding:20px 22px; border-radius:24px; display:flex; flex-direction:column;"
        "background:rgba(18,25,34,.88); border:2px solid rgba(245,203,61,.55); }"
        ".slot { color:#f5cb3d; font-size:24px; font-weight:700; display:flex; align-items:center; gap:10px; }"
        ".idx { display:inline-flex; align-items:center; justify-content:center; flex:none; width:34px; height:34px;"
        "border-radius:50%; background:rgba(245,203,61,.16); border:2px solid rgba(245,203,61,.55); font-size:20px; }"
        ".icon { height:126px; margin:6px 0 10px; display:flex; align-items:center; justify-content:center; }"
        # width:auto keeps the img box on the artwork's own aspect ratio, which is what
        # lets border-radius actually clip the square tiles some icons ship with.
        ".icon img { height:100%; width:auto; max-width:100%; object-fit:contain; border-radius:12px; }"
        ".name { flex:1; display:flex; align-items:center; justify-content:center; text-align:center;"
        "font-size:34px; font-weight:700; line-height:1.22; word-break:break-word; }"
        ".noicon .name { font-size:40px; }"
        ".empty { color:#929aa4; font-size:28px; font-weight:400; }"
        ".motto { text-align:center; color:#f5cb3d; font-size:29px; font-weight:700; margin-top:42px; }"
        ".footer { text-align:center; color:#a7afb8; font-size:23px; margin-top:24px; }"
        "</style></head><body>"
        '<div class="kicker">为了超级地球</div>'
        '<div class="title">绝地潜兵 2 随机配装</div>'
        '<div class="rule"></div>'
        '<div class="section">常规装备</div>'
        f'<div class="grid">{ordinary}</div>'
        '<div class="section">战略配备</div>'
        f'<div class="grid">{stratagems}</div>'
        f'<div class="motto">{html.escape(motto)}</div>'
        f'<div class="footer">{html.escape(footer)}</div>'
        "</body></html>"
    )


def format_loadout(loadout: Mapping[str, str], *, motto: str = "") -> str:
    """Plain-text rendering, used when the host cannot render images.

    Slot numbers match the badges on the card so a text-only user can reroll too.
    """
    lines = ["【绝地潜兵 2·随机配装】"]
    for index, slot in enumerate(LOADOUT_SLOTS, start=1):
        if loadout.get(slot):
            lines.append(f"{index} {LOADOUT_SLOT_LABELS[slot]}：{loadout[slot]}")
    for index in range(LOADOUT_STRATAGEM_COUNT):
        name = loadout.get(f"stratagem_{index}", "")
        if name:
            lines.append(f"{5 + index} 战略配备：{name}")
    if motto:
        lines.append(motto)
    return "\n".join(lines)


def format_entry(entry: UpdateEntry, detail_url: str = "") -> str:
    title = entry.title_zh or entry.title
    summary = entry.summary_zh or entry.summary
    label = CATEGORY_LABELS.get(entry.category, entry.category)
    published = entry.published_at.astimezone(BEIJING_TZ).strftime("%Y-%m-%d %H:%M")
    lines = [f"【绝地潜兵 2·{label}】{title}", summary, entry.url]
    if detail_url:
        lines.append(f"详细数值调整：{detail_url}")
    lines.append(f"发布时间：{published}（北京时间）")
    return "\n".join(lines)


def create_plugin() -> HelldiversPlugin:
    return HelldiversPlugin()


# A descriptive alias is useful to hosts that derive plugin names from classes.
GameUpdatePlugin = HelldiversPlugin
