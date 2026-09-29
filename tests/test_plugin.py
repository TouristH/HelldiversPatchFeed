from __future__ import annotations

import asyncio
import json
import random
import re
import sqlite3
import unicodedata
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from plugin import (
    ACTION_ALIASES,
    ADMIN_ACTIONS,
    ACTION_LABELS,
    CATEGORY_SHORTCUTS,
    COMMAND_ACTIONS,
    COMMAND_ALIASES,
    COMMAND_NAME,
    COMMAND_RE,
    COMMAND_PATTERN,
    COMMAND_RPC_TIMEOUT_MS,
    COMMAND_TRIGGERS,
    DEFAULT_INTERVAL_SECONDS,
    DIAG_HTML,
    FORCE_PUSH_MIN_INTERVAL_SECONDS,
    FUNCTIONAL_ACTIONS,
    HELP_ONLY_ALIASES,
    HELP_TOPICS,
    LETTER_SHORTCUTS,
    LOADOUT_CARD_HEIGHT,
    LOADOUT_CARD_WIDTH,
    HelldiversPlugin,
    LOADOUT_REROLL_TTL_SECONDS,
    LOADOUT_STRATAGEM_COUNT,
    PUSH_CATEGORY_ALIASES,
    PUSH_CATEGORY_LABELS,
    PluginConfig,
    PluginSection,
    RENDER_RPC_TIMEOUT_MS,
    SINGLE_CHAR_SHORTCUTS,
    SQLiteStore,
    UNSUBSCRIBED_ALLOWED_ACTIONS,
    UpdateEntry,
    USER_AGENT,
    WIKI_API_URL,
    WIKI_SECTION_KEYWORDS,
    _category,
    _endpoint_or_default,
    clean_summary,
    deduplicate_entries,
    extract_wiki_section,
    format_entry,
    format_loadout,
    generate_loadout,
    human_seconds,
    is_official_announcement,
    is_relevant,
    is_steam_media,
    is_version_update,
    load_loadout_icons,
    load_loadout_pool,
    pad_display,
    parse_localized_steam_feed,
    parse_steam_news,
    parse_wiki_entries,
    render_loadout_html,
    usage_hint,
    wikitext_to_plain,
)

# Shape of helldivers.wiki.gg/zh/wiki/1.006.300: same infobox, Chinese headings.
WIKI_ZH_PAGE_WIKITEXT = """{{Infobox Game Version
|title= Machinery of Oppression: 6.3.0
}}
== 🌍总览 ==
绝地潜兵们，这次更新很大。
== ⚖️平衡性调整 ==
通用平衡性调整
*部分敌人的耐久伤害被降低
=== 主武器 ===
*备弹数量从 1 提升至 2
=== 战略配备 ===
*轨道激光冷却从 240 秒降至 180 秒
== 🔧修复 ==
*修复了打开终端时的崩溃问题
"""

# Shape of helldivers.wiki.gg/wiki/1.006.300: the balancing heading owns subsections.
WIKI_PAGE_WIKITEXT = """{{Infobox Game Version
|title= Machinery of Oppression: 6.3.0
}}
== 🌍 Overview ==
Attention Helldivers, this is a big one.
== ⚖️ Balancing ==
General balance changes
*Some enemies have had their '''durable damage''' reduced
=== Primary weapons ===
*Increased refill from 1 to 2
*[[AR-23 Liberator|Liberator]] damage reduced from 200 to 180
=== Stratagems ===
*Orbital Laser cooldown reduced from 240 to 180
== 🔧 Fixes ==
*Fixed a crash when opening terminals
"""


RECENT_CHANGES = [
    {"title": "1.007.101", "timestamp": "2026-09-24T09:00:00Z", "comment": "/* 平衡性调整 */ 更新数值"},
    {"title": "R-4 Hyena", "timestamp": "2026-09-23T10:00:00Z", "comment": "removed the needless magazine"},
]


def wiki_fetcher(
    *,
    zh_page: str = "",
    en_page: str = "",
    recent: list[dict] | None = None,
) -> object:
    """Fake transport: ``/zh/api.php`` and ``/api.php`` served independently."""

    async def fetch_json(url: str, params: dict | None = None) -> dict:
        if url.endswith("/api.php"):
            is_zh = "/zh/" in url
            if params and params.get("list") == "recentchanges":
                return {"query": {"recentchanges": recent or []}}
            page = zh_page if is_zh else en_page
            if params and params.get("prop") == "revisions":
                body = WIKI_ZH_PAGE_WIKITEXT if is_zh else WIKI_PAGE_WIKITEXT
                return {"query": {"pages": {"1": {"revisions": [{"slots": {"main": {"*": body}}}]}}}}
            return {"query": {"search": [{"title": page}] if page else []}}
        return {"appnews": {"newsitems": []}}

    return fetch_json

# Mirrors the shape of store.steampowered.com/feeds/news/app/553850/?l=schinese&cc=CN:
# only the builds the developer actually translated carry Chinese text.
LOCALIZED_RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
<item>
  <title>自由入寂：7.1.0</title>
  <description>&lt;p&gt;社区反馈改动 敌人将不再把无人乘坐的载具作为目标&lt;/p&gt;</description>
  <guid>https://store.steampowered.com/news/app/553850/view/1</guid>
</item>
<item>
  <title>Devoid of Liberty: 7.1.1</title>
  <description>&lt;p&gt;Hotfix, not translated&lt;/p&gt;</description>
  <guid>https://store.steampowered.com/news/app/553850/view/2</guid>
