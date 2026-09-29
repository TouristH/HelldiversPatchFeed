# Third-party notices

This file records third-party sources used by HelldiversPatchFeed. It is an
attribution record, not a new license grant.

## Helldivers 2 Random Loadout Generator CN

- Project: [Xenfo-LC/Helldivers-2-Random-Loadout-Generator-CN](https://github.com/Xenfo-LC/Helldivers-2-Random-Loadout-Generator-CN)
- Referenced revision: `141ba007b52940903b22067571d87ec0263902d6`
- Upstream project credit: original project by Erlend Dahl; Android port and
  version maintenance by Xenfo; Chinese localization credited upstream to
  GPT 5.6 Sol and Xenfo.
- Use in this repository:
  - the initial random-loadout slot structure and early catalog organization
    were developed with reference to the upstream data set;
  - the random-loadout slot organization and reroll experience in `plugin.py`
    were designed with reference to that project.

Starting with version `0.2.0`, no equipment record or embedded icon in
`loadout_data.json` or `loadout_icons.json` is sourced from the upstream
`static/images` tree. The catalog and icon bundle were rebuilt from the Wiki
sources described below. The acknowledgement remains because the initial
feature design was assisted by the upstream project.

The referenced repository did not contain a standalone open-source license at
the time of review on 2026-09-29. Accordingly, the material identified above
is not represented as being licensed under this repository's MIT License.
Copyright and other rights remain with the upstream contributors and the
applicable rights holders. Redistributors must independently obtain any
permission they require.

## HELLDIVERS 2 material

HELLDIVERS, HELLDIVERS 2, related names, artwork, icons, and other game media
are the property of their respective rights holders, including Arrowhead Game
Studios and Sony Interactive Entertainment. This repository is an unofficial
fan project and is not affiliated with, endorsed by, or sponsored by them.

## Helldivers Wiki

The following sources are credited to the **Helldivers Wiki contributors** and
were accessed on 2026-09-30:

- Chinese [武器](https://helldivers.wiki.gg/zh/wiki/武器), revision `6540`:
  primary weapons, secondary weapons, throwables, Chinese names, and the
  verified Chinese name used for the newly listed GL-28 support weapon;
- Chinese [战略配备](https://helldivers.wiki.gg/zh/wiki/战略配备), revision `6647`:
  the Chinese stratagem catalog and Chinese names;
- English [Weapons](https://helldivers.wiki.gg/wiki/Weapons), revision `137028`:
  latest-catalog discovery and canonical English weapon names;
- English [Boosters](https://helldivers.wiki.gg/wiki/Boosters), revision `136929`:
  latest-catalog discovery and canonical English booster names;
- English [Stratagems](https://helldivers.wiki.gg/wiki/Stratagems), revision
  `133897`: latest-catalog discovery and canonical English stratagem names.

Individual Chinese item pages were also queried for translated infobox titles.
Their exact page URLs and revision IDs are recorded per item in
`loadout_data.json`. All 216 image filenames were resolved through the Chinese
Wiki API. The API reports that these images are served from the Wiki's shared
English media repository; `loadout_icons.json` records the returned description
URL, repository identifier, source SHA-1, dimensions, and embedded SHA-256 for
every file.

The two Chinese pages are available under the
[Creative Commons Attribution-ShareAlike 4.0 International License](https://creativecommons.org/licenses/by-sa/4.0/deed.zh-hans)
(CC BY-SA 4.0). The three English pages are available under the
[Creative Commons Attribution-NonCommercial-ShareAlike 4.0 International License](https://creativecommons.org/licenses/by-nc-sa/4.0/)
(CC BY-NC-SA 4.0).

For `loadout_data.json`, this project extracted and combined the Chinese and
English catalogs, normalized model prefixes and names, assigned plugin slots,
and converted the result to JSON. The final display language is Chinese. Ten
items for which the Chinese Wiki did not provide a usable Chinese title on the
access date use transparent community translations and carry
`translation_source: community_translation`; the database identifies those
items explicitly. Other curated labels are likewise distinguished from direct
Wiki titles. The Chinese- and English-Wiki-derived portions of the database,
including adaptations, are redistributed under CC BY-SA 4.0 and CC BY-NC-SA
4.0 respectively. The English-derived portions are therefore subject to the
CC BY-NC-SA 4.0 non-commercial restriction.

`loadout_icons.json` contains all 216 current icons downloaded through the
Chinese Wiki API: raster artwork uses the API's 224-pixel thumbnail and vector
artwork retains the Wiki-provided SVG. Querying or downloading a file through
the Wiki does not itself grant additional rights in the underlying game art.
The HELLDIVERS 2 material section above therefore remains controlling for game
artwork, alongside any file-specific notice on the linked Wiki description
page. These images are not represented as covered by this repository's MIT
License.

Separately, the plugin retrieves Wiki excerpts at runtime. Messages containing
that material include the page link and an attribution matching the source:
CC BY-SA 4.0 for the Chinese Wiki or CC BY-NC-SA 4.0 for the English Wiki.

## Python dependencies

Python dependencies are installed from their own distributions and are not
vendored here. Each dependency remains governed by the license published by
its maintainer.

## Repository license boundary

The root MIT License applies to this project's original code and documentation.
It does not replace the CC BY-SA 4.0 or CC BY-NC-SA 4.0 terms for Wiki-derived
catalog material or the separate rights described above for referenced code,
data, game media, and embedded icons.
