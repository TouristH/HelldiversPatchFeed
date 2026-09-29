# Changelog

All notable changes to this project will be documented in this file.

## 0.2.0 - 2026-09-30

- Rebuilt the random-loadout database as a 216-item, Chinese-only catalog with
  stable IDs, canonical English names, source pages, translation provenance,
  and per-file Wiki SHA-1 values.
- Replaced the legacy partial artwork bundle with current icons for every item,
  resolved through the Chinese Helldivers Wiki API and embedded for offline
  card rendering.
- Added a reproducible maintainer refresh tool and strict catalog, language,
  icon-coverage, integrity, and attribution tests.
- Updated third-party notices to identify the Chinese Wiki as the new icon
  source and to distinguish Wiki titles from the ten community translations.

## 0.1.1 - 2026-09-29

- Refreshed the random-loadout catalog against the current Helldivers Wiki data.
- Corrected outdated or mistranslated Chinese equipment names and added newly documented weapons, boosters, and stratagems; untranslated additions retain their canonical English names.
- Kept equipment without reusable artwork name-only in rendered cards.
- Expanded CC BY-SA 4.0 and CC BY-NC-SA 4.0 attribution and disclosed OpenAI Codex AI assistance.

## 0.1.0 - 2026-09-29

- Initial public release for MaiBot Plugin SDK 2.x.
- Added autonomous official Steam update polling and per-group delivery state.
- Added on-demand Steam and Helldivers Wiki queries with Chinese-first output.
- Added subscription, permission, rate-limit, diagnostics, and status commands.
- Added image-based random loadouts with single-slot rerolls and text fallback.
- Added WebUI configuration metadata, tests, licensing, and third-party notices.
