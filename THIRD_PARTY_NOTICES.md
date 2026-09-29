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
  - `loadout_data.json` derives equipment names and slot classifications from
    the upstream data set;
  - `loadout_icons.json` contains resized, re-encoded copies of images obtained
    from the upstream `static/images` tree;
  - the random-loadout slot organization and reroll experience in `plugin.py`
    were designed with reference to that project.

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

The plugin retrieves excerpts from [helldivers.wiki.gg](https://helldivers.wiki.gg/)
at runtime. Those excerpts are not bundled in this repository. Messages that
contain Wiki material include the source link and a CC BY-SA attribution.

## Python dependencies

Python dependencies are installed from their own distributions and are not
vendored here. Each dependency remains governed by the license published by
its maintainer.
