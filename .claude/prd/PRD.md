# Product Requirements Document: ToolTranslateStringXMLAndroid

**Author**: Doan Vũ  
**Date**: 2026-04-02  
**Status**: Draft  
**Stakeholders**: Android developer(s) shipping multi-language apps

---

## 1. Executive Summary

`ToolTranslateStringXMLAndroid` is a Python CLI tool that automates the localization of Android `strings.xml` files. Given a source English XML file, a list of target languages, and an optional manual-override dictionary, it produces correctly structured `values-{isoCode}/strings.xml` files ready to drop into an Android project — eliminating hours of manual copy-paste and reducing human translation errors.

---

## 2. Background & Context

Android apps declare UI strings in `res/values/strings.xml`. Supporting multiple languages requires creating a separate `res/values-{isoCode}/strings.xml` per locale. Doing this by hand is:

- **Slow** — each new language is a full copy-edit cycle.
- **Error-prone** — translators may miss Android-specific escape rules (e.g., unescaped apostrophes cause build failures).
- **Hard to maintain** — adding a new string to the source requires updating every language file.

The target app (`Precision Challenge`) already has ~50 strings and targets 26 languages. Without automation, every content change ripples into 26 manual edits.

The tool was built to solve this with a one-command workflow: point it at the source XML, run it, and get all translated files ready to copy into the Android `res/` folder.

---

## 3. Objectives & Success Metrics

### Goals

1. Produce valid, Android-compatible `strings.xml` for all configured languages in a single command run.
2. Allow per-language term overrides (via `manual_dict.json`) to ensure brand-consistent or contextually correct translations.
3. Respect Android XML constraints: skip `translatable="false"` strings, escape single quotes.
4. Keep setup friction near zero — no API keys, no accounts, no build system integration required.

### Non-Goals

1. **Not a translation memory / TM system** — no segment history, no fuzzy matching across runs.
2. **Not a real-time sync** — does not watch for file changes or integrate into a Gradle build.
3. **Not a professional translation service** — quality depends on Google Translate; not suitable for legal/medical copy without human review.
4. **Not a GUI tool** — CLI only; no web UI or IDE plugin.
5. **Not plurals-aware** — does not handle Android `<plurals>` or `<string-array>` elements in this version.

### Success Metrics

| Metric | Current | Target |
|--------|---------|--------|
| Time to translate all languages | ~hours (manual) | < 5 minutes (automated) |
| Build-breaking apostrophe errors | Occasional | 0 |
| Languages supported per run | 1 (manual) | All languages in `languages.json` |
| Developer effort to add a new language | High | Edit `languages.json`, re-run |

---

## 4. Target Users & Segments

**Primary user: Solo / small-team Android developer**

- Builds consumer apps (games, filters, utilities) targeting a global audience.
- Does not have a dedicated localization engineer or budget for a paid translation platform.
- Comfortable running Python scripts from the command line.
- Needs "good enough" translations fast — human review happens later for high-traffic strings.

**Current app context:**  
The source XML (`strings.xml`) belongs to `Precision Challenge`, a video/filter camera app with ~50 UI strings. Target languages include Arabic, Chinese, French, German, Japanese, Korean, Spanish, and 19 others.

---

## 5. User Stories & Requirements

### P0 — Must Have

| # | User Story | Acceptance Criteria |
|---|-----------|---------------------|
| 1 | As a developer, I can run a single command and get translated XML files for all languages in `languages.json`. | `python translate.py` exits 0 and creates `output/values-{isoCode}/strings.xml` for every language entry. |
| 2 | As a developer, strings marked `translatable="false"` are preserved verbatim in all outputs. | `app_name` (and any other `translatable="false"` entry) appears unchanged in every output file. |
| 3 | As a developer, single quotes in translated output are escaped as `\'` so Android builds don't fail. | Any translated string containing `'` has it replaced with `\'` in the output XML. |
| 4 | As a developer, I can define manual overrides per language in `manual_dict.json` so common terms (Save, Delete, etc.) are always correct. | Words listed in `manual_dict[lang]` are substituted before Google Translate is called; final output uses the override value. |
| 5 | As a developer, Chinese variants (`zh-CN`, `zh-TW`, etc.) are normalized to a single output folder `values-zh`. | All ISO codes starting with `zh` write output to `values-zh/`. |

### P1 — Should Have

| # | User Story | Acceptance Criteria |
|---|-----------|---------------------|
| 6 | As a developer, I see per-language progress in the console so I know the tool is running and how long each language takes. | Console prints `START` and `done in values-{isoCode} (Xms)` for each language. |
| 7 | As a developer, if Google Translate fails for a string, the original English text is used as a fallback (no crash). | On `Exception` in `translate_text`, the function returns the original text and logs the error; the run continues. |
| 8 | As a developer, I can configure all paths (input XML, languages JSON, manual dict, output dir) without editing the script. | Paths are defined as variables at the top of `__main__` (currently) — in a future version, via CLI args or a config file. |

### P2 — Nice to Have / Future