</item>
</channel></rss>"""


def entry(
    source_id: str,
    title: str = "Patch 1.0.0",
    summary: str = "Balance update",
    *,
    published: datetime | None = None,
    source_feed: str = "",
    tags: tuple[str, ...] = (),
) -> UpdateEntry:
    """Release-style entry; category is derived exactly like the real parser does."""
    return UpdateEntry(
        source="steam",
        source_id=source_id,
        title=title,
        summary=summary,
        url="https://example.test/news/" + source_id,
        published_at=published or datetime(2026, 9, 28, tzinfo=timezone.utc),
        category=_category(title, summary),
        source_feed=source_feed,
        tags=tags,
    )


def test_clean_summary_removes_markup_urls_and_truncates() -> None:
    text = clean_summary("[b]Patch[/b] <p>Fixes https://example.test/x</p>  many words in a very long announcement body", limit=24)
    assert "[" not in text and "<" not in text and "https://" not in text
    assert text.endswith("\u2026")
    assert len(text) <= 24


def test_parse_steam_news_and_relevance() -> None:
    payload = {
        "appnews": {
            "newsitems": [
                {
                    "gid": "steam-1",
                    "title": "Patch 01.002.300",
                    "contents": "[b]Balance update[/b]",
                    "url": "https://store.steampowered.com/news/1",
                    "date": 1780000000,
                },
                {"gid": "steam-2", "title": "Community screenshot", "contents": "hello"},
            ]
        }
    }
    items = parse_steam_news(payload)
    assert [item.source_id for item in items] == ["steam-1", "steam-2"]
    assert items[0].category == "patch"
    assert is_relevant(items[0])
    assert not is_relevant(items[1])


def test_parse_wiki_recent_changes() -> None:
    items = parse_wiki_entries(
        {
            "query": {
                "recentchanges": [
                    {
                        "rcid": 42,
                        "title": "Warbond: Borderline Justice",
                        "comment": "Warbond release notes",
                        "timestamp": "2026-09-27T09:00:00Z",
                    }
                ]
            }
        }
    )
    assert len(items) == 1
    assert items[0].source == "wiki"
    assert items[0].source_id == "42"
    assert items[0].category == "warbond"
    assert items[0].url.startswith("https://helldivers.wiki.gg/wiki/")


def test_deduplicate_keeps_newest_identical_source_record() -> None:
    older = entry("same")
    newer = UpdateEntry(**{**older.__dict__, "published_at": datetime(2026, 9, 29, tzinfo=timezone.utc)})
    result = deduplicate_entries([older, newer])
    assert result == [newer]


def test_deduplicate_retains_changed_content_as_new_event() -> None:
    original = entry("edited", summary="Balance update")
    changed = entry("edited", summary="Balance update with follow-up fix")
    result = deduplicate_entries([original, changed])
    assert len(result) == 2
    assert {item.content_hash for item in result} == {original.content_hash, changed.content_hash}


def test_sqlite_store_tracks_subscriptions_and_delivery(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "feed.sqlite3")
    item = entry("store-1")
    assert not store.has_entries()
    assert store.upsert(item)
    assert not store.upsert(item)
    store.ensure_deliveries(item.key, ["100", "200"])
    assert store.delivery(item.key, "100")["status"] == "pending"
    store.mark_delivery(item.key, "100", False, "network timeout")
    failed = store.delivery(item.key, "100")
    assert failed["status"] == "failed" and failed["attempts"] == 1
    store.mark_delivery(item.key, "100", True)
    assert store.delivery(item.key, "100")["status"] == "sent"
    store.subscribe("300")
    store.subscribe("300")
    assert store.groups() == ["300"]
    assert store.unsubscribe("300")
    assert not store.unsubscribe("300")
    assert store.counts()["entries"] == 1
    store.close()


def test_first_poll_only_builds_history_then_sends_new_entries() -> None:
    async def scenario() -> None:
        plugin = HelldiversPlugin()
        plugin._store = SQLiteStore(":memory:")
        plugin._cfg = {"default_groups": ["100"]}
        plugin._first_run = True
        initial = entry("poll-1")
        latest = entry("poll-2")
        batches = [[initial], [initial, latest], [initial, latest]]
        sent: list[str] = []

        async def collect() -> list[UpdateEntry]:
            return batches.pop(0)

        async def send(group: str, message: str) -> tuple[bool, str]:
            sent.append(group + message)
            return True, ""

        plugin.collect_entries = collect  # type: ignore[method-assign]
        plugin._send_to_group = send  # type: ignore[method-assign]
        assert await plugin.poll_once() == 0
        assert sent == []
        assert await plugin.poll_once() == 1
        assert len(sent) == 1
        assert "poll-2" in sent[0]
        assert await plugin.poll_once() == 0
        assert len(sent) == 1

    asyncio.run(scenario())


def test_failed_delivery_is_retried() -> None:
    async def scenario() -> None:
        plugin = HelldiversPlugin()
        plugin._store = SQLiteStore(":memory:")
        plugin._cfg = {"default_groups": ["100"]}
        plugin._first_run = False
        item = entry("retry-1")
        responses = [(False, "temporary error"), (True, "")]

        async def collect() -> list[UpdateEntry]:
            return [item]

        async def send(group: str, message: str) -> tuple[bool, str]:
            return responses.pop(0)

        plugin.collect_entries = collect  # type: ignore[method-assign]
        plugin._send_to_group = send  # type: ignore[method-assign]
        assert await plugin.poll_once() == 0
        assert plugin._store.delivery(item.key, "100")["status"] == "failed"
        assert await plugin.poll_once() == 1
        delivery = plugin._store.delivery(item.key, "100")
        assert delivery["status"] == "sent" and delivery["attempts"] == 2

    asyncio.run(scenario())


def test_wiki_still_works_when_steam_is_unavailable() -> None:
    async def scenario() -> None:
        plugin = HelldiversPlugin()
        plugin._cfg = {"steam_enabled": True, "wiki_enabled": True}

        async def fetch(url: str, params: object = None) -> dict:
            if "steampowered" in url:
                raise OSError("Steam unavailable")
            return {"query": {"recentchanges": [{"rcid": 12, "title": "Patch 02", "comment": "Update notes"}]}}

        async def fetch_text(url: str) -> str:
            raise OSError("Steam unavailable")

        plugin._fetch_json = fetch  # type: ignore[method-assign]
        plugin._fetch_text = fetch_text  # type: ignore[method-assign]
        result = await plugin.collect_entries()
        assert len(result) == 1 and result[0].source == "wiki"

    asyncio.run(scenario())


def test_poll_task_cancellation_is_clean() -> None:
    async def scenario() -> None:
        plugin = HelldiversPlugin()
        plugin._cfg = {"poll_interval_seconds": 3600}
        plugin.poll_once = lambda: asyncio.sleep(3600)  # type: ignore[method-assign]
        plugin._task = asyncio.create_task(plugin._poll_loop())
        await asyncio.sleep(0)
        plugin._stop_poll_task()
        await asyncio.sleep(0)
        assert plugin._task is None

    asyncio.run(scenario())


def command_plugin(replies: list[str]) -> HelldiversPlugin:
    """Build a plugin whose replies are captured instead of sent to a platform.

    ``require_subscription`` is off here so these tests exercise command
    behaviour rather than the subscription gate; access control has its own
    tests built on ``gated_plugin``.
    """
    plugin = HelldiversPlugin()
    plugin._store = SQLiteStore(":memory:")
    plugin._cfg = {"require_subscription": False}

    async def reply(kwargs: object, message: str) -> bool:
        replies.append(message)
        return True

    async def offline(url: str, params: dict | None = None) -> dict:
        raise OSError("network disabled in tests")

    async def offline_text(url: str) -> str:
        raise OSError("network disabled in tests")

    plugin._reply = reply  # type: ignore[method-assign]
    plugin._fetch_json = offline  # type: ignore[method-assign]
    plugin._fetch_text = offline_text  # type: ignore[method-assign]
    return plugin


def gated_plugin(replies: list[str], **config: object) -> HelldiversPlugin:
    """Like ``command_plugin`` but with the default subscription gate enabled."""
    plugin = command_plugin(replies)
    plugin._cfg = dict(config)
    return plugin


def test_reply_sends_text_to_the_originating_stream() -> None:
    async def scenario() -> None:
        plugin = HelldiversPlugin()
        calls: list[tuple[object, object]] = []

        class FakeSend:
            async def text(self, text: object = None, stream_id: object = None) -> bool:
                calls.append((text, stream_id))
                return True

        class FakeCtx:
            send = FakeSend()

        attach_ctx(plugin, FakeCtx())
        assert await plugin._reply({"stream_id": "s-9"}, "hello")
        assert calls == [("hello", "s-9")]
        assert not await plugin._reply({}, "no stream")
        assert len(calls) == 1

    asyncio.run(scenario())


def test_subscribe_command_replies_and_persists_group() -> None:
    async def scenario() -> None:
        replies: list[str] = []
        plugin = command_plugin(replies)
        ok, _, _ = await plugin.handle_command(text="/helldivers subscribe", group_id="100", stream_id="s-1")
        assert ok
        assert plugin._store is not None and plugin._store.groups() == ["100"]
        assert replies and "已订阅" in replies[0] and "100" in replies[0]
        ok, _, _ = await plugin.handle_command(
            matched_groups={"action": "unsubscribe"}, group_id="100", stream_id="s-1"
        )
        assert ok
        assert plugin._store.groups() == []
        assert "已取消" in replies[1]

    asyncio.run(scenario())


def test_status_and_list_commands_reply_instead_of_silently_returning() -> None:
    async def scenario() -> None:
        replies: list[str] = []
        plugin = command_plugin(replies)
        await plugin.handle_command(matched_groups={"action": "subscribe"}, group_id="7", stream_id="s")
        ok, _, _ = await plugin.handle_command(text="/helldivers", group_id="7", stream_id="s")
        assert ok and "已订阅" in replies[-1]
        ok, _, _ = await plugin.handle_command(
            text="/helldivers list", group_id="7", stream_id="s", is_local_operator=True
        )
        assert ok and "7" in replies[-1] and "本群" in replies[-1]
        ok, _, _ = await plugin.handle_command(text="/helldivers status", group_id="8", stream_id="s")
        assert ok and "未订阅" in replies[-1]
        assert len(replies) == 4

    asyncio.run(scenario())


def test_command_gate_allows_only_subscribe_before_subscribing() -> None:
    """The allow-list is the single source of truth for what a new group may run."""
    plugin = gated_plugin([])
    for action in sorted(COMMAND_ACTIONS):
        allowed = plugin._command_gate({}, "100", action) == ""
        assert allowed == (action in UNSUBSCRIBED_ALLOWED_ACTIONS), f"{action} 的放行判断不对"
    assert plugin._store is not None
    plugin._store.subscribe("100")
    for action in sorted(COMMAND_ACTIONS):
        assert plugin._command_gate({}, "100", action) == "", f"订阅后 {action} 仍被拦截"


def test_gate_allowlist_only_contains_real_actions() -> None:
    assert UNSUBSCRIBED_ALLOWED_ACTIONS <= COMMAND_ACTIONS, "放行清单里有不存在的指令"


def test_unsubscribed_group_gets_a_hint_instead_of_the_command_output() -> None:
    async def scenario() -> None:
        replies: list[str] = []
        plugin = gated_plugin(replies)
        ok, _, _ = await plugin.handle_command(text="/helldivers status", group_id="100", stream_id="s")
        assert not ok
        assert "只能使用 /helldivers subscribe" in replies[-1]
        assert "订阅本群后即可使用全部指令" in replies[-1]
        # subscribe is the way out, and it works
        ok, _, _ = await plugin.handle_command(text="/helldivers subscribe", group_id="100", stream_id="s")
        assert ok and "已订阅" in replies[-1]
        ok, _, _ = await plugin.handle_command(text="/helldivers status", group_id="100", stream_id="s")
        assert ok and "已订阅" in replies[-1]

    asyncio.run(scenario())


def test_gate_does_not_run_any_side_effect_for_a_blocked_command() -> None:
    """A refused command must not write to the store either."""

    async def scenario() -> None:
        replies: list[str] = []
        plugin = gated_plugin(replies)
        await plugin.handle_command(text="/helldivers unsubscribe", group_id="100", stream_id="s")
        assert plugin._store is not None
        assert plugin._store.groups() == []
        assert "只能使用 /helldivers subscribe" in replies[-1]

    asyncio.run(scenario())


def test_configured_default_groups_count_as_subscribed() -> None:
    """A group listed in config receives pushes, so it must pass the gate too."""
    async def scenario() -> None:
        replies: list[str] = []
        plugin = gated_plugin(replies, default_groups=["999"])
        assert plugin._command_gate({}, "999", "status") == ""
        ok, _, _ = await plugin.handle_command(text="/helldivers status", group_id="999", stream_id="s")
        assert ok and "已订阅" in replies[-1]
        plugin._store.subscribe("100")  # type: ignore[union-attr]
        ok, _, _ = await plugin.handle_command(
            text="/helldivers list", group_id="100", stream_id="s", is_local_operator=True
        )
        assert ok and "999（配置）" in replies[-1] and "100（本群）" in replies[-1]

    asyncio.run(scenario())


def test_operator_and_private_chat_bypass_the_gate() -> None:
    async def scenario() -> None:
        replies: list[str] = []
        plugin = gated_plugin(replies)
        # Operator in an unsubscribed group.
        ok, _, _ = await plugin.handle_command(
            text="/helldivers status", group_id="100", stream_id="s", is_local_operator=True
        )
        assert ok and "未订阅" in replies[-1]
        # No group id at all (private chat) is not gated.
        assert plugin._command_gate({}, "", "status") == ""

    asyncio.run(scenario())


def test_require_subscription_can_be_switched_off() -> None:
    async def scenario() -> None:
        replies: list[str] = []
        plugin = gated_plugin(replies, require_subscription=False)
        assert plugin._command_gate({}, "100", "status") == ""
        ok, _, _ = await plugin.handle_command(text="/helldivers status", group_id="100", stream_id="s")
        assert ok and "未订阅" in replies[-1]

    asyncio.run(scenario())


def test_trigger_aliases_stay_in_sync_with_the_pattern_and_the_decorator() -> None:
    """The regex, the registered aliases and the parser must come from one list."""
    assert COMMAND_TRIGGERS[0] == COMMAND_NAME
    assert COMMAND_ALIASES == tuple(f"/{name}" for name in COMMAND_TRIGGERS[1:])
    for name in COMMAND_TRIGGERS:
        assert COMMAND_RE.match(f"/{name}") is not None, f"/{name} 不被命令正则接受"
        assert COMMAND_RE.match(f"/{name} status") is not None, f"/{name} status 解析失败"
    # The host prefix-matches every incoming message against these, so a bare word
    # would swallow ordinary chat -- they must all keep the leading slash.
    for alias in COMMAND_ALIASES:
        assert alias.startswith("/"), f"别名 {alias} 没有 / 前缀，会误伤普通聊天"


def test_command_decorator_registers_exactly_the_documented_aliases() -> None:
    info = getattr(HelldiversPlugin.handle_command, "__maibot_component_info__", None)
    assert info is not None, "命令缺少组件元数据"
    meta = dict(getattr(info, "metadata", info) or {})
    aliases = list(getattr(info, "aliases", meta.get("aliases", [])) or [])
    assert aliases == list(COMMAND_ALIASES)
    pattern = getattr(info, "command_pattern", None) or meta.get("command_pattern")
    assert pattern == COMMAND_PATTERN


def test_every_short_form_maps_to_a_real_target() -> None:
    for alias, action in ACTION_ALIASES.items():
        assert action in COMMAND_ACTIONS, f"动作别名 {alias} -> {action} 不是真实动作"
    for alias, topic in HELP_ONLY_ALIASES.items():
        assert topic in HELP_TOPICS, f"主题别名 {alias} -> {topic} 不是真实主题"
    for alias, category in PUSH_CATEGORY_ALIASES.items():
        assert category in PUSH_CATEGORY_LABELS, f"类型别名 {alias} -> {category} 不是真实类型"


def test_letter_and_single_character_shortcuts_resolve() -> None:
    plugin = command_plugin([])
    # The tables are the source of truth; the alias dict must agree with them.
    for shortcut, action in (*LETTER_SHORTCUTS, *SINGLE_CHAR_SHORTCUTS):
        assert ACTION_ALIASES.get(shortcut) == action, f"简写 {shortcut} 没有映射到 {action}"
    # ...and each resolves end-to-end through the parser, on every trigger.
    for shortcut, action in (*LETTER_SHORTCUTS, *SINGLE_CHAR_SHORTCUTS):
        assert plugin._resolve_command({"text": f"/hd {shortcut}"})[0] == action
        assert plugin._resolve_command({"text": f"/helldivers {shortcut}"})[0] == action


def test_pad_display_accounts_for_wide_characters() -> None:
    """CJK glyphs take two columns, so plain ``ljust`` produces ragged tables."""
    assert pad_display("sub", 8) == "sub     "
    assert pad_display("订阅", 8) == "订阅    "  # 4 columns used, 4 spaces left
    assert pad_display("取消订阅", 12) == "取消订阅    "
    assert pad_display("toolongvalue", 4) == "toolongvalue"


def test_shortcut_table_rows_share_one_display_width() -> None:
    plugin = command_plugin([])
    rows = plugin._help_text("shortcut").splitlines()
    suffixes = tuple(f"/hd {shortcut}" for shortcut, _ in LETTER_SHORTCUTS)
    table = [row for row in rows if row.startswith("  ") and row.rstrip().endswith(suffixes)]
    assert len(table) == len(LETTER_SHORTCUTS), f"简写表只渲染出 {len(table)} 行"
    widths = {
        sum(2 if unicodedata.east_asian_width(ch) in {"W", "F"} else 1 for ch in row.split("/hd ")[0])
        for row in table
    }
    assert len(widths) == 1, f"简写表列宽不一致：{sorted(widths)}"


def test_shortcut_tables_are_fully_documented_in_the_help() -> None:
    """``topic_shortcut`` and ``usage_hint`` render from the tables, so they agree."""
    plugin = command_plugin([])
    body = plugin._help_text("shortcut")
    hint = plugin._usage_hint()
    for shortcut, action in LETTER_SHORTCUTS:
        assert f"/hd {shortcut}" in body, f"字母简写 {shortcut} 未在简写说明里列出"
        assert f"{shortcut} {ACTION_LABELS[action]}" in hint
    for shortcut, action in SINGLE_CHAR_SHORTCUTS:
        assert f"{shortcut}={ACTION_LABELS[action]}" in body, f"单字简写 {shortcut} 未列出"
    for shortcut, category in CATEGORY_SHORTCUTS:
        assert f"{shortcut}={PUSH_CATEGORY_LABELS[category]}" in body
        assert f"{shortcut} {PUSH_CATEGORY_LABELS[category]}" in hint


def test_letter_shortcuts_work_across_every_trigger() -> None:
    plugin = command_plugin([])
    for trigger in COMMAND_TRIGGERS:
        action, argument = plugin._resolve_command({"text": f"/{trigger} p hf"})
        assert (action, argument) == ("push", "hotfix"), f"/{trigger} p hf 解析错误"


def test_push_shortcut_categories_resolve() -> None:
    plugin = command_plugin([])
    for shortcut, category in (("v", "version"), ("pt", "patch"), ("hf", "hotfix"),
                               ("wb", "warbond"), ("wk", "wiki"), ("bl", "balance")):
        assert plugin._resolve_command({"text": f"/hd p {shortcut}"}) == ("push", category)
        # A bare shortcut is shorthand for ``push <category>`` too.
        assert plugin._resolve_command({"text": f"/hd {shortcut}"}) == ("push", category)


def test_unparseable_command_is_refused_instead_of_running_a_default() -> None:
    """``/hdx`` only reaches us because the host alias check is ``startswith``."""

    async def scenario() -> None:
        replies: list[str] = []
        plugin = gated_plugin(replies, require_subscription=False)
        ok, why, _ = await plugin.handle_command(text="/hdx", group_id="100", stream_id="s")
        assert ok is False
        assert "没看懂" in replies[-1] and "用法：" in replies[-1]
        # It must not be mistaken for the default action, so nothing was collected.
        assert plugin._store is not None and plugin._store.counts()["entries"] == 0
        assert why

    asyncio.run(scenario())


def test_shortcut_help_topic_is_reachable_by_short_and_chinese_names() -> None:
    plugin = command_plugin([])
    body = plugin._help_text("简写")
    assert body == plugin._help_text("shortcut")
    assert body.startswith("【/helldivers")
    assert "中文写法" in body
    # ``/hd 简写`` is shorthand for ``help shortcut``.
    assert plugin._resolve_command({"text": "/hd 简写"}) == ("help", "shortcut")
    assert plugin._resolve_command({"text": "/hd h 简写"}) == ("help", "简写")


def test_short_trigger_runs_the_command_end_to_end() -> None:
    async def scenario() -> None:
        replies: list[str] = []
        plugin = command_plugin(replies)
        assert plugin._store is not None
        plugin._store.subscribe("1")
        ok, _, _ = await plugin.handle_command(text="/hd 订", group_id="2", stream_id="s")
        assert ok and "已订阅" in replies[-1]
        ok, _, _ = await plugin.handle_command(text="/潜兵 st", group_id="2", stream_id="s")
        assert ok and "已订阅" in replies[-1]

    asyncio.run(scenario())


def test_gate_toggle_is_reported_in_the_help_text() -> None:
    replies: list[str] = []
    gate_on = gated_plugin(replies)
    assert "未订阅的群只能发 subscribe" in gate_on._usage_hint()
    assert "未订阅的群只能发 subscribe" in gate_on._help_text("subscribe")
    gate_off = gated_plugin(replies, require_subscription=False)
    assert "未订阅的群只能发 subscribe" not in gate_off._usage_hint()
    assert "未订阅的群只能发 subscribe" not in gate_off._help_text("subscribe")


def test_command_answers_alias_unknown_action_and_private_chat() -> None:
    async def scenario() -> None:
        replies: list[str] = []
        plugin = command_plugin(replies)
        ok, _, _ = await plugin.handle_command(text="/helldivers 帮助", stream_id="s")
        assert ok and "用法" in replies[-1]
        ok, _, _ = await plugin.handle_command(text="/helldivers 胡写", stream_id="s")
        assert ok is False and "未知子命令" in replies[-1]
        ok, _, _ = await plugin.handle_command(text="/helldivers subscribe", stream_id="s")
        assert ok is False and "群聊" in replies[-1]
        assert len(replies) == 3

    asyncio.run(scenario())


def test_command_without_loaded_store_still_replies() -> None:
    async def scenario() -> None:
        replies: list[str] = []
        plugin = command_plugin(replies)
        plugin._store = None
        ok, _, _ = await plugin.handle_command(text="/helldivers status", stream_id="s")
        assert ok is False and "尚未加载" in replies[-1]

    asyncio.run(scenario())


def test_entries_collected_without_subscribers_are_not_left_pending() -> None:
    async def scenario() -> None:
        plugin = HelldiversPlugin()
        plugin._store = SQLiteStore(":memory:")
        plugin._cfg = {"require_subscription": False}
        plugin._first_run = False
        item = entry("nobody-1")

        async def collect() -> list[UpdateEntry]:
            return [item]

        async def send(group: str, message: str) -> tuple[bool, str]:
            raise AssertionError("nothing should be delivered while nobody is subscribed")

        plugin.collect_entries = collect  # type: ignore[method-assign]
        plugin._send_to_group = send  # type: ignore[method-assign]
        assert await plugin.poll_once() == 0
        assert plugin._store.delivery(item.key, "100") is None
        counts = plugin._store.counts()
        assert counts["pending"] == 0 and counts["skipped"] == 1
        # A group subscribing later must not receive the backlog.
        plugin._store.subscribe("100")
        assert await plugin.poll_once() == 0
        counts = plugin._store.counts()
        assert counts["pending"] == 0 and counts["skipped"] == 1

    asyncio.run(scenario())


def test_legacy_pending_entry_without_deliveries_is_reconciled() -> None:
    async def scenario() -> None:
        plugin = HelldiversPlugin()
        plugin._store = SQLiteStore(":memory:")
        plugin._cfg = {"require_subscription": False}
        plugin._first_run = False
        item = entry("legacy-1")
        # Reproduce a row written by the older code: pending, but no delivery rows.
        assert plugin._store.upsert(item, "pending")
        assert plugin._store.counts()["pending"] == 1

        async def collect() -> list[UpdateEntry]:
            return [item]

        plugin.collect_entries = collect  # type: ignore[method-assign]
        assert await plugin.poll_once() == 0
        counts = plugin._store.counts()
        assert counts["pending"] == 0 and counts["skipped"] == 1

    asyncio.run(scenario())


def test_unsubscribe_releases_pending_deliveries() -> None:
    async def scenario() -> None:
        plugin = HelldiversPlugin()
        plugin._store = SQLiteStore(":memory:")
        plugin._cfg = {"require_subscription": False}
        plugin._first_run = False
        item = entry("leaver-1")
        plugin._store.subscribe("100")
        plugin._store.upsert(item, "pending")
        plugin._store.ensure_deliveries(item.key, ["100"])

        async def collect() -> list[UpdateEntry]:
            return [item]

        async def send(group: str, message: str) -> tuple[bool, str]:
            return False, "network timeout"

        plugin.collect_entries = collect  # type: ignore[method-assign]
        plugin._send_to_group = send  # type: ignore[method-assign]
        assert await plugin.poll_once() == 0
        assert plugin._store.counts()["failed"] == 1
        assert plugin._store.unsubscribe("100")
        counts = plugin._store.counts()
        assert counts["failed"] == 0 and counts["skipped"] == 1
        assert plugin._store.delivery(item.key, "100") is None

    asyncio.run(scenario())


def force_push_plugin(replies: list[str], batches: list[list[UpdateEntry]]) -> HelldiversPlugin:
    """Plugin wired for ``/helldivers push``: replies captured, sources scripted."""
    plugin = command_plugin(replies)
    plugin._cfg = {"require_subscription": False}
    plugin._first_run = False

    async def collect() -> list[UpdateEntry]:
        return batches.pop(0) if batches else []

    plugin.collect_entries = collect  # type: ignore[method-assign]
    return plugin


def test_force_push_skips_poll_interval_and_sends_latest() -> None:
    async def scenario() -> None:
        replies: list[str] = []
        plugin = force_push_plugin(replies, [[]])
        assert plugin._store is not None
        stored = entry("old-patch", title="Patch 01.002.300")
        plugin._store.upsert(stored, "sent")

        async def send(group: str, message: str) -> tuple[bool, str]:
            raise AssertionError("no group is subscribed, nothing should be delivered")

        plugin._send_to_group = send  # type: ignore[method-assign]
        ok, _, _ = await plugin.handle_command(text="/helldivers push", group_id="100", stream_id="s")
        assert ok
        assert "【强制推送·版本更新】" in replies[-1] and "已即时检查更新源" in replies[-1]
        assert "Patch 01.002.300" in replies[-1] and "https://example.test/news/old-patch" in replies[-1]

    asyncio.run(scenario())


def test_force_push_rate_limits_source_checks_but_still_answers() -> None:
    async def scenario() -> None:
        replies: list[str] = []
        plugin = force_push_plugin(replies, [[]])
        assert plugin._store is not None
        plugin._store.upsert(entry("cached-patch", title="Patch 01.002.300"), "sent")
        checks = 0

        async def collect() -> list[UpdateEntry]:
            nonlocal checks
            checks += 1
            return []

        plugin.collect_entries = collect  # type: ignore[method-assign]
        await plugin.handle_command(text="/helldivers push", group_id="100", stream_id="s")
        assert checks == 1
        ok, _, _ = await plugin.handle_command(text="/helldivers 推送", group_id="100", stream_id="s")
        assert ok and checks == 1
        assert "最近一期" in replies[-1] and "Patch 01.002.300" in replies[-1]
        # Operators bypass the cooldown and force a real re-check.
        await plugin.handle_command(text="/helldivers push", group_id="100", stream_id="s", is_local_operator=True)
        assert checks == 2

    asyncio.run(scenario())


def test_force_push_does_not_duplicate_a_freshly_delivered_entry() -> None:
    async def scenario() -> None:
        replies: list[str] = []
        item = entry("fresh-1", title="Hotfix 01.003.000")
        plugin = force_push_plugin(replies, [[item]])
        assert plugin._store is not None
        plugin._store.subscribe("100")
        plugin._store.upsert(item, "pending")
        plugin._store.ensure_deliveries(item.key, ["100"])
        sent: list[str] = []

        async def send(group: str, message: str) -> tuple[bool, str]:
            sent.append(group)
            return True, ""

        plugin._send_to_group = send  # type: ignore[method-assign]
        ok, _, _ = await plugin.handle_command(text="/helldivers push", group_id="100", stream_id="s")
        assert ok
        assert sent == ["100"]
        assert replies[-1] == "版本更新已在本轮推送：Hotfix 01.003.000"

    asyncio.run(scenario())


def test_force_push_without_any_entry_asks_to_wait() -> None:
    async def scenario() -> None:
        replies: list[str] = []
        plugin = force_push_plugin(replies, [[]])
        ok, _, _ = await plugin.handle_command(text="/helldivers push", group_id="100", stream_id="s")
        assert ok is False and "还没有采集到" in replies[-1]

    asyncio.run(scenario())


def test_parse_steam_news_keeps_the_origin_feed_and_tags() -> None:
    items = parse_steam_news(
        {
            "appnews": {
                "newsitems": [
                    {
                        "gid": "official",
                        "title": "Devoid of Liberty: 7.1.1",
                        "contents": "Hotfix for crashes",
                        "feedname": "steam_community_announcements",
                        "tags": ["patchnotes"],
                        "date": 1780000000,
                    },
                    {
                        "gid": "press",
                        "title": "Helldivers 2 preview",
                        "contents": "A look at the next update",
                        "feedname": "Rock, Paper, Shotgun",
                        "date": 1780000000,
                    },
                ]
            }
        }
    )
    assert items[0].source_feed == "steam_community_announcements" and items[0].tags == ("patchnotes",)
    assert is_version_update(items[0])
    assert is_steam_media(items[1]) and not is_official_announcement(items[1])
    assert not is_version_update(items[1])


def test_collection_drops_third_party_steam_press() -> None:
    async def scenario() -> None:
        plugin = HelldiversPlugin()
        plugin._cfg = {"steam_enabled": True, "wiki_enabled": False}

        async def fetch(url: str, params: object = None) -> dict:
            return {
                "appnews": {
                    "newsitems": [
                        {
                            "gid": "note",
                            "title": "Devoid of Liberty: 7.1.0",
                            "contents": "Community Feedback Changes",
                            "feedname": "steam_community_announcements",
                            "date": 1780000000,
                        },
                        {
                            "gid": "press",
                            "title": "Helldivers 2 update is set to unleash new missions",
                            "contents": "An update about the game",
                            "feedname": "PCGamesN",
                            "date": 1780000000,
                        },
                    ]
                }
            }

        plugin._fetch_json = fetch  # type: ignore[method-assign]

        async def fetch_text(url: str) -> str:
            return ""

        plugin._fetch_text = fetch_text  # type: ignore[method-assign]
        got = await plugin.collect_entries()
        # The release note has no keyword in title or summary, only a build number.
        assert [item.source_id for item in got] == ["note"]

    asyncio.run(scenario())


def test_automatic_delivery_only_sends_version_updates() -> None:
    async def scenario() -> None:
        plugin = HelldiversPlugin()
        plugin._store = SQLiteStore(":memory:")
        plugin._cfg = {"default_groups": ["100"]}
        plugin._first_run = False
        release = entry("v-1", title="Devoid of Liberty: 7.1.0")
        roadmap = entry("r-1", title="Revealing our Machinery of Oppression Content Roadmap")
        sent: list[str] = []

        async def collect() -> list[UpdateEntry]:
            return [release, roadmap]

        async def send(group: str, message: str) -> tuple[bool, str]:
            sent.append(message.splitlines()[0])
            return True, ""

        plugin.collect_entries = collect  # type: ignore[method-assign]
        plugin._send_to_group = send  # type: ignore[method-assign]
        assert await plugin.poll_once() == 1
        assert len(sent) == 1 and "7.1.0" in sent[0]
        counts = plugin._store.counts()
        assert counts["sent"] == 1 and counts["muted"] == 1 and counts["pending"] == 0

    asyncio.run(scenario())


def test_push_category_suffix_selects_the_matching_announcement() -> None:
    async def scenario() -> None:
        replies: list[str] = []
        plugin = force_push_plugin(replies, [])
        assert plugin._store is not None
        store = plugin._store

        def at(day: int) -> datetime:
            return datetime(2026, 9, day, tzinfo=timezone.utc)

        store.upsert(entry("roadmap", title="Content Roadmap Revealed", published=at(27)), "muted")
        store.upsert(entry("warbond", title="The Exo Experts Warbond lumbers onto the battlefield", summary="Warbond", published=at(26)), "muted")
        store.upsert(entry("release", title="Devoid of Liberty: 7.1.0", published=at(25)), "sent")
        store.upsert(entry("hotfix", title="Hotfix 01.003.000", summary="Hotfix for crashes", published=at(24)), "sent")
        store.upsert(entry("patch", title="Machinery of Oppression: 6.3.1", summary="Patch notes", published=at(20)), "sent")

        await plugin.handle_command(text="/helldivers push", stream_id="s")
        assert "Devoid of Liberty: 7.1.0" in replies[-1]
        await plugin.handle_command(text="/helldivers push patch", stream_id="s")
        assert "6.3.1" in replies[-1] and "【强制推送·补丁】" in replies[-1]
        await plugin.handle_command(text="/helldivers push 热修复", stream_id="s")
        assert "Hotfix 01.003.000" in replies[-1] and "热修复" in replies[-1]
        await plugin.handle_command(text="/helldivers push warbond", stream_id="s")
        assert "Exo Experts Warbond" in replies[-1]
        await plugin.handle_command(text="/helldivers push all", stream_id="s")
        assert "Content Roadmap Revealed" in replies[-1] and "最新公告" in replies[-1]

    asyncio.run(scenario())


def test_explicit_push_category_does_not_broadcast_the_rest_of_the_batch() -> None:
    """``push <type>`` answers with one message, not with the whole new batch."""

    async def scenario() -> None:
        replies: list[str] = []
        batch = [
            entry("v-1", title="Devoid of Liberty: 7.1.0"),
            entry("v-2", title="Hotfix 01.003.000", summary="Hotfix for crashes"),
            entry("v-3", title="Machinery of Oppression: 6.3.1", summary="Patch notes"),
        ]
        plugin = force_push_plugin(replies, [list(batch), list(batch)])
        assert plugin._store is not None
        plugin._store.subscribe("100")
        broadcast: list[str] = []

        async def send(group: str, message: str) -> tuple[bool, str]:
            broadcast.append(message)
            return True, ""

        plugin._send_to_group = send  # type: ignore[method-assign]

        for command, expected in (
            ("/helldivers push patch", "6.3.1"),
            ("/helldivers push hotfix", "01.003.000"),
        ):
            replies.clear()
            broadcast.clear()
            plugin._last_force_check = float("-inf")  # force a real source refresh
            ok, _, _ = await plugin.handle_command(text=command, group_id="100", stream_id="s")
            assert ok
            assert broadcast == [], f"{command} 把本轮其它公告也灌进群了：{broadcast}"
            assert len(replies) == 1 and expected in replies[0]

    asyncio.run(scenario())


def test_explicit_push_keeps_the_backlog_without_repeating_itself() -> None:
    """The silent refresh must not lose announcements, nor re-send the pushed one."""

    async def scenario() -> None:
        replies: list[str] = []
        batch = [
            entry("v-1", title="Devoid of Liberty: 7.1.0"),
            entry("v-2", title="Hotfix 01.003.000", summary="Hotfix for crashes"),
            entry("v-3", title="Machinery of Oppression: 6.3.1", summary="Patch notes"),
        ]
        plugin = force_push_plugin(replies, [list(batch), list(batch)])
        assert plugin._store is not None
        plugin._store.subscribe("100")
        broadcast: list[str] = []

        async def send(group: str, message: str) -> tuple[bool, str]:
            broadcast.append(message)
            return True, ""

        plugin._send_to_group = send  # type: ignore[method-assign]

        ok, _, _ = await plugin.handle_command(text="/helldivers push patch", group_id="100", stream_id="s")
        assert ok and len(replies) == 1 and "Machinery of Oppression" in replies[0]
        assert broadcast == []

        # The scheduled poll takes over the backlog...
        assert await plugin.poll_once() == 2
        delivered = "\n".join(broadcast)
        assert "Devoid of Liberty: 7.1.0" in delivered
        assert "Hotfix 01.003.000" in delivered
        # ...but never repeats the card the command already answered with.
        assert "Machinery of Oppression" not in delivered

    asyncio.run(scenario())


def test_bare_push_still_announces_the_whole_batch() -> None:
    """The default category mirrors the automatic feed, so it keeps broadcasting."""

    async def scenario() -> None:
        replies: list[str] = []
        batch = [
            entry("v-1", title="Devoid of Liberty: 7.1.0"),
            entry("v-2", title="Hotfix 01.003.000", summary="Hotfix for crashes"),
        ]
        plugin = force_push_plugin(replies, [list(batch)])
        assert plugin._store is not None
        plugin._store.subscribe("100")
        broadcast: list[str] = []

        async def send(group: str, message: str) -> tuple[bool, str]:
            broadcast.append(message)
            return True, ""

        plugin._send_to_group = send  # type: ignore[method-assign]

        ok, _, _ = await plugin.handle_command(text="/helldivers push", group_id="100", stream_id="s")
        assert ok
        assert len(broadcast) == 2
        delivered = "\n".join(broadcast)
        assert "Devoid of Liberty: 7.1.0" in delivered and "Hotfix 01.003.000" in delivered

    asyncio.run(scenario())


def test_bare_category_is_shorthand_for_push_and_unknown_is_rejected() -> None:
    async def scenario() -> None:
        replies: list[str] = []
        plugin = force_push_plugin(replies, [])
        assert plugin._store is not None
        plugin._store.upsert(entry("release", title="Devoid of Liberty: 7.1.0"), "sent")

        ok, _, _ = await plugin.handle_command(text="/helldivers push banana", stream_id="s")
        assert ok is False and "未知的推送类型" in replies[-1]
        ok, _, _ = await plugin.handle_command(text="/helldivers 版本", stream_id="s")
        assert ok and "Devoid of Liberty: 7.1.0" in replies[-1]

    asyncio.run(scenario())


def test_explicit_push_category_returns_content_even_when_already_delivered() -> None:
    async def scenario() -> None:
        replies: list[str] = []
        plugin = force_push_plugin(replies, [])
        assert plugin._store is not None
        store = plugin._store
        store.subscribe("100")
        patch = entry("patch-1", title="Machinery of Oppression: 6.3.1", summary="Patch notes")
        store.upsert(patch, "sent")
        store.ensure_deliveries(patch.key, ["100"])
        store.mark_delivery(patch.key, "100", True)

        # First call refreshes the sources, the second one runs inside the cooldown.
        await plugin.handle_command(text="/helldivers push patch", group_id="100", stream_id="s")
        assert "6.3.1" in replies[-1] and "已在本轮推送" not in replies[-1]
        await plugin.handle_command(text="/helldivers push patch", group_id="100", stream_id="s")
        assert "6.3.1" in replies[-1] and "已在本轮推送" not in replies[-1]

    asyncio.run(scenario())


def test_help_with_topic_documents_that_command() -> None:
    async def scenario() -> None:
        replies: list[str] = []
        plugin = command_plugin(replies)
        ok, _, _ = await plugin.handle_command(text="/helldivers help push", stream_id="s")
        assert ok and "立即推送，跳过定时轮询" in replies[-1] and "warbond" in replies[-1]
        ok, _, _ = await plugin.handle_command(text="/helldivers help 订阅", stream_id="s")
        assert ok and replies[-1].startswith("【/helldivers subscribe】")
        # A push category documents the push topic.
        ok, _, _ = await plugin.handle_command(text="/helldivers help 补丁", stream_id="s")
        assert ok and "hotfix" in replies[-1] and "【/helldivers push" in replies[-1]
        # No topic still returns the whole list.
        ok, _, _ = await plugin.handle_command(text="/helldivers help", stream_id="s")
        assert ok and "用法：" in replies[-1]
        # An unknown topic falls back to the list with a hint.
        ok, _, _ = await plugin.handle_command(text="/helldivers help banana", stream_id="s")
        assert ok and "没有「banana」这个主题" in replies[-1] and "用法：" in replies[-1]
        assert len(replies) == 5

    asyncio.run(scenario())


def test_parse_localized_steam_feed_keeps_only_translated_versions() -> None:
    mapping = parse_localized_steam_feed(LOCALIZED_RSS)
    assert set(mapping) == {"7.1.0"}
    assert mapping["7.1.0"][0] == "自由入寂：7.1.0"
    assert "社区反馈改动" in mapping["7.1.0"][1]
    assert parse_localized_steam_feed("<not xml") == {}


def test_collect_entries_attaches_the_localized_release_text() -> None:
    async def scenario() -> None:
        plugin = HelldiversPlugin()
        plugin._cfg = {"steam_enabled": True, "wiki_enabled": False}

        async def fetch_json(url: str, params: object = None) -> dict:
            return {
                "appnews": {
                    "newsitems": [
                        {
                            "gid": "42",
                            "title": "Devoid of Liberty: 7.1.0",
                            "contents": "*** INCOMING TRANSMISSION *** Community Feedback Changes",
                            "feedname": "steam_community_announcements",
                            "date": 1780000000,
                        }
                    ]
                }
            }

        async def fetch_text(url: str) -> str:
            assert "l=schinese" in url and "cc=CN" in url
            return LOCALIZED_RSS

        plugin._fetch_json = fetch_json  # type: ignore[method-assign]
        plugin._fetch_text = fetch_text  # type: ignore[method-assign]
        got = await plugin.collect_entries()
        assert len(got) == 1
        assert got[0].title_zh == "自由入寂：7.1.0" and "社区反馈改动" in got[0].summary_zh
        assert got[0].title == "Devoid of Liberty: 7.1.0"  # the English original is kept
        # Localization must never change the dedupe key.
        assert replace(got[0], title_zh="", summary_zh="").key == got[0].key

    asyncio.run(scenario())


def test_localization_feed_can_be_turned_off() -> None:
    async def scenario() -> None:
        plugin = HelldiversPlugin()
        plugin._cfg = {"steam_enabled": True, "wiki_enabled": False, "steam_locale": ""}
        called = False

        async def fetch_text(url: str) -> str:
            nonlocal called
            called = True
            return LOCALIZED_RSS

        async def fetch_json(url: str, params: object = None) -> dict:
            return {"appnews": {"newsitems": []}}

        plugin._fetch_text = fetch_text  # type: ignore[method-assign]
        plugin._fetch_json = fetch_json  # type: ignore[method-assign]
        await plugin.collect_entries()
        assert called is False

    asyncio.run(scenario())


def test_format_entry_is_chinese_with_beijing_time() -> None:
    item = entry("fmt-1", title="Devoid of Liberty: 7.1.1", summary="Hotfix for crashes")
    text = format_entry(item)
    assert text.startswith("【绝地潜兵 2·热修复】Devoid of Liberty: 7.1.1")
    assert "发布时间：2026-09-28 08:00（北京时间）" in text

    localized = replace(item, title_zh="自由入寂：7.1.1", summary_zh="修复了使用终端时的崩溃")
    text = format_entry(localized)
    assert "自由入寂：7.1.1" in text and "修复了使用终端时的崩溃" in text
    assert "Hotfix for crashes" not in text


def test_extract_wiki_section_keeps_nested_subsections() -> None:
    body = extract_wiki_section(WIKI_PAGE_WIKITEXT, ("balancing", "数值"))
    # A === subsection must not terminate the == section that owns it.
    assert "durable damage" in body
    assert "Increased refill from 1 to 2" in body
    assert "Orbital Laser cooldown reduced from 240 to 180" in body
    assert "Fixed a crash when opening terminals" not in body
    assert extract_wiki_section("== Other ==\ntext", ("balancing",)) == ""

    zh_body = extract_wiki_section(WIKI_ZH_PAGE_WIKITEXT, WIKI_SECTION_KEYWORDS)
    assert "备弹数量从 1 提升至 2" in zh_body
    assert "轨道激光冷却从 240 秒降至 180 秒" in zh_body
    assert "修复了打开终端时的崩溃问题" not in zh_body


def test_wikitext_to_plain_flattens_markup() -> None:
    text = wikitext_to_plain(extract_wiki_section(WIKI_PAGE_WIKITEXT, ("balancing",)))
    assert "{{Infobox" not in text
    assert "[[" not in text and "]]" not in text
    assert "'''" not in text
    assert "· Increased refill from 1 to 2" in text
    assert "Liberator damage reduced from 200 to 180" in text


def test_format_entry_appends_the_wiki_detail_link() -> None:
    item = entry("fmt-2", title="Machinery of Oppression: 6.3.0")
    assert "详细数值调整" not in format_entry(item)
    linked = format_entry(item, "https://helldivers.wiki.gg/wiki/1.006.300")
    assert "详细数值调整：https://helldivers.wiki.gg/wiki/1.006.300" in linked
    assert linked.index("详细数值调整") < linked.index("发布时间")


def test_auto_push_attaches_the_wiki_detail_link() -> None:
    async def scenario() -> None:
        replies: list[str] = []
        plugin = command_plugin(replies)
        plugin._cfg = {"default_groups": ["100"]}
        plugin._first_run = False
        plugin._store.subscribe("100")
        sent: list[str] = []

        async def collect() -> list[UpdateEntry]:
            return [entry("v-9", title="Machinery of Oppression: 6.3.0")]

        async def send(group: str, message: str) -> tuple[bool, str]:
            sent.append(message)
            return True, ""

        plugin._fetch_json = wiki_fetcher(zh_page="1.006.300")  # type: ignore[method-assign]
        plugin.collect_entries = collect  # type: ignore[method-assign]
        plugin._send_to_group = send  # type: ignore[method-assign]
        assert await plugin.poll_once() == 1
        assert len(sent) == 1
        # The Chinese wiki wins when it has the page.
        assert "详细数值调整：https://helldivers.wiki.gg/zh/wiki/1.006.300" in sent[0]

    asyncio.run(scenario())


def test_push_wiki_sends_the_recent_changes_feed() -> None:
    async def scenario() -> None:
        replies: list[str] = []
        plugin = command_plugin(replies)
        plugin._fetch_json = wiki_fetcher(recent=RECENT_CHANGES)  # type: ignore[method-assign]

        ok, _, _ = await plugin.handle_command(text="/helldivers push wiki", stream_id="s")
        assert ok
        text = replies[-1]
        assert text.startswith("【绝地潜兵 2·Wiki 最近更改】共 2 条最新编辑")
        assert "· 1.007.101 — 09-24 17:00 — /* 平衡性调整 */ 更新数值（补丁相关）" in text
        assert "· R-4 Hyena — 09-23 18:00" in text
        assert "其中 1 条与补丁/更新相关" in text
        assert "https://helldivers.wiki.gg/zh/wiki/Special:RecentChanges" in text
        assert "来源：helldivers.wiki.gg 中文站（CC BY-SA 4.0）" in text
        # A site-wide feed needs no stored announcement.
        assert len(replies) == 1

    asyncio.run(scenario())


def test_push_wiki_reports_an_unreachable_feed() -> None:
    async def scenario() -> None:
        replies: list[str] = []
        plugin = command_plugin(replies)  # _fetch_json raises offline
        ok, _, _ = await plugin.handle_command(text="/helldivers 最近更改", stream_id="s")
        assert ok and "暂时取不到" in replies[-1]

    asyncio.run(scenario())


def test_push_balance_prefers_the_chinese_wiki_without_any_translation() -> None:
    async def scenario() -> None:
        replies: list[str] = []
        plugin = command_plugin(replies)
        plugin._store.upsert(entry("v-9", title="Machinery of Oppression: 6.3.0"), "sent")
        plugin._fetch_json = wiki_fetcher(zh_page="1.006.300")  # type: ignore[method-assign]

        ok, _, _ = await plugin.handle_command(text="/helldivers push balance", stream_id="s")
        assert ok
        text = replies[-1]
        assert text.startswith("【绝地潜兵 2·数值调整】Machinery of Oppression: 6.3.0")
        assert "备弹数量从 1 提升至 2" in text and "轨道激光冷却从 240 秒降至 180 秒" in text
        assert "Increased refill from 1 to 2" not in text
        assert "https://helldivers.wiki.gg/zh/wiki/1.006.300" in text
        assert "来源：helldivers.wiki.gg 中文站（CC BY-SA 4.0）" in text

    asyncio.run(scenario())


def test_push_balance_falls_back_to_the_english_wiki() -> None:
    async def scenario() -> None:
        replies: list[str] = []
        plugin = command_plugin(replies)
        plugin._store.upsert(entry("v-9", title="Machinery of Oppression: 6.3.0"), "sent")
        # The Chinese wiki lags behind on recent releases.
        plugin._fetch_json = wiki_fetcher(en_page="1.006.300")  # type: ignore[method-assign]

        ok, _, _ = await plugin.handle_command(text="/helldivers push 数值", stream_id="s")
        assert ok
        text = replies[-1]
        assert "Increased refill from 1 to 2" in text
        assert "https://helldivers.wiki.gg/wiki/1.006.300" in text
        assert "/zh/wiki/" not in text
        assert "来源：helldivers.wiki.gg 英文站（CC BY-NC-SA 4.0）" in text

    asyncio.run(scenario())


def test_push_balance_reports_a_missing_page() -> None:
    async def scenario() -> None:
        replies: list[str] = []
        plugin = command_plugin(replies)
        plugin._store.upsert(entry("v-9", title="Machinery of Oppression: 6.3.0"), "sent")
        plugin._fetch_json = wiki_fetcher()  # type: ignore[method-assign]

        ok, _, _ = await plugin.handle_command(text="/helldivers push balance", stream_id="s")
        assert ok and "没有在 Wiki 上找到" in replies[-1]

    asyncio.run(scenario())


def test_loadout_pool_reads_every_slot() -> None:
    pool = load_loadout_pool()
    assert set(pool) == {"primary", "secondary", "grenade", "booster", "stratagem"}
    assert {slot: len(names) for slot, names in pool.items()} == {
        "primary": 55,
        "secondary": 25,
        "grenade": 23,
        "booster": 20,
        "stratagem": 93,
    }
    all_names = [name for names in pool.values() for name in names]
    assert all(isinstance(name, str) and name.strip() for name in all_names)
    assert len(all_names) == len(set(all_names)), "装备名称必须全局唯一"

    current_names = set(all_names)
    assert {
        "AR-2 野狼",
        "SMG/FLAM-34 司炉者",
        "CQC-73 堑壕工具",
        "AR-11 Arbitrator",
        "P-34 Breacher",
        "G-60 Anti-Tank Seeker",
        "Surplus EAT Allocation",
        "Integrated Extinguishers",
        "40-K 热熔枪",
        "GR-8 无后坐力炮",
        "M-1000 重装机枪",
        "MS-11 单兵导弹发射井",
        "S-11 矛枪",
        "GL-28 弹链式榴弹发射器背包",
        "TD-110 Maelstrom",
    } <= current_names
    assert {"AR-2 郊狼", "FLAM-34 炉管者", "CQC-72 堑壕工具"}.isdisjoint(current_names)
    assert load_loadout_pool("does-not-exist.json") == {}


def test_generate_loadout_fills_eight_slots_without_duplicates() -> None:
    pool = {
        "primary": ["主武器甲"],
        "secondary": ["副武器乙"],
        "grenade": ["投掷物丙"],
        "booster": ["强化资源丁"],
        "stratagem": ["战备1", "战备2", "战备3", "战备4", "战备5"],
    }
    loadout = generate_loadout(pool, random.Random(7))
    assert loadout["primary"] == "主武器甲" and loadout["booster"] == "强化资源丁"
    stratagems = [loadout[f"stratagem_{index}"] for index in range(LOADOUT_STRATAGEM_COUNT)]
    assert len(set(stratagems)) == LOADOUT_STRATAGEM_COUNT
    assert set(stratagems) <= set(pool["stratagem"])
    assert generate_loadout({}, random.Random(1)) == {}


LOADOUT_SAMPLE = {
    "primary": "AR-23 解放者",
    "secondary": "P-2 和平制造者",
    "grenade": "G-12 高爆弹",
    "booster": "UAV侦察强化",
    "stratagem_0": "MG-43 机枪",
    "stratagem_1": "“飞鹰”500KG炸弹",
    "stratagem_2": "轨道激光炮",
    "stratagem_3": "SH-20 防弹护盾背包",
}


def test_render_loadout_html_inlines_icons_without_touching_the_network() -> None:
    icons = {name: "data:image/webp;base64,AAAA" for name in LOADOUT_SAMPLE.values()}
    page = render_loadout_html(LOADOUT_SAMPLE, motto="为了民主", icons=icons)
    for name in LOADOUT_SAMPLE.values():
        assert name in page
    assert "绝地潜兵 2 随机配装" in page and "常规装备" in page and "战略配备" in page
    # Every card carries its reroll number as a badge, 1-8 in reading order.
    badges = re.findall(r'<b class="idx">(\d)</b>', page)
    assert badges == [str(index) for index in range(1, 9)]
    assert "为了民主" in page
    assert page.count('<img src="data:image/webp;base64,AAAA" alt="">') == len(LOADOUT_SAMPLE)
    # allow_network is off on the render capability, so nothing may be fetched.
    for marker in ("@import", "http://", "https://"):
        assert marker not in page


def test_render_loadout_html_without_icons_omits_images() -> None:
    page = render_loadout_html(LOADOUT_SAMPLE, motto="为了民主")
    assert "<img" not in page and "src=" not in page
    # A slot with no icon still shows its name rather than an empty frame.
    assert "AR-23 解放者" in page


def test_render_loadout_html_survives_an_item_missing_from_the_icon_file() -> None:
    sample = dict(LOADOUT_SAMPLE)
    sample["primary"] = "AR-11 Arbitrator"
    page = render_loadout_html(sample, icons={"MG-43 机枪": "data:image/webp;base64,BBBB"})
    assert page.count("<img") == 1
    assert "MG-43 机枪" in page and "轨道激光" in page and "AR-11 Arbitrator" in page
    assert (
        '<div class="card noicon"><div class="slot"><b class="idx">1</b>主武器</div>'
        '<div class="name">AR-11 Arbitrator</div></div>'
    ) in page


def test_loadout_icons_cover_legacy_items_and_leave_new_items_name_only() -> None:
    pool = load_loadout_pool()
    icons = load_loadout_icons()
    names = {name for names in pool.values() for name in names}
    missing = {name for name in names if name not in icons}
    assert missing == {
        "R-4 鬣狗",
        "R/40-K 高能精确射手步枪",
        "AR-11 Arbitrator",
        "GL-15 Evictor",
        "LAS-12 Sai",
        "P/40-K 爆弹手枪",
        "P-34 Breacher",
        "G/40-K 热熔地雷",
        "G-60 Anti-Tank Seeker",
        "G-8 Immolation",
        "Surplus EAT Allocation",
        "Integrated Extinguishers",
        "B/FLAM-80 焚燃者",
        "“飞鹰”毒气空袭",
        "TD-220 堡垒MK XVI",
        "GL-28 弹链式榴弹发射器背包",
        "TD-110 Maelstrom",
    }
    assert set(icons) <= names, "图标文件不应保留已从目录移除的旧名称"
    assert all(uri.startswith("data:image/") for uri in icons.values())


def test_real_icon_card_references_nothing_outside_itself() -> None:
    """The shipping icon set must keep the card renderable with allow_network off."""
    page = render_loadout_html(LOADOUT_SAMPLE, icons=load_loadout_icons())
    assert page.count('<img src="data:image/') == len(LOADOUT_SAMPLE)
    # Every src/href is a data URI, so a blackholed network cannot change the card.
    assert not re.search(r'(?:src|href)="(?!data:)', page)


def test_loadout_icons_tolerate_a_missing_file() -> None:
    import plugin as plugin_module

    saved = plugin_module._LOADOUT_ICONS_CACHE
    plugin_module._LOADOUT_ICONS_CACHE = None
    try:
        assert plugin_module.load_loadout_icons("does-not-exist.json") == {}
    finally:
        plugin_module._LOADOUT_ICONS_CACHE = saved


def test_loadout_command_sends_an_image() -> None:
    async def scenario() -> None:
        replies: list[str] = []
        plugin = command_plugin(replies)
        rendered: list[str] = []
        sent: list[str] = []

        class FakeRender:
            async def html2png(self, html: str, **kwargs: object) -> dict:
                rendered.append(html)
                return {"image_base64": "AAAA", "mime_type": "image/png"}

        class FakeSend:
            async def image(self, image_data: str, stream_id: str) -> bool:
                sent.append(image_data)
                return True

        class Ctx:
            render = FakeRender()
            send = FakeSend()

        attach_ctx(plugin, Ctx())
        ok, _, _ = await plugin.handle_command(text="/helldivers 配装", stream_id="s")
        assert ok
        assert sent == ["AAAA"] and len(rendered) == 1
        assert "绝地潜兵 2 随机配装" in rendered[0]
        # The shipped icon set is wired in, so the card carries real artwork.
        assert '<img src="data:image/' in rendered[0]
        assert replies == []  # no text fallback once the image went out

    asyncio.run(scenario())


def test_loadout_command_falls_back_to_text_when_rendering_fails() -> None:
    async def scenario() -> None:
        replies: list[str] = []
        plugin = command_plugin(replies)

        class BrokenRender:
            async def html2png(self, html: str, **kwargs: object) -> dict:
                raise RuntimeError("no browser available")

        class Ctx:
            render = BrokenRender()

        attach_ctx(plugin, Ctx())
        ok, _, _ = await plugin.handle_command(text="/helldivers loadout", stream_id="s")
        assert ok and replies
        assert replies[-1].startswith("【绝地潜兵 2·随机配装】")
        assert "主武器：" in replies[-1] and "战略配备：" in replies[-1]
        # The host's reason must travel with the fallback, not vanish into a log.
        assert "no browser available" in replies[-1]
        assert "配装图生成失败" in replies[-1]

    asyncio.run(scenario())


def test_loadout_fallback_surfaces_a_rejected_render_payload() -> None:
    """MaiBot answers a failed render with success=False instead of raising."""

    async def scenario() -> None:
        replies: list[str] = []
        plugin = command_plugin(replies)

        class RejectingRender:
            async def html2png(self, html: str, **kwargs: object) -> dict:
                return {"success": False, "error": "插件运行时浏览器渲染能力已禁用"}

        class Ctx:
            render = RejectingRender()

        attach_ctx(plugin, Ctx())
        await plugin.handle_command(text="/helldivers loadout", stream_id="s")
        assert "插件运行时浏览器渲染能力已禁用" in replies[-1]
        assert plugin._last_render_error == "插件运行时浏览器渲染能力已禁用"

    asyncio.run(scenario())


def test_loadout_fallback_surfaces_a_rejected_image_send() -> None:
    async def scenario() -> None:
        replies: list[str] = []
        plugin = command_plugin(replies)

        class GoodRender:
            async def html2png(self, html: str, **kwargs: object) -> dict:
                return {"image_base64": "AAAA", "mime_type": "image/png"}

        class RefusingSend:
            async def image(self, image_data: str, stream_id: str) -> bool:
                return False

        class Ctx:
            render = GoodRender()
            send = RefusingSend()

        attach_ctx(plugin, Ctx())
        await plugin.handle_command(text="/helldivers loadout", stream_id="s")
        assert "发送图片返回 False" in replies[-1]

    asyncio.run(scenario())


def test_card_stylesheets_are_brace_balanced() -> None:
    """A stray ``}}`` in a non-f-string silently swallows the next CSS rule."""
    pages = (("diag", DIAG_HTML), ("card", render_loadout_html(LOADOUT_SAMPLE)))
    for name, page in pages:
        css = page.split("<style>")[1].split("</style>")[0]
        assert css.count("{") == css.count("}"), f"{name} 的 CSS 花括号不平衡"
        assert page.count("<style>") == 1 and page.count("</style>") == 1


def command_metadata() -> dict:
    """``@Command`` metadata, shape-agnostic across the real SDK and the stub."""
    info = getattr(HelldiversPlugin.handle_command, "__maibot_component_info__", None)
    if info is None:
        return {}
    return dict(getattr(info, "metadata", info) or {})


def test_command_timeout_covers_the_render_budget() -> None:
    """A text-only loadout was really the host's 30 s cap on ``cap.call``."""
    assert RENDER_RPC_TIMEOUT_MS > 30_000  # the host's DEFAULT_COMPONENT_RPC_TIMEOUT_MS
    assert COMMAND_RPC_TIMEOUT_MS > RENDER_RPC_TIMEOUT_MS
    metadata = command_metadata()
    if metadata:  # the stub also records it, so this is not vacuous
        assert metadata["timeout_ms"] == COMMAND_RPC_TIMEOUT_MS


