#!/usr/bin/env python3
"""Rebuild the random-loadout catalog and embedded icon bundle.

The Chinese Helldivers Wiki is authoritative for display names and is also the
only endpoint used to resolve/download image files.  English list pages are
used solely to discover items that have not reached the Chinese catalog yet.

This is a maintainer tool, not a runtime dependency of the MaiBot plugin.
Install ``beautifulsoup4`` from ``requirements-dev.txt`` before running it.
"""

from __future__ import annotations

import argparse
import base64
import concurrent.futures
import datetime as dt
import hashlib
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

try:
    from bs4 import BeautifulSoup
except ImportError as exc:  # pragma: no cover - maintainer-facing guard
    raise SystemExit(
        "缺少 beautifulsoup4；请先执行 python -m pip install -r requirements-dev.txt"
    ) from exc


ROOT = Path(__file__).resolve().parents[1]
DATA_PATH = ROOT / "loadout_data.json"
ICONS_PATH = ROOT / "loadout_icons.json"

ZH_API = "https://helldivers.wiki.gg/zh/api.php"
EN_API = "https://helldivers.wiki.gg/api.php"
ZH_ROOT = "https://helldivers.wiki.gg/zh/wiki/"
EN_ROOT = "https://helldivers.wiki.gg/wiki/"
USER_AGENT = (
    "HelldiversPatchFeed-loadout-refresh/0.2.1 "
    "(https://github.com/TouristH/HelldiversPatchFeed)"
)
SLOTS = ("primary", "secondary", "grenade", "booster", "stratagem")
EXPECTED_COUNTS = {
    "primary": 55,
    "secondary": 25,
    "grenade": 23,
    "booster": 20,
    "stratagem": 93,
}

# The Chinese Wiki currently has no Booster catalog.  These established
# Chinese labels are therefore maintained explicitly instead of pretending
# that they came from a Chinese Wiki page.
BOOSTER_ZH = {
    "Hellpod Space Optimization": "绝地喷射舱空间优化",
    "Vitality Enhancement": "生命强化",
    "UAV Recon Booster": "UAV侦察强化",
    "Stamina Enhancement": "耐力强化",
    "Muscle Enhancement": "肌肉强化",
    "Increased Reinforcement Budget": "提高增援预算",
    "Flexible Reinforcement Budget": "灵活增援预算",
    "Localization Confusion": "定位混淆",
    "Expert Extraction Pilot": "专业撤离飞行员",
    "Motivational Shocks": "激励性冲击",
    "Experimental Infusion": "实验性注射剂",
    "Firebomb Hellpods": "火焰喷射仓",
    "Dead Sprint": "死亡冲刺",
    "Armed Resupply Pods": "武装补给仓",
    "Sample Extricator": "样本拯救者",
    "Sample Scanner": "样本扫描仪",
    "Stun Pods": "眩晕喷射仓",
    "Concealed Insertion": "隐蔽上阵",
    "Surplus EAT Allocation": "额外消耗性反坦克武器配额",
    "Integrated Extinguishers": "集成灭火器",
}

# These entries were present on the English catalog but did not have a usable
# Chinese Wiki title on the refresh date.  Keep the translation status in the
# generated database so downstream users can distinguish community wording
# from Wiki wording.
COMMUNITY_ZH = {
    "AR-11 Arbitrator": "AR-11 仲裁者",
    "GL-15 Evictor": "GL-15 驱逐者",
    "LAS-12 Sai": "LAS-12 铁尺",
    "P-34 Breacher": "P-34 破门者",
    "G-60 Anti-Tank Seeker": "G-60 反坦克寻踪者",
    "G-8 Immolation": "G-8 焚祭",
    "Surplus EAT Allocation": BOOSTER_ZH["Surplus EAT Allocation"],
    "Integrated Extinguishers": BOOSTER_ZH["Integrated Extinguishers"],
    "A/GM-17 Gas Mortar Sentry": "A/GM-17 毒气迫击哨戒炮",
    "TD-110 Maelstrom": "TD-110 大漩涡",
}