| # | User Story | Acceptance Criteria |
|---|-----------|---------------------|
| 9 | As a developer, I can pass paths as CLI arguments (`--input`, `--output`, `--languages`, `--dict`) instead of editing the script. | `argparse`-based CLI; all four paths are optional with sensible defaults. |
| 10 | As a developer, only strings that have changed since the last run are re-translated (incremental mode) to save API calls and time. | Tool compares existing output against source; skips strings where the source text is unchanged. |
| 11 | As a developer, `<plurals>` and `<string-array>` elements are also translated. | Tool handles `<plurals>` items and `<string-array>` items in addition to `<string>`. |
| 12 | As a developer, I can run the tool via a Gradle task so translation is part of the build pipeline. | A Gradle `exec` task wraps `python translate.py`; documented in README. |
| 13 | As a developer, I can use an alternative translation backend (DeepL, Azure Translator) in place of Google Translate. | Backend is abstracted behind a `translate_text` interface; swappable via config. |

---

## 6. Solution Overview

### Architecture

```
mnt/data/
├── strings.xml          ← source (English, Android XML)
├── languages.json       ← [{"isoCode": "fr", "name": "French"}, ...]
├── manual_dict.json     ← {"fr": {"save": "enregistrer"}, ...}
└── output/
    ├── values-ar/strings.xml
    ├── values-de/strings.xml
    └── ...              ← one folder per language
```

### Translation Pipeline (per language)

```
load_xml(strings.xml)
    │
    ▼
for each <string> (skip translatable="false")
    │
    ├─ lowercase source text
    ├─ apply manual_dict substitutions  (deterministic overrides)
    ├─ call googletrans.Translator      (ML translation)
    ├─ apply_case_correction()          (restore capitalisation)
    └─ escape_single_quotes()           (Android XML safety)
    │
    ▼
save_translated_xml(values-{isoCode}/strings.xml)
```

### Key Design Decisions

| Decision | Rationale |
|----------|-----------|
| `googletrans` (unofficial Google Translate wrapper) | Zero cost, no API key required. Trade-off: unofficial API may break on Google-side changes. |
| Manual dict applied as string substitution before translation | Gives developer deterministic control over high-visibility terms without needing to post-process ML output. |
| One `Translator()` instance per string call | Current implementation; a future optimisation is to batch strings or reuse a single instance per language. |
| `translatable="false"` check | Standard Android convention — must be honoured to avoid overwriting app identifiers. |
| Chinese ISO normalisation (`zh*` → `zh`) | `googletrans` accepts `zh` but Android uses `zh-CN`/`zh-TW`; normalisation ensures output folder matches Android expectations while keeping the translate call valid. |

### Known Limitations / Tech Debt

- `Translator()` is instantiated on every string call — slow and risks rate-limiting.
- No retry logic for transient Google Translate failures.
- `apply_case_correction` uses `istitle()` which is English-centric and may misfire for non-Latin scripts.
- Output XML is written with `encoding="utf-8"` and an XML declaration, which is correct, but the root tag is `<resources>` — the tool must preserve this as Android requires it (currently delegated to `ET.ElementTree.write`; verified correct).
- No test coverage.

---

## 7. Open Questions

| Question | Owner | Notes |
|----------|-------|-------|
| Should `zh-CN` output go to `values-zh-rCN/` (Android proper locale format) instead of `values-zh/`? | Developer | Android uses `values-zh-rCN` for region-specific Chinese; current `values-zh` only covers generic Chinese. |
| Is `googletrans` acceptable long-term or should we migrate to an official API (Google Cloud Translation, DeepL)? | Developer | `googletrans` uses the web endpoint without a key — could break or be rate-limited at any time. |
| Should the manual dict support regex patterns, not just exact substring matches? | Developer | Current substring replace can produce false positives (e.g. "save" matching inside "unsaved"). |
| Should we add a `--dry-run` flag that prints translations without writing files? | Developer | Useful for spot-checking quality before committing output to the Android project. |
| Who reviews translation quality before it ships? | Developer | Currently no review step; translations go directly to production. |

---

## 8. Timeline & Phasing

### Phase 1 — Current (Shipped)
- Core translation pipeline (P0 stories 1–5)
- Manual dictionary overrides
- Progress logging
- Single-quote escaping
- Chinese ISO normalisation

### Phase 2 — Near-term Improvements
- CLI argument support (`argparse`) — removes need to edit the script
- Translator instance reuse / batching — performance improvement
- Basic retry logic for Google Translate failures
- Unit tests for `escape_single_quotes`, `apply_case_correction`, `normalize_chinese_iso`

### Phase 3 — Future / Optional
- Incremental translation (skip unchanged strings)
- `<plurals>` and `<string-array>` support
- Pluggable translation backend
- Gradle integration

---

## 9. File & Directory Spec

### Input: `strings.xml`

Standard Android resource file. Tool processes only `<string>` elements at the top level of `<resources>`. Elements with `translatable="false"` attribute are copied as-is.

### Input: `languages.json`

```json
[
  { "isoCode": "fr", "name": "French" },
  { "isoCode": "zh-CN", "name": "Chinese" }
]
```

Each entry must have `isoCode` (BCP-47 / Android locale code) and `name` (human-readable, used in log output only).

### Input: `manual_dict.json`

```json
{
  "fr": {
    "save": "enregistrer",
    "settings": "paramètres"
  }
}
```

Keys are target language ISO codes. Values are `{source_word: target_word}` maps. Matching is **case-insensitive substring** — the source text is lowercased before matching.

### Output: `values-{isoCode}/strings.xml`

UTF-8 encoded Android XML with `<?xml version='1.0' encoding='utf-8'?>` declaration. Root element is `<resources>`. Each `<string name="...">` contains the translated value.

**Note on Chinese:** regardless of whether `languages.json` lists `zh-CN`, `zh-TW`, or any other `zh*` code, all Chinese output writes to `values-zh/`.