def test_render_raises_the_rpc_timeout_above_the_host_cap() -> None:
    async def scenario() -> None:
        replies: list[str] = []
        plugin = command_plugin(replies)
        calls: list[tuple[str, int, dict]] = []

        class Ctx:
            async def call_capability(
                self, capability: str, timeout_ms: int | None = None, **kwargs: object
            ) -> dict:
                calls.append((capability, int(timeout_ms or 0), dict(kwargs)))
                return {"image_base64": "AAAA", "mime_type": "image/png"}

        attach_ctx(plugin, Ctx())
        ok, _, _ = await plugin.handle_command(text="/helldivers loadout", stream_id="s", user_id="u1")
        assert ok
        assert len(calls) == 1
        capability, timeout_ms, args = calls[0]
        assert capability == "render.html2png"
        assert timeout_ms == RENDER_RPC_TIMEOUT_MS
        # timeout_ms is an RPC argument, never a capability argument.
        assert "timeout_ms" not in args
        assert args["selector"] == "body" and args["full_page"] is True
        assert args["viewport"] == {"width": LOADOUT_CARD_WIDTH, "height": LOADOUT_CARD_HEIGHT}
        assert args["html"].startswith("<!DOCTYPE html>")

    asyncio.run(scenario())


def test_render_falls_back_to_the_typed_proxy_without_call_capability() -> None:
    async def scenario() -> None:
        replies: list[str] = []
        plugin = command_plugin(replies)
        seen: list[dict] = []

        class Render:
            async def html2png(self, html: str, **kwargs: object) -> dict:
                seen.append(dict(kwargs))
                return {"image_base64": "AAAA"}

        class Send:
            async def image(self, image_data: str, stream_id: str) -> bool:
                return True

        class Ctx:
            render = Render()
            send = Send()

        attach_ctx(plugin, Ctx())
        ok, _, _ = await plugin.handle_command(text="/helldivers loadout", stream_id="s")
        assert ok and len(seen) == 1
        assert seen[0]["full_page"] is True and seen[0]["device_scale_factor"] == 1.0

    asyncio.run(scenario())