# Some translated Chinese Wiki articles still keep an English infobox title.
# These labels mirror the established Chinese terminology used across the Wiki
# and this plugin.  They stay separately tagged as curated translations.
STRATAGEM_CURATED_ZH = {
    "Orbital EMS Strike": "轨道电磁冲击波攻击",
    "Orbital 380mm HE Barrage": "轨道380毫米高爆弹火力网",
    "Orbital Walking Barrage": "轨道游走火力网",
    "Orbital Laser": "轨道激光炮",
    "Orbital Napalm Barrage": "轨道凝固汽油弹火力网",
    "Orbital Railcannon Strike": "轨道磁轨炮攻击",
    "B-1 Supply Pack": "B-1 补给背包",
    "LIFT-850 Jump Pack": "LIFT-850 喷射背包",
    "SH-20 Ballistic Shield Backpack": "SH-20 防弹护盾背包",
    "AX/AR-23 Guard Dog": "AX/AR-23 “护卫犬”",
    "AX/LAS-5 Rover": "AX/LAS-5 “护卫犬”漫游者",
    "SH-32 Shield Generator Pack": "SH-32 防护罩生成背包",
    "SH-51 Directional Shield": "SH-51 定向护盾",
    "AX/FLAM-75 Hot Dog": "AX/FLAM-75 热狗",
    "B-100 Portable Hellbomb": "B-100 便携式地狱火炸弹",
    "AX/ARC-3 K-9": "AX/ARC-3 “护卫犬”K-9",
    "LIFT-860 Hover Pack": "LIFT-860 悬浮背包",
    "AX/TX-13 Dog Breath": "AX/TX-13“护卫犬”腐息",
    "LIFT-182 Warp Pack": "LIFT-182 传送背包",
    "A/MG-43 Machine Gun Sentry": "A/MG-43 哨戒机枪",
    "A/G-16 Gatling Sentry": "A/G-16 加特林哨戒炮",
    "A/AC-8 Autocannon Sentry": "A/AC-8 自动哨戒炮",
    "A/M-12 Mortar Sentry": "A/M-12 迫击哨戒炮",
    "A/MLS-4X Rocket Sentry": "A/MLS-4X 火箭哨戒炮",
    "A/ARC-3 Tesla Tower": "A/ARC-3 特斯拉塔",
    "A/M-23 EMS Mortar Sentry": "A/M-23 电磁冲击波迫击哨戒炮",
    "A/LAS-98 Laser Sentry": "A/LAS-98 激光哨戒炮",
    "A/FLAM-40 Flame Sentry": "A/FLAM-40 火焰喷射哨戒炮",
}


@dataclass(frozen=True)
class ListedItem:
    name_en: str
    page_en: str
    icon_file: str
    slot: str


@dataclass(frozen=True)
class ZhPage:
    title: str
    revision: int
    wikitext: str


def has_han(text: str) -> bool:
    return bool(re.search(r"[\u3400-\u4dbf\u4e00-\u9fff]", text))


def clean_wikitext_value(value: str) -> str:
    value = re.sub(r"<!--.*?-->", "", value, flags=re.S)
    value = re.sub(r"\[\[(?:[^]|]+\|)?([^]]+)\]\]", r"\1", value)
    value = value.replace("'''", "").replace("''", "")
    return re.sub(r"\s+", " ", value).strip()


def wiki_url(root: str, title: str) -> str:
    slug = urllib.parse.quote(title.replace(" ", "_"), safe="/:'().-_")
    return root + slug


def image_filename(src: str) -> str:
    path = urllib.parse.urlsplit(src).path
    parts = path.split("/")
    # MediaWiki raster thumbnails use /images/thumb/<original>/<size>-<name>.
    # Always retain the source filename so imageinfo can resolve its SHA-1.
    if "thumb" in parts and parts.index("thumb") + 1 < len(parts):
        filename = parts[parts.index("thumb") + 1]
    else:
        filename = parts[-1]
    return urllib.parse.unquote(filename).replace("_", " ")