def test_diag_explains_a_render_timeout() -> None:
    async def scenario() -> None:
        replies: list[str] = []
        plugin = command_plugin(replies)

        class Ctx:
            async def call_capability(
                self, capability: str, timeout_ms: int | None = None, **kwargs: object
            ) -> dict:
                raise RuntimeError(f"[E_TIMEOUT] 请求 cap.call 超时 ({timeout_ms}ms)")

        attach_ctx(plugin, Ctx())
        ok, _, _ = await plugin.handle_command(text="/helldivers diag", stream_id="s", is_local_operator=True)
        assert ok is False
        text = replies[-1]
        assert "渲染超时" in text
        assert str(RENDER_RPC_TIMEOUT_MS // 1000) in text
        assert "Playwright" in text and "Chromium" in text
        assert "E_TIMEOUT" in text  # the host's own words are still shown

    asyncio.run(scenario())


def test_diag_reports_the_render_failure_and_how_to_fix_it() -> None:
    async def scenario() -> None:
        replies: list[str] = []
        plugin, _rendered, sent = loadout_plugin(replies)

        class BrokenRender:
            async def html2png(self, html: str, **kwargs: object) -> dict:
                return {"success": False, "error": "当前环境未安装 Python Playwright"}

        ctx_obj = plugin.ctx


        ctx_obj.render = BrokenRender()  # type: ignore[assignment]
        ok, _, _ = await plugin.handle_command(text="/helldivers 自检", stream_id="s", is_local_operator=True)
        assert ok is False
        text = replies[-1]
        assert text.startswith("【绝地潜兵 2·自检】")
        assert "配装数据：" in text and "装备图标：" in text and "来源开关：" in text
        assert "浏览器渲染：失败" in text
        assert "当前环境未安装 Python Playwright" in text
        assert "plugin_runtime.render.enabled" in text
        assert sent == []  # nothing was sent as an image

    asyncio.run(scenario())


def test_diag_sends_a_real_test_image_when_the_pipeline_works() -> None:
    async def scenario() -> None:
        replies: list[str] = []
        plugin, rendered, sent = loadout_plugin(replies)
        ok, _, _ = await plugin.handle_command(text="/helldivers diag", stream_id="s", is_local_operator=True)
        assert ok
        assert len(rendered) == 1 and "渲染自检 OK" in rendered[0]
        assert sent == ["AAAA"]  # exactly one image, not two
        text = replies[-1]
        assert "浏览器渲染：成功" in text and "图片发送：成功" in text
        assert plugin._last_render_error == ""

    asyncio.run(scenario())


def test_status_surfaces_the_last_image_failure() -> None:
    async def scenario() -> None:
        replies: list[str] = []
        plugin = command_plugin(replies)
        plugin._store.subscribe("100")
        plugin._last_render_error = "插件运行时浏览器渲染能力已禁用"
        await plugin.handle_command(text="/helldivers status", group_id="100", stream_id="s")
        assert "配装出图异常：插件运行时浏览器渲染能力已禁用" in replies[-1]

    asyncio.run(scenario())


def test_format_loadout_numbers_match_the_card_badges() -> None:
    text = format_loadout(LOADOUT_SAMPLE)
    assert "1 主武器：AR-23 解放者" in text
    assert "4 强化资源：UAV侦察强化" in text
    assert "5 战略配备：MG-43 机枪" in text
    assert "8 战略配备：SH-20 防弹护盾背包" in text


def loadout_plugin(replies: list[str]) -> tuple[HelldiversPlugin, list[str], list[str]]:
    """Plugin wired for loadout commands, with rendered HTML and sent images captured."""
    plugin = command_plugin(replies)
    rendered: list[str] = []
    sent: list[str] = []

    class FakeRender:
        async def html2png(self, html: str, **kwargs: object) -> dict:
            rendered.append(html)
            return {"image_base64": "AAAA", "mime_type": "image/png"}

    class FakeSend:
        async def image(self, image_data: str, stream_id: str) -> bool:
            sent.append(image_data)
            return True

    class Ctx:
        render = FakeRender()
        send = FakeSend()

    attach_ctx(plugin, Ctx())
    return plugin, rendered, sent


def attach_ctx(plugin: HelldiversPlugin, ctx: object) -> None:
    """Inject a fake context.

    The stub ``MaiBotPlugin`` accepts ``plugin.ctx = ...``; the real SDK exposes
    ``ctx`` as a read-only property and injects it through ``_set_context``.
    """
    setter = getattr(plugin, "_set_context", None)
    if callable(setter):
        setter(ctx)
        return
    plugin.ctx = ctx  # type: ignore[assignment]


def session_slots(plugin: HelldiversPlugin, scope: str = "s|u1") -> dict[str, str]:
    return dict(plugin._loadout_sessions[scope][0])


def age_session(plugin: HelldiversPlugin, seconds: float, scope: str = "s|u1") -> None:
    loadout, motto, stamp = plugin._loadout_sessions[scope]
    plugin._loadout_sessions[scope] = (loadout, motto, stamp - seconds)


def test_loadout_reroll_redraws_only_the_requested_slot() -> None:
    async def scenario() -> None:
        replies: list[str] = []
        plugin, _rendered, sent = loadout_plugin(replies)
        ok, _, _ = await plugin.handle_command(text="/helldivers loadout", stream_id="s", user_id="u1")
        assert ok and len(sent) == 1
        before = session_slots(plugin)

        # The Chinese alias path must work too.
        ok, _, _ = await plugin.handle_command(text="/helldivers 配装 3", stream_id="s", user_id="u1")
        assert ok
        after = session_slots(plugin)
        assert {key for key in after if after[key] != before[key]} == {"grenade"}
        assert after["grenade"] != before["grenade"]
        assert len(sent) == 2  # the reroll comes back as an image as well
        assert replies == []

    asyncio.run(scenario())


def test_loadout_reroll_keeps_the_four_stratagems_distinct() -> None:
    async def scenario() -> None:
        plugin, _rendered, _sent = loadout_plugin([])
        await plugin.handle_command(text="/helldivers loadout", stream_id="s", user_id="u1")
        before = session_slots(plugin)
        previous = {before[f"stratagem_{index}"] for index in range(LOADOUT_STRATAGEM_COUNT)}

        ok, _, _ = await plugin.handle_command(text="/helldivers loadout 7", stream_id="s", user_id="u1")
        assert ok
        after = session_slots(plugin)
        picked = [after[f"stratagem_{index}"] for index in range(LOADOUT_STRATAGEM_COUNT)]
        assert len(set(picked)) == LOADOUT_STRATAGEM_COUNT
        # Slot 7 is stratagem_2 alone, and it must be a different stratagem.
        assert after["stratagem_2"] not in previous
        assert {key for key in after if after[key] != before[key]} == {"stratagem_2"}

    asyncio.run(scenario())


def test_loadout_reroll_stops_working_after_the_window() -> None:
    async def scenario() -> None:
        replies: list[str] = []
        plugin, _rendered, sent = loadout_plugin(replies)
        await plugin.handle_command(text="/helldivers loadout", stream_id="s", user_id="u1")
        age_session(plugin, LOADOUT_REROLL_TTL_SECONDS + 1)

        ok, _, _ = await plugin.handle_command(text="/helldivers loadout 3", stream_id="s", user_id="u1")
        assert ok is False
        assert "没有可重抽的配装" in replies[-1] and str(LOADOUT_REROLL_TTL_SECONDS) in replies[-1]
        assert len(sent) == 1  # nothing new was drawn
        assert "s|u1" not in plugin._loadout_sessions  # expired sessions are dropped

    asyncio.run(scenario())


def test_loadout_reroll_window_slides_and_accepts_chinese_numerals() -> None:
    async def scenario() -> None:
        plugin, _rendered, sent = loadout_plugin([])
        await plugin.handle_command(text="/helldivers loadout", stream_id="s", user_id="u1")
        # Still inside the window, but only just.
        age_session(plugin, LOADOUT_REROLL_TTL_SECONDS - 5)

        ok, _, _ = await plugin.handle_command(text="/helldivers loadout 三", stream_id="s", user_id="u1")
        assert ok and len(sent) == 2
        _loadout, _motto, stamp = plugin._loadout_sessions["s|u1"]
        # A reroll restarts the countdown, so a chain of rerolls keeps working.
        assert time.monotonic() - stamp < 5

    asyncio.run(scenario())


def test_loadout_reroll_rejects_bad_numbers_and_never_draws_a_fresh_loadout() -> None:
    async def scenario() -> None:
        replies: list[str] = []
        plugin, _rendered, sent = loadout_plugin(replies)
        await plugin.handle_command(text="/helldivers loadout", stream_id="s", user_id="u1")
        before = session_slots(plugin)

        for bad in ("0", "9", "abc"):
            replies.clear()
            ok, _, _ = await plugin.handle_command(text=f"/helldivers loadout {bad}", stream_id="s", user_id="u1")
            assert ok is False, bad
            assert "序号需要在 1-8 之间" in replies[-1]
        assert len(sent) == 1
        assert session_slots(plugin) == before  # a rejected reroll leaves the draw alone

    asyncio.run(scenario())


def test_loadout_reroll_is_scoped_to_the_user_who_drew_it() -> None:
    async def scenario() -> None:
        replies: list[str] = []
        plugin, _rendered, sent = loadout_plugin(replies)
        await plugin.handle_command(text="/helldivers loadout", stream_id="s", user_id="u1")

        ok, _, _ = await plugin.handle_command(text="/helldivers loadout 3", stream_id="s", user_id="u2")
        assert ok is False and "没有可重抽的配装" in replies[-1]
        assert len(sent) == 1
        # A different chat is a different session as well.
        ok, _, _ = await plugin.handle_command(text="/helldivers loadout 3", stream_id="other", user_id="u1")
        assert ok is False and len(sent) == 1

    asyncio.run(scenario())


def test_loadout_without_a_previous_draw_asks_to_roll_first() -> None:
    async def scenario() -> None:
        replies: list[str] = []
        plugin, _rendered, sent = loadout_plugin(replies)
        ok, _, _ = await plugin.handle_command(text="/helldivers loadout 5", stream_id="s", user_id="u1")
        assert ok is False and "请先发 /helldivers loadout" in replies[-1]
        assert sent == []

    asyncio.run(scenario())


def test_help_stays_in_sync_with_the_command_surface() -> None:
    """Guards the drift that left ``loadout`` out of the topic list."""
    plugin = command_plugin([])
    hint = usage_hint()
    for action in sorted(COMMAND_ACTIONS):
        assert action in HELP_TOPICS, f"{action} 没有帮助主题"
        assert f"/helldivers {action}" in hint, f"{action} 不在用法总览里"
    # Every topic must appear in the "可用的主题" line of ``help help``.
    listed = HELP_TOPICS["help"]
    for topic in sorted(HELP_TOPICS):
        assert topic in listed, f"{topic} 未列在 help 主题清单里"
    # Every push category is documented, and every topic offers a Chinese form.
    push_body = plugin._help_text("push")
    for name, label in PUSH_CATEGORY_LABELS.items():
        assert label in push_body, f"推送类型 {name}（{label}）未在 help push 里说明"
        assert name in push_body, f"推送类型键 {name} 未在 help push 里说明"
    for topic in HELP_TOPICS:
        assert "中文写法" in plugin._help_text(topic), f"{topic} 主题没写中文写法"
    # Status output wording must match what the command actually prints.
    assert "无需推送" in plugin._help_text("status")


def test_help_lookup_resolves_topics_aliases_and_categories() -> None:
    plugin = command_plugin([])
    for topic in HELP_TOPICS:
        assert plugin._help_text(topic).startswith("【/helldivers")
    assert plugin._help_text("配装") == plugin._help_text("loadout")
    assert plugin._help_text("订阅") == plugin._help_text("subscribe")
    # A bare push category name documents the push categories.
    assert plugin._help_text("补丁") == plugin._help_text("push")
    assert plugin._help_text("banana").startswith("没有「banana」这个主题")
    assert plugin._help_text("") == usage_hint(subscribed_only=False)


def test_help_renders_the_configured_tunables_instead_of_the_defaults() -> None:
    plugin = command_plugin([])
    plugin._cfg = {
        "poll_interval_seconds": 3600,
        "force_push_cooldown_seconds": 5,
        "loadout_reroll_seconds": 42,
        "wiki_recent_limit": 3,
    }
    assert "3600 秒（1 小时）" in plugin._help_text("subscribe")
    push_body = plugin._help_text("push")
    assert "5 秒" in push_body and "3 条编辑" in push_body
    assert "42 秒" in plugin._help_text("loadout")
    usage = plugin._help_text("")
    assert "42 秒" in usage and "5 秒" in usage
    # A bad hand-edited value falls back to the default instead of raising.
    plugin._cfg["loadout_reroll_seconds"] = "abc"
    assert str(LOADOUT_REROLL_TTL_SECONDS) in plugin._help_text("loadout")


def test_human_seconds_formats_whole_units() -> None:
    assert human_seconds(43200) == "43200 秒（12 小时）"
    assert human_seconds(900) == "900 秒（15 分钟）"
    assert human_seconds(45) == "45 秒"


MANIFEST_PATH = Path(__file__).resolve().parent.parent / "_manifest.json"
PLUGIN_PATH = Path(__file__).resolve().parent.parent / "plugin.py"

# Documented values from the manifest manual (docs.mai-mai.org/plugin/manifest).
PLUGIN_TYPES = frozenset(
    {"adapter", "tool", "provider", "management", "data", "media", "game", "integration", "extension", "other"}
)
# Everything the plugin actually calls through ``ctx``.
CAPABILITIES_IN_USE = frozenset({"chat.open_session", "send.text", "send.image", "render.html2png"})

SEMVER = r"\d+\.\d+\.\d+"


def load_manifest() -> dict:
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def test_manifest_matches_the_documented_schema() -> None:
    """Host 的 ManifestValidator 是严格模式：字段写错会直接被拒绝加载。"""
    manifest = load_manifest()
    assert manifest["manifest_version"] == 2
    assert re.fullmatch(r"[a-z0-9]+(?:[.-][a-z0-9]+)+", manifest["id"]), manifest["id"]
    assert re.fullmatch(SEMVER, manifest["version"]), manifest["version"]
    for key in ("name", "description", "license"):
        assert isinstance(manifest[key], str) and manifest[key].strip(), key

    author = manifest["author"]
    assert author["name"].strip()
    assert author["url"].startswith(("http://", "https://")), "author.url 必填且必须是 URL"

    assert manifest["urls"]["repository"].startswith(("http://", "https://"))
    for key, value in manifest["urls"].items():
        assert value.startswith(("http://", "https://")), key

    for key in ("host_application", "sdk"):
        window = manifest[key]
        assert re.fullmatch(SEMVER, window["min_version"]), (key, window)
        assert re.fullmatch(SEMVER, window["max_version"]), (key, window)
        assert window["min_version"] <= window["max_version"], key

    assert manifest["plugin_type"] in PLUGIN_TYPES, manifest["plugin_type"]
    assert manifest["capabilities"] and all(isinstance(item, str) and item.strip() for item in manifest["capabilities"])
    assert len(set(manifest["capabilities"])) == len(manifest["capabilities"])

    i18n = manifest["i18n"]
    assert i18n["supported_locales"], "supported_locales 非空时 default_locale 必须在其中"
    assert i18n["default_locale"] in i18n["supported_locales"]


def test_runtime_user_agent_matches_manifest_version() -> None:
    assert USER_AGENT.startswith(f"HelldiversPatchFeed/{load_manifest()['version']} ")


def test_manifest_uses_the_repository_owners_identity() -> None:
    """Publication metadata must not drift back to a previous or project-name signature."""
    manifest = load_manifest()
    assert manifest["id"] == "github.touristh.helldivers2-patch-feed"
    assert manifest["author"] == {"name": "TouristH", "url": "https://github.com/TouristH"}
    expected = "https://github.com/TouristH/HelldiversPatchFeed"
    assert manifest["urls"]["repository"] == expected
    assert manifest["urls"]["homepage"] == expected
    assert manifest["urls"]["documentation"] == f"{expected}/blob/main/README.md"
    assert manifest["urls"]["issues"] == f"{expected}/issues"


def test_license_and_third_party_notices_are_published() -> None:
    root = MANIFEST_PATH.parent
    license_text = (root / "LICENSE").read_text(encoding="utf-8")
    notices = (root / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")
    assert load_manifest()["license"] == "MIT"
    assert "MIT License" in license_text and "Copyright (c) 2026 TouristH" in license_text
    assert "Xenfo-LC/Helldivers-2-Random-Loadout-Generator-CN" in notices
    assert "Erlend Dahl" in notices and "Xenfo" in notices
    assert "did not contain a standalone open-source license" in notices
    for revision in ("6540", "6647", "136693", "136929", "133897"):
        assert f"`{revision}`" in notices
    assert "CC BY-SA 4.0" in notices and "CC BY-NC-SA 4.0" in notices
    assert "creativecommons.org/licenses/by-sa/4.0" in notices
    assert "creativecommons.org/licenses/by-nc-sa/4.0" in notices
    assert "loadout_data.json" in notices and "names only" in notices
    contributors = (root / "CONTRIBUTORS.md").read_text(encoding="utf-8")
    assert "TouristH" in contributors and "OpenAI Codex" in contributors


def test_manifest_dependencies_are_well_formed() -> None:
    manifest = load_manifest()
    seen: set[str] = set()
    for dependency in manifest["dependencies"]:
        assert dependency["type"] in {"plugin", "python_package"}, dependency
        assert dependency["version_spec"].strip(), dependency
        key = dependency.get("id") if dependency["type"] == "plugin" else dependency["name"]
        assert key and key != manifest["id"], "不允许自依赖"
        assert key not in seen, f"不允许重复声明依赖：{key}"
        seen.add(key)
        if dependency["type"] == "python_package":
            assert re.fullmatch(r"[A-Za-z0-9._-]+", dependency["name"]), dependency["name"]
        else:
            assert re.fullmatch(r"[a-z0-9]+(?:[.-][a-z0-9]+)+", dependency["id"]), dependency["id"]


def test_manifest_declares_the_python_package_the_host_must_install() -> None:
    """插件自身的 requirements.txt 不被宿主读取；自动安装只认这里的 python_package。"""
    packages = {
        item["name"]: item for item in load_manifest()["dependencies"] if item["type"] == "python_package"
    }
    assert "playwright" in packages, "宿主渲染能力需要 playwright，缺失时渲染必定失败"
    # 约束必须与主程序（playwright>=1.54.0）有交集，否则插件会被拒绝加载。
    assert packages["playwright"]["version_spec"].startswith(">=")


def test_manifest_declares_every_capability_the_plugin_uses() -> None:
    declared = set(load_manifest()["capabilities"])
    assert CAPABILITIES_IN_USE <= declared, f"未声明的能力：{CAPABILITIES_IN_USE - declared}"
    # The raw capability name used by the render call must be declared too.
    source = PLUGIN_PATH.read_text(encoding="utf-8")
    for capability in re.findall(r'call_capability\(\s*"([^"]+)"', source):
        assert capability in declared, f"call_capability 用了未声明的能力：{capability}"


def test_manifest_does_not_claim_locales_it_does_not_ship() -> None:
    """``en-US`` was declared while every user-facing string is Chinese."""
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    i18n = manifest["i18n"]
    locales_dir = MANIFEST_PATH.parent / str(i18n.get("locales_path") or "i18n")
    shipped = {path.stem for path in locales_dir.glob("*.json")} if locales_dir.exists() else set()
    allowed = shipped | {i18n["default_locale"]}
    claimed = set(i18n.get("supported_locales", []))
    assert claimed <= allowed, (
        f"声明了却没有语言文件的语言：{sorted(claimed - allowed)}（要么补 i18n/<locale>.json，要么从 supported_locales 去掉）"
    )


def test_requirements_txt_supports_manual_install_when_auto_install_fails() -> None:
    """requirements.txt 同时列出宿主侧依赖，供用户手动安装。

    Host 自动安装（manifest dependencies）可能因网络等原因失败；requirements.txt
    里列出的 python_package 必须与 manifest 声明保持一致，方便用户
    ``pip install -r requirements.txt`` 兜底。
    """
    text = (MANIFEST_PATH.parent / "requirements.txt").read_text(encoding="utf-8")
    assert "maibot-plugin-sdk" in text
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    packages = [d for d in manifest["dependencies"] if d.get("type") == "python_package"]
    assert packages, "manifest 至少声明一个 python_package"
    for dep in packages:
        pin = f"{dep['name']}{dep['version_spec']}"
        assert pin in text.replace(" ", ""), (
            f"manifest 声明的 {pin} 没有同步写进 requirements.txt，手动安装会漏装"
        )


def config_field_names() -> set[str]:
    """``PluginSection`` field names, with or without pydantic behind it."""
    fields = getattr(PluginSection, "model_fields", None)
    if isinstance(fields, dict):
        return set(fields)
    return {name for name in vars(PluginSection) if not name.startswith("_")}


def config_keys_read_by_code() -> set[str]:
    source = PLUGIN_PATH.read_text(encoding="utf-8")
    keys = set(re.findall(r'_cfg\.get\(\s*"([a-z_0-9]+)"', source))
    keys |= set(re.findall(r'_cfg_int\(\s*"([a-z_0-9]+)"', source))
    keys |= set(re.findall(r'_cfg_bool\(\s*"([a-z_0-9]+)"', source))
    keys |= set(re.findall(r'_cfg_id_list\(\s*"([a-z_0-9]+)"', source))
    return keys


def test_config_model_declares_every_key_the_code_reads() -> None:
    """Guards the old defect: keys were read but never exposed, so nobody could set them."""
    missing = sorted(config_keys_read_by_code() - config_field_names())
    assert not missing, f"代码在读但模型里没有的配置项（WebUI 配不出来）：{missing}"


def test_every_config_field_is_actually_used() -> None:
    """Guards the opposite drift: knobs that exist in the WebUI but change nothing."""
    # ``config_version`` is maintained by the Runner, not read by the plugin logic.
    unused = sorted(config_field_names() - config_keys_read_by_code() - {"config_version"})
    assert not unused, f"声明了但代码从不读取的配置项：{unused}"


def test_example_config_documents_every_field() -> None:
    text = (MANIFEST_PATH.parent / "config.example.toml").read_text(encoding="utf-8")
    missing = sorted(name for name in config_field_names() if f"{name} =" not in text)
    assert not missing, f"config.example.toml 缺少这些配置项：{missing}"


def test_example_config_validates_against_the_model() -> None:
    """The shipped example must parse and contain no keys the model rejects."""
    import tomllib

    with (MANIFEST_PATH.parent / "config.example.toml").open("rb") as handle:
        example = tomllib.load(handle)
    validated = PluginConfig.model_validate(example).model_dump()["plugin"]
    unknown = sorted(set(example["plugin"]) - set(validated))
    assert not unknown, f"config.example.toml 写了模型不认识的键：{unknown}"


def test_config_fields_carry_webui_metadata() -> None:
    fields = getattr(PluginSection, "model_fields", None)
    if not isinstance(fields, dict):
        import pytest

        pytest.skip("未安装 maibot-plugin-sdk，配置模型退化为普通类属性")
    groups = set()
    for name, info in fields.items():
        assert (info.description or "").strip(), f"{name} 缺少 description"
        extra = info.json_schema_extra or {}
        assert str(extra.get("label") or "").strip(), f"{name} 缺少 WebUI label"
        assert extra.get("group"), f"{name} 缺少 WebUI 分组"
        groups.add(extra["group"])
    assert groups == {"基础", "权限", "数据源", "推送与限流", "随机配装"}, groups


def test_config_numeric_fields_declare_sane_bounds() -> None:
    fields = getattr(PluginSection, "model_fields", None)
    if not isinstance(fields, dict):
        import pytest

        pytest.skip("未安装 maibot-plugin-sdk，配置模型退化为普通类属性")
    checked = 0
    for name, info in fields.items():
        if info.annotation not in {int, float}:
            continue
        bounds: dict[str, float] = {}
        for metadata in info.metadata:
            if getattr(metadata, "ge", None) is not None:
                bounds["ge"] = metadata.ge
            if getattr(metadata, "le", None) is not None:
                bounds["le"] = metadata.le
        assert "ge" in bounds and "le" in bounds, f"{name} 数值项应同时声明 ge/le"
        assert bounds["ge"] <= bounds["le"], name
        checked += 1
    assert checked >= 5, "数值型配置项太少，测试可能没生效"


def test_config_defaults_are_validated_by_the_real_model() -> None:
    """With the SDK present, the declared defaults must pass pydantic validation."""
    fields = getattr(PluginSection, "model_fields", None)
    if not isinstance(fields, dict):
        import pytest

        pytest.skip("未安装 maibot-plugin-sdk，配置模型退化为普通类属性")
    instance = PluginSection()
    dumped = instance.model_dump(mode="python")
    assert set(dumped) == config_field_names()
    # Bounds and defaults must agree with what the code falls back to.
    assert instance.poll_interval_seconds == DEFAULT_INTERVAL_SECONDS
    assert instance.force_push_cooldown_seconds == FORCE_PUSH_MIN_INTERVAL_SECONDS
    assert instance.loadout_reroll_seconds == LOADOUT_REROLL_TTL_SECONDS


def test_migration_backfills_the_origin_feed_from_the_steam_url(tmp_path: Path) -> None:
    path = tmp_path / "legacy.sqlite3"
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE entries (
          entry_key TEXT PRIMARY KEY, source TEXT NOT NULL, source_id TEXT NOT NULL,
          content_hash TEXT NOT NULL, title TEXT NOT NULL, summary TEXT NOT NULL,
          url TEXT NOT NULL, published_at TEXT NOT NULL, category TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'pending', first_seen TEXT NOT NULL
        );
        """
    )
    official = "https://steamstore-a.akamaihd.net/news/externalpost/steam_community_announcements/1"
    press = "https://steamstore-a.akamaihd.net/news/externalpost/Rock,_Paper,_Shotgun/2"
    conn.execute(
        "INSERT INTO entries VALUES('k1','steam','1','h1','Devoid of Liberty: 7.1.0','s',?,?, 'update','sent','2026-09-28 00:00:00')",
        (official, "2026-09-25T00:00:00+00:00"),
    )
    conn.execute(
        "INSERT INTO entries VALUES('k2','steam','2','h2','A preview','s',?,?, 'update','sent','2026-09-28 00:00:00')",
        (press, "2026-09-26T00:00:00+00:00"),
    )
    conn.commit()
    conn.close()

    store = SQLiteStore(path)
    rows = {str(row["entry_key"]): row for row in store.latest_entries()}
    assert rows["k1"]["source_feed"] == "steam_community_announcements"
    assert rows["k2"]["source_feed"] == "steam_third_party"
    assert rows["k1"]["tags"] == ""
    assert rows["k1"]["title_zh"] == "" and rows["k1"]["summary_zh"] == ""
    store.close()


def test_poll_loop_survives_a_non_numeric_interval() -> None:
    """A typo in config.toml must not turn the 12 h cycle into a 60 s retry."""

    async def scenario() -> None:
        plugin = command_plugin([])
        plugin._cfg = {"poll_interval_seconds": "十二小时"}
        polls: list[bool] = []

        async def fake_poll(*, deliver: bool = True) -> int:
            polls.append(deliver)
            return 0

        plugin.poll_once = fake_poll  # type: ignore[method-assign]

        slept: list[float] = []
        real_sleep = asyncio.sleep

        async def fake_sleep(seconds: float) -> None:
            slept.append(seconds)
            raise asyncio.CancelledError

        asyncio.sleep = fake_sleep  # type: ignore[assignment]
        try:
            task = asyncio.create_task(plugin._poll_loop())
            try:
                await task
            except asyncio.CancelledError:
                pass
        finally:
            asyncio.sleep = real_sleep
        assert polls == [True], "第一轮仍应采集一次"
        assert slept == [DEFAULT_INTERVAL_SECONDS], f"非法间隔应回落到默认值，实际是 {slept}"

    asyncio.run(scenario())


def test_poll_loop_clamps_an_absurdly_small_interval() -> None:
    """Below the documented floor (60 s) we must not poll faster than allowed."""

    async def scenario() -> None:
        plugin = command_plugin([])
        plugin._cfg = {"poll_interval_seconds": 1}
        slept: list[float] = []
        real_sleep = asyncio.sleep

        async def fake_sleep(seconds: float) -> None:
            slept.append(seconds)
            raise asyncio.CancelledError

        async def fake_poll(*, deliver: bool = True) -> int:
            return 0

        plugin.poll_once = fake_poll  # type: ignore[method-assign]
        asyncio.sleep = fake_sleep  # type: ignore[assignment]
        try:
            task = asyncio.create_task(plugin._poll_loop())
            try:
                await task
            except asyncio.CancelledError:
                pass
        finally:
            asyncio.sleep = real_sleep
        assert slept == [60], f"应被夹到 60 秒，实际 {slept}"

    asyncio.run(scenario())


def test_live_only_push_categories_still_honour_the_cooldown() -> None:
    """``wiki`` / ``balance`` have no stored copy, so they must refuse, not refetch."""

    async def scenario() -> None:
        replies: list[str] = []
        plugin = command_plugin(replies)
        calls: list[str] = []

        async def fake_json(url: str, params: dict | None = None) -> dict:
            calls.append(url)
            return {}

        plugin._fetch_json = fake_json  # type: ignore[method-assign]
        plugin._last_force_check = time.monotonic()  # a check just happened

        for category in ("wiki", "balance"):
            calls.clear()
            replies.clear()
            ok, why, _ = await plugin.handle_command(
                text=f"/helldivers push {category}", group_id="100", stream_id="s"
            )
            assert ok is False, f"{category} 在冷却期内不应成功"
            assert "冷却" in replies[-1]
            assert calls == [], f"{category} 在冷却期内不应请求上游，实际请求了 {calls}"

        # The operator bypasses the cooldown, so the upstream call is allowed again.
        plugin._fetch_json = fake_json  # type: ignore[method-assign]
        calls.clear()
        await plugin.handle_command(
            text="/helldivers push wiki", group_id="100", stream_id="s", is_local_operator=True
        )
        assert calls, "操作员应能绕过冷却"

    asyncio.run(scenario())


def test_database_backed_push_still_answers_inside_the_cooldown() -> None:
    """The cooldown must not take away the cached answer for stored categories."""

    async def scenario() -> None:
        replies: list[str] = []
        plugin = force_push_plugin(replies, [[]])
        assert plugin._store is not None
        plugin._store.upsert(entry("7-1-0", title="Devoid of Liberty: 7.1.0"), "sent")
        plugin._last_force_check = time.monotonic()

        ok, _, _ = await plugin.handle_command(text="/helldivers push version", group_id="100", stream_id="s")
        assert ok and "最近一期" in replies[-1] and "7.1.0" in replies[-1]

    asyncio.run(scenario())


def test_endpoint_config_only_accepts_http_and_https() -> None:
    """``urllib`` would open ``file://``; a configured endpoint must not."""
    assert _endpoint_or_default("https://mirror.test/api", WIKI_API_URL) == ("https://mirror.test/api", "")
    assert _endpoint_or_default("http://mirror.test/api", WIKI_API_URL)[0] == "http://mirror.test/api"
    assert _endpoint_or_default("", WIKI_API_URL) == (WIKI_API_URL, "")
    for hostile in ("file:///etc/passwd", "ftp://x/y", "data:text/plain,x", "C:/Windows/win.ini"):
        url, rejected = _endpoint_or_default(hostile, WIKI_API_URL)
        assert url == WIKI_API_URL, f"{hostile} 应被拒绝"
        assert rejected == hostile


def test_collection_ignores_a_non_http_endpoint() -> None:
    async def scenario() -> None:
        plugin = command_plugin([])
        plugin._cfg = {"steam_endpoint": "file:///etc/passwd", "wiki_endpoint": "file:///etc/hosts", "wiki_enabled": True}
        fetched: list[str] = []

        async def fake_json(url: str, params: dict | None = None) -> dict:
            fetched.append(url)
            return {}

        plugin._fetch_json = fake_json  # type: ignore[method-assign]
        await plugin.collect_entries()
        assert fetched, "应回落到默认端点并继续采集"
        assert all(url.startswith(("http://", "https://")) for url in fetched), fetched

    asyncio.run(scenario())


def test_diag_counts_configured_groups_like_status_does() -> None:
    """``diag`` used to read the subscription table only, disagreeing with status."""

    async def scenario() -> None:
        replies: list[str] = []
        plugin = gated_plugin(replies, default_groups=["999"], require_subscription=False)
        assert plugin._store is not None
        plugin._store.subscribe("100")
        await plugin.handle_command(
            text="/helldivers diag", group_id="100", stream_id="s", is_local_operator=True
        )
        diag_line = next(line for line in replies[-1].splitlines() if "订阅群" in line)
        assert "2" in diag_line, diag_line
        assert "【绝地潜兵 2·自检】" in replies[-1]

    asyncio.run(scenario())


def test_card_icon_attribute_is_escaped() -> None:
    """The icon lands in an HTML attribute, so a hand-edited file cannot break out."""
    hostile = 'data:image/png;base64,x" onerror="alert(1)'
    html_text = render_loadout_html(
        {"primary": "AR-23", "secondary": "", "grenade": "", "booster": ""},
        icons={"AR-23": hostile},
    )
    assert '" onerror="' not in html_text, "图标属性里的引号必须被转义"
    assert "&quot;" in html_text


def test_every_command_is_exactly_one_of_functional_or_admin() -> None:
    """The two permission classes must partition the command surface."""
    assert not (FUNCTIONAL_ACTIONS & ADMIN_ACTIONS), "同一指令不能既是功能又是管理"
    assert FUNCTIONAL_ACTIONS | ADMIN_ACTIONS == COMMAND_ACTIONS, (
        f"未分类的指令：{sorted(COMMAND_ACTIONS - FUNCTIONAL_ACTIONS - ADMIN_ACTIONS)}"
    )
    assert ADMIN_ACTIONS, "至少要有一个管理指令"


def test_functional_commands_are_open_to_anyone() -> None:
    plugin = command_plugin([])
    for action in sorted(FUNCTIONAL_ACTIONS):
        assert plugin._command_permission({"user_id": "100"}, action) == "", f"{action} 不该拦普通用户"


def test_management_commands_need_the_whitelist() -> None:
    plugin = command_plugin([])
    plugin._cfg = {"admin_users": ["900"]}
    for action in sorted(ADMIN_ACTIONS):
        assert plugin._command_permission({"user_id": "100"}, action), f"{action} 应拦住普通用户"
        assert plugin._command_permission({"user_id": "900"}, action) == "", f"{action} 应放行管理员"
        assert plugin._command_permission({"user_id": "100", "is_local_operator": True}, action) == ""


def test_admin_whitelist_accepts_unquoted_numbers() -> None:
    """``admin_users = [123456789]`` in TOML is an integer list, not a string list."""
    section = PluginConfig.model_validate({"plugin": {"admin_users": [123456789]}}).plugin
    assert section.admin_users == ["123456789"]


def test_blacklist_blocks_functional_commands_only() -> None:
    plugin = command_plugin([])
    plugin._cfg = {"command_blacklist": {"*": ["800"], "push": ["700"]}}
    # Globally blocked.
    for action in sorted(FUNCTIONAL_ACTIONS):
        assert plugin._command_permission({"user_id": "800"}, action), f"{action} 应被全局黑名单拦住"
    # Blocked for exactly one command.
    assert plugin._command_permission({"user_id": "700"}, "push")
    for action in sorted(FUNCTIONAL_ACTIONS - {"push"}):
        assert plugin._command_permission({"user_id": "700"}, action) == "", f"{action} 不该受影响"
    # Anyone not listed is unaffected.
    assert plugin._command_permission({"user_id": "100"}, "push") == ""


def test_blacklist_keys_accept_action_aliases() -> None:
    plugin = command_plugin([])
    for key in ("推送", "p", "push"):
        plugin._cfg = {"command_blacklist": {key: ["700"]}}
        assert plugin._command_permission({"user_id": "700"}, "push"), f"别名 {key} 没生效"


def test_admin_whitelist_wins_over_the_blacklist() -> None:
    plugin = command_plugin([])
    plugin._cfg = {"admin_users": ["900"], "command_blacklist": {"*": ["900"]}}
    for action in sorted(COMMAND_ACTIONS):
        assert plugin._command_permission({"user_id": "900"}, action) == "", f"{action} 应放行管理员"


def test_permission_denial_reaches_the_group_without_side_effects() -> None:
    async def scenario() -> None:
        replies: list[str] = []
        plugin = gated_plugin(replies, require_subscription=False)
        ok, why, _ = await plugin.handle_command(
            text="/helldivers diag", group_id="100", stream_id="s", user_id="100"
        )
        assert ok is False and "管理指令" in replies[-1]
        assert why
        # A denied command must not reach the renderer at all.
        assert plugin._last_render_error == ""

        # Blacklisted users get their own message.
        plugin._cfg = {"require_subscription": False, "command_blacklist": {"*": ["800"]}}
        replies.clear()
        ok, _, _ = await plugin.handle_command(
            text="/helldivers subscribe", group_id="100", stream_id="s", user_id="800"
        )
        assert ok is False and "禁止" in replies[-1]
        assert plugin._store is not None and plugin._store.groups() == []

    asyncio.run(scenario())


def test_admin_commands_are_labelled_in_the_help() -> None:
    plugin = command_plugin([])
    assert "管理指令" in plugin._usage_hint()
    for action in sorted(ADMIN_ACTIONS):
        assert "管理指令" in plugin._help_text(action), f"{action} 的帮助没说是管理指令"


def test_blacklist_config_only_names_real_commands() -> None:
    """A typo'd key would silently do nothing, so keep the docs honest."""
    plugin = command_plugin([])
    plugin._cfg = {"command_blacklist": {"推送": ["1"], "*": ["2"], "不存在的指令": ["3"]}}
    parsed = plugin._command_blacklist()
    assert parsed["push"] == {"1"}
    assert parsed["*"] == {"2"}
    # Unknown keys are still carried (harmless) but never match a real action.
    assert "不存在的指令" in parsed
    assert plugin._command_permission({"user_id": "1"}, "push")