def page_title_from_href(href: str) -> str:
    path = urllib.parse.urlsplit(href).path
    marker = "/wiki/"
    if marker not in path:
        return ""
    return urllib.parse.unquote(path.split(marker, 1)[1]).replace("_", " ")


def request_bytes(
    url: str,
    *,
    data: dict[str, str] | None = None,
    timeout: float = 45.0,
) -> tuple[bytes, str]:
    body = urllib.parse.urlencode(data).encode("utf-8") if data else None
    for attempt in range(4):
        request = urllib.request.Request(
            url,
            data=body,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json,*/*"},
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                content_type = response.headers.get_content_type()
                return response.read(), content_type
        except urllib.error.HTTPError as exc:
            if exc.code != 429 and exc.code < 500:
                raise
            failure: Exception = exc
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            failure = exc
        if attempt < 3:
            delay = 2**attempt
            print(f"  Wiki 请求失败，{delay} 秒后重试：{failure}", file=sys.stderr, flush=True)
            time.sleep(delay)
    raise failure


def api_json(api: str, params: dict[str, str]) -> dict[str, Any]:
    payload, _ = request_bytes(api, data=params)
    result = json.loads(payload.decode("utf-8"))
    if "error" in result:
        raise RuntimeError(f"Wiki API error: {result['error']}")
    return result


def parsed_page(api: str, page: str) -> tuple[int, BeautifulSoup]:
    result = api_json(
        api,
        {
            "action": "parse",
            "format": "json",
            "formatversion": "2",
            "page": page,
            "prop": "text|revid",
        },
    )["parse"]
    return int(result["revid"]), BeautifulSoup(result["text"], "html.parser")


def gallery_rows(soup: BeautifulSoup) -> list[tuple[str, str, str]]:
    rows: list[tuple[str, str, str]] = []
    for box in soup.select("li.gallerybox"):
        image = box.select_one(".thumb img")
        image_link = box.select_one(".thumb a")
        label = box.select_one(".gallerytext big a")
        if image is None or label is None:
            continue
        rows.append(
            (
                label.get_text(" ", strip=True),
                page_title_from_href(image_link.get("href", "")) if image_link else "",
                image_filename(image.get("src", "")),
            )
        )
    return rows


def weapon_slot(icon_file: str) -> str | None:
    if " Primary Render." in icon_file:
        return "primary"
    if " Secondary Render." in icon_file:
        return "secondary"
    if " Throwable Render." in icon_file:
        return "grenade"
    return None


def weapon_catalog(
    en_soup: BeautifulSoup,
    zh_soup: BeautifulSoup,
) -> tuple[list[ListedItem], dict[str, tuple[str, str]], dict[str, tuple[str, str]]]:
    english: list[ListedItem] = []
    for name, page, icon in gallery_rows(en_soup):
        slot = weapon_slot(icon)
        if slot:
            english.append(ListedItem(name, page or name, icon, slot))

    zh_by_icon: dict[str, tuple[str, str]] = {}
    zh_by_page: dict[str, tuple[str, str]] = {}
    for name, page, icon in gallery_rows(zh_soup):
        if not has_han(name):
            continue
        zh_by_icon[icon] = (name, page)
        if page:
            zh_by_page[page] = (name, page)
    return english, zh_by_icon, zh_by_page


def table_rows(
    soup: BeautifulSoup,
    *,
    header_markers: tuple[str, ...],
    slot: str,
) -> list[ListedItem]:
    output: list[ListedItem] = []
    for table in soup.select("table.wikitable.sortable"):
        first = table.select_one("tr")
        header = first.get_text(" ", strip=True) if first else ""
        if not all(marker in header for marker in header_markers):
            continue
        for row in table.select("tr")[1:]:
            cells = row.select("td")
            image = row.select_one("td img")
            if len(cells) < 2 or image is None:
                continue
            name = cells[1].get_text(" ", strip=True)
            link = cells[1].select_one("a")
            page = page_title_from_href(link.get("href", "")) if link else name
            output.append(ListedItem(name, page or name, image_filename(image.get("src", "")), slot))
    return output


def fetch_zh_pages(titles: Iterable[str]) -> dict[str, ZhPage]:
    requested = list(dict.fromkeys(title for title in titles if title))
    output: dict[str, ZhPage] = {}
    for offset in range(0, len(requested), 40):
        batch = requested[offset : offset + 40]
        result = api_json(
            ZH_API,
            {
                "action": "query",
                "format": "json",
                "formatversion": "2",
                "prop": "revisions",
                "rvprop": "ids|content",
                "rvslots": "main",
                "redirects": "1",
                "titles": "|".join(batch),
            },
        )["query"]
        redirects = {item["from"]: item["to"] for item in result.get("redirects", [])}
        pages: dict[str, ZhPage] = {}
        for page in result.get("pages", []):
            revisions = page.get("revisions") or []
            if page.get("missing") or not revisions:
                continue
            revision = revisions[0]
            content = revision.get("slots", {}).get("main", {}).get("content", "")
            pages[page["title"]] = ZhPage(page["title"], int(revision["revid"]), content)
        for original in batch:
            target = redirects.get(original, original)
            if target in pages:
                output[original] = pages[target]
    return output


def infobox_title(page: ZhPage | None) -> str:
    if page is None:
        return ""
    match = re.search(r"(?im)^\|\s*title\s*=\s*(.*?)\s*$", page.wikitext)
    return clean_wikitext_value(match.group(1)) if match else ""


def slug(value: str) -> str:
    value = value.casefold().replace("&", " and ")
    value = re.sub(r"[^a-z0-9]+", "-", value).strip("-")
    return value


def load_existing_icons() -> tuple[dict[str, str], dict[str, dict[str, Any]]]:
    try:
        payload = json.loads(ICONS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}, {}
    icons = payload.get("icons") if isinstance(payload, dict) else {}
    files = payload.get("files") if isinstance(payload, dict) else {}
    return (
        icons if isinstance(icons, dict) else {},
        files if isinstance(files, dict) else {},
    )


def image_info(files: Iterable[str]) -> dict[str, dict[str, Any]]:
    requested = list(dict.fromkeys(files))
    output: dict[str, dict[str, Any]] = {}
    for offset in range(0, len(requested), 40):
        batch = requested[offset : offset + 40]
        query = api_json(
            ZH_API,
            {
                "action": "query",
                "format": "json",
                "formatversion": "2",
                "prop": "imageinfo",
                "iiprop": "url|mime|size|sha1",
                "iiurlwidth": "224",
                "titles": "|".join(f"File:{name}" for name in batch),
            },
        )["query"]
        for page in query.get("pages", []):
            info = (page.get("imageinfo") or [None])[0]
            if not info:
                continue
            filename = page["title"].split(":", 1)[-1]
            output[filename] = {
                "url": info.get("thumburl") or info["url"],
                "original_url": info["url"],
                "description_url": info.get("descriptionurl", ""),
                "sha1": info["sha1"],
                "mime": info["mime"],
                "width": info["width"],
                "height": info["height"],
                "embedded_width": info.get("thumbwidth", info["width"]),
                "embedded_height": info.get("thumbheight", info["height"]),
                "repository": page.get("imagerepository", "local"),
            }
    missing = sorted(set(requested) - set(output))
    if missing:
        raise RuntimeError(f"中文 Wiki API 未解析出 {len(missing)} 个图标：{missing}")
    return output


def download_icon(info: dict[str, Any]) -> tuple[str, str]:
    payload, content_type = request_bytes(info["url"], timeout=60.0)
    mime = content_type if content_type.startswith("image/") else info["mime"]
    if not mime.startswith("image/"):
        raise RuntimeError(f"图标响应不是图像：{info['url']} ({mime})")
    return f"data:{mime};base64,{base64.b64encode(payload).decode('ascii')}", hashlib.sha256(payload).hexdigest()


def build_items(
    weapon_rows: list[ListedItem],
    zh_weapon_by_icon: dict[str, tuple[str, str]],
    zh_weapon_by_page: dict[str, tuple[str, str]],
    booster_rows: list[ListedItem],
    stratagem_rows: list[ListedItem],
    zh_stratagem_rows: list[ListedItem],
    zh_pages: dict[str, ZhPage],
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []

    def append(
        listed: ListedItem,
        name_zh: str,
        translation_source: str,
        zh_page: ZhPage | None = None,
        explicit_zh_page: str = "",
    ) -> None:
        page_zh = zh_page.title if zh_page else explicit_zh_page
        items.append(
            {
                "id": f"{listed.slot}.{slug(listed.page_en or listed.name_en)}",
                "slot": listed.slot,
                "name": name_zh,
                "name_zh": name_zh,
                "name_en": listed.name_en,
                "translation_source": translation_source,
                "wiki_zh_page": wiki_url(ZH_ROOT, page_zh) if page_zh else None,
                "wiki_zh_revision": zh_page.revision if zh_page else None,
                "wiki_en_page": wiki_url(EN_ROOT, listed.page_en or listed.name_en),
                "icon_file": listed.icon_file,
            }
        )

    for listed in weapon_rows:
        localized = zh_weapon_by_icon.get(listed.icon_file)
        page = zh_pages.get(listed.page_en)
        title = infobox_title(page)
        if localized:
            append(listed, localized[0], "zh_wiki_weapon_gallery", page, localized[1])
        elif has_han(title):
            append(listed, title, "zh_wiki_page", page)
        elif listed.name_en in COMMUNITY_ZH:
            append(listed, COMMUNITY_ZH[listed.name_en], "community_translation")
        else:
            raise RuntimeError(f"缺少武器中文名：{listed.name_en}")

    for listed in booster_rows:
        try:
            name_zh = BOOSTER_ZH[listed.name_en]
        except KeyError as exc:
            raise RuntimeError(f"缺少强化资源中文名：{listed.name_en}") from exc
        source = "community_translation" if listed.name_en in COMMUNITY_ZH else "curated_zh_translation"
        append(listed, name_zh, source)

    zh_stratagem_by_icon = {row.icon_file: row for row in zh_stratagem_rows}
    for listed in stratagem_rows:
        page = zh_pages.get(listed.page_en)
        title = infobox_title(page)
        table_row = zh_stratagem_by_icon.get(listed.icon_file)
        support = zh_weapon_by_page.get(listed.page_en)
        if has_han(title):
            append(listed, title, "zh_wiki_page", page)
        elif table_row and has_han(table_row.name_en):
            append(listed, table_row.name_en, "zh_wiki_stratagem_table", page, table_row.page_en)
        elif support:
            append(listed, support[0], "zh_wiki_weapon_gallery", page, support[1])
        elif listed.name_en in COMMUNITY_ZH:
            append(listed, COMMUNITY_ZH[listed.name_en], "community_translation", page)
        elif listed.name_en in STRATAGEM_CURATED_ZH:
            append(listed, STRATAGEM_CURATED_ZH[listed.name_en], "curated_zh_translation", page)
        else:
            raise RuntimeError(f"缺少战略配备中文名：{listed.name_en}")
    return items


def validate_items(items: list[dict[str, Any]]) -> None:
    counts = {slot: sum(item["slot"] == slot for item in items) for slot in SLOTS}
    if counts != EXPECTED_COUNTS:
        raise RuntimeError(f"目录数量发生变化，请人工复核：{counts} != {EXPECTED_COUNTS}")
    names = [item["name"] for item in items]
    ids = [item["id"] for item in items]
    files = [item["icon_file"] for item in items]
    if len(names) != len(set(names)):
        raise RuntimeError("中文装备名不唯一")
    if len(ids) != len(set(ids)):
        raise RuntimeError("装备 ID 不唯一")
    if len(files) != len(set(files)):
        raise RuntimeError("装备图标文件不唯一")
    missing_zh = [item["name"] for item in items if not has_han(item["name"])]
    if missing_zh:
        raise RuntimeError(f"仍有非中文显示名：{missing_zh}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--catalog-date",
        default=dt.date.today().isoformat(),
        help="写入元数据的目录校准日期（YYYY-MM-DD）",
    )
    args = parser.parse_args()

    print("[1/6] 获取中英文 Wiki 目录……", flush=True)
    en_weapon_rev, en_weapon_soup = parsed_page(EN_API, "Weapons")
    zh_weapon_rev, zh_weapon_soup = parsed_page(ZH_API, "武器")
    en_booster_rev, en_booster_soup = parsed_page(EN_API, "Boosters")
    en_stratagem_rev, en_stratagem_soup = parsed_page(EN_API, "Stratagems")
    zh_stratagem_rev, zh_stratagem_soup = parsed_page(ZH_API, "战略配备")

    weapon_rows, zh_weapon_by_icon, zh_weapon_by_page = weapon_catalog(
        en_weapon_soup, zh_weapon_soup
    )
    booster_rows = table_rows(
        en_booster_soup, header_markers=("Booster", "Warbond", "Price"), slot="booster"
    )
    stratagem_rows = table_rows(
        en_stratagem_soup,
        header_markers=("Unlock Level", "Source"),
        slot="stratagem",
    )
    zh_stratagem_rows = table_rows(
        zh_stratagem_soup,
        header_markers=("解锁等级", "来源"),
        slot="stratagem",
    )
    print(
        "  英文目录："
        f"武器 {len(weapon_rows)}、强化资源 {len(booster_rows)}、战略配备 {len(stratagem_rows)}",
        flush=True,
    )

    print("[2/6] 读取中文条目页信息框……", flush=True)
    zh_pages = fetch_zh_pages(
        [row.page_en for row in weapon_rows] + [row.page_en for row in stratagem_rows]
    )
    items = build_items(
        weapon_rows,
        zh_weapon_by_icon,
        zh_weapon_by_page,
        booster_rows,
        stratagem_rows,
        zh_stratagem_rows,
        zh_pages,
    )
    validate_items(items)
    community = [item["name"] for item in items if item["translation_source"] == "community_translation"]
    print(f"  共 {len(items)} 项；社区补译 {len(community)} 项：{'、'.join(community)}", flush=True)

    print("[3/6] 通过中文 Wiki API 解析图标……", flush=True)
    infos = image_info(item["icon_file"] for item in items)
    old_icons, old_files = load_existing_icons()
    icons: dict[str, str] = {}
    file_meta: dict[str, dict[str, Any]] = {}

    def obtain(item: dict[str, Any]) -> tuple[str, str, str, dict[str, Any]]:
        name = item["name"]
        info = dict(infos[item["icon_file"]])
        old = old_files.get(name, {})
        if old.get("source_sha1") == info["sha1"] and name in old_icons:
            uri = old_icons[name]
            embedded_sha256 = old.get("embedded_sha256", "")
        else:
            uri, embedded_sha256 = download_icon(info)
        info["embedded_sha256"] = embedded_sha256
        info["file"] = item["icon_file"]
        info["query_url"] = wiki_url(ZH_ROOT, f"File:{item['icon_file']}")
        info["source_sha1"] = info.pop("sha1")
        return name, uri, item["icon_file"], info

    print("[4/6] 下载并内嵌 216 个最新版图标……", flush=True)
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(obtain, item) for item in items]
        for index, future in enumerate(concurrent.futures.as_completed(futures), start=1):
            name, uri, _filename, metadata = future.result()
            icons[name] = uri
            file_meta[name] = metadata
            if index % 25 == 0 or index == len(futures):
                print(f"  已处理 {index}/{len(futures)}", flush=True)

    for item in items:
        metadata = file_meta[item["name"]]
        item["icon_sha1"] = metadata["source_sha1"]
        item["icon_source"] = metadata["query_url"]

    # Keep JSON ordering deterministic and aligned with the item catalog.
    icons = {item["name"]: icons[item["name"]] for item in items}
    file_meta = {item["name"]: file_meta[item["name"]] for item in items}

    source_rows = [
        {
            "url": wiki_url(ZH_ROOT, "武器"),
            "revision": str(zh_weapon_rev),
            "license": "CC BY-SA 4.0",
            "license_url": "https://creativecommons.org/licenses/by-sa/4.0/deed.zh-hans",
            "scope": ["Chinese weapon names", "weapon icon filenames", "support-weapon names"],
        },
        {
            "url": wiki_url(ZH_ROOT, "战略配备"),
            "revision": str(zh_stratagem_rev),
            "license": "CC BY-SA 4.0",
            "license_url": "https://creativecommons.org/licenses/by-sa/4.0/deed.zh-hans",
            "scope": ["Chinese stratagem names", "stratagem icon filenames"],
        },
        {
            "url": ZH_API,
            "revision": "per-file SHA-1 recorded on every item",
            "scope": ["all embedded icon files resolved through the Chinese Wiki API"],
        },
        {
            "url": wiki_url(EN_ROOT, "Weapons"),
            "revision": str(en_weapon_rev),
            "license": "CC BY-NC-SA 4.0",
            "license_url": "https://creativecommons.org/licenses/by-nc-sa/4.0/",
            "scope": ["latest weapon catalog discovery", "canonical English names"],
        },
        {
            "url": wiki_url(EN_ROOT, "Boosters"),
            "revision": str(en_booster_rev),
            "license": "CC BY-NC-SA 4.0",
            "license_url": "https://creativecommons.org/licenses/by-nc-sa/4.0/",
            "scope": ["latest booster catalog discovery", "canonical English names"],
        },
        {
            "url": wiki_url(EN_ROOT, "Stratagems"),
            "revision": str(en_stratagem_rev),
            "license": "CC BY-NC-SA 4.0",
            "license_url": "https://creativecommons.org/licenses/by-nc-sa/4.0/",
            "scope": ["latest stratagem catalog discovery", "canonical English names"],
        },
    ]
    data_payload = {
        "schema_version": 2,
        "catalog_updated_at": args.catalog_date,
        "language": "zh-CN",
        "source": wiki_url(ZH_ROOT, "武器"),
        "source_revision": str(zh_weapon_rev),
        "sources": source_rows,
        "translation_policy": {
            "display_language": "zh-CN",
            "priority": [
                "Chinese Wiki page infobox",
                "Chinese Wiki catalog",
                "curated/community Chinese translation",
            ],
            "community_translation_count": len(community),
            "community_translations": community,
        },
        "notice": (
            "216 项配装目录已按当前 Helldivers Wiki 校准，显示名全部为中文；"
            "图标均通过中文 Wiki API 解析并内嵌。中文 Wiki 尚无可用中文标题的条目"
            "使用明确标记的社区译名。完整署名、许可与游戏素材权利边界见 "
            "THIRD_PARTY_NOTICES.md。"
        ),
        "slots": list(SLOTS),
        "items": items,
    }
    icon_payload = {
        "schema_version": 2,
        "catalog_updated_at": args.catalog_date,
        "source": ZH_API,
        "source_pages": [wiki_url(ZH_ROOT, "武器"), wiki_url(ZH_ROOT, "战略配备")],
        "note": (
            "全部图标通过 Helldivers Wiki 中文站 API 查询；跨语言共享媒体会由 API 标记为"
            "共享仓库文件。图像所含游戏素材权利仍归相应权利人，详见 THIRD_PARTY_NOTICES.md。"
        ),
        "raster_max_width": 224,
        "vector_policy": "保留 Wiki 提供的原始 SVG；PNG 使用中文 Wiki API 的 224px 缩略图",
        "files": file_meta,
        "icons": icons,
    }

    print("[5/6] 写入数据库与图标包……", flush=True)
    DATA_PATH.write_text(json.dumps(data_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    ICONS_PATH.write_text(json.dumps(icon_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    size_mb = ICONS_PATH.stat().st_size / (1024 * 1024)
    print(
        f"[6/6] 完成：{len(items)} 项 / {len(icons)} 图标，图标包 {size_mb:.2f} MiB；"
        f"目录修订 zh {zh_weapon_rev}/{zh_stratagem_rev}，"
        f"en {en_weapon_rev}/{en_booster_rev}/{en_stratagem_rev}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (urllib.error.URLError, TimeoutError, RuntimeError) as exc:
        print(f"刷新失败：{exc}", file=sys.stderr)
        raise SystemExit(1) from exc
