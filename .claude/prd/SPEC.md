# Technical Specification: ToolTranslateStringXMLAndroid

**Date**: 2026-04-02  
**Status**: Draft  
**Linked PRD**: [PRD.md](PRD.md)

---

## 1. Module Map

```
translate.py
├── load_manual_dict(file_path) → dict
├── load_languages_from_json(file_path) → list[dict]
├── load_xml(file_path) → xml.etree.ElementTree.Element
├── escape_single_quotes(text) → str
├── apply_case_correction(original, translated) → str
├── translate_text(text, dest_lang, manual_dict) → str
├── normalize_chinese_iso(iso_code) → str
├── save_translated_xml(root, isoCode, output_dir) → None
├── print_progress_start(language_name, iso_code) → None
├── print_progress_done(language_name, iso_code, duration) → None
└── process_strings(input_xml_path, languages_json_path, manual_dict_path, output_dir) → None
```

---

## 2. Function Specifications

### `load_manual_dict(file_path: str) -> dict`

- Reads `manual_dict.json` from `file_path`.
- Returns parsed JSON as a `dict` keyed by language ISO code.
- Raises `FileNotFoundError` / `json.JSONDecodeError` on bad input (no internal handling — let it propagate to caller).

**Schema contract:**
```
{
  "<iso_code>": {
    "<source_word_lowercase>": "<target_word>"
  }
}
```

---

### `load_languages_from_json(file_path: str) -> list[dict]`

- Reads `languages.json`.
- Returns a list of `{"isoCode": str, "name": str}` objects.
- Order of the list determines translation order and console output order.

---

### `load_xml(file_path: str) -> xml.etree.ElementTree.Element`

- Parses `strings.xml` with `ET.parse`.
- Returns the root element (`<resources>`).
- **Side-effect note:** `process_strings` calls this inside the language loop, so the XML is re-parsed fresh for each language — preventing state bleed between language iterations.

---

### `escape_single_quotes(text: str) -> str`

- Replaces every `'` with `\'`.
- Applied **after** translation.
- Required because Android XML string values must escape apostrophes.

**Examples:**
```
"Don't stop"  →  "Don\'t stop"
"It's fine"   →  "It\'s fine"
"No apostrophe" → "No apostrophe"  (unchanged)
```

---

### `apply_case_correction(original: str, translated: str) -> str`

- If `original.istitle()` is `True` → return `translated.capitalize()`.
- Otherwise → return `translated.lower()`.

**Behaviour table:**
| original | istitle()? | output |
|----------|-----------|--------|
| `"Save"` | True | `translated.capitalize()` |
| `"save"` | False | `translated.lower()` |
| `"SAVE"` | False | `translated.lower()` |
| `"Save Video"` | True | `translated.capitalize()` (only first char uppercased) |

**Known limitation:** `istitle()` returns `True` only when every word starts with a capital — e.g. `"Select Language"` → `True`, `"Select language"` → `False`. Multi-word title-case strings will capitalize only the first character of the full translated string, not each word.

---

### `translate_text(text: str, dest_lang: str, manual_dict: dict) -> str`

**Algorithm:**

1. Save `original_text = text`.
2. `text = text.lower()`.
3. If `dest_lang` in `manual_dict`:
   - Iterate `manual_dict[dest_lang].items()`.
   - For each `(word, translated_word)`: `text = text.replace(word, translated_word)`.
   - Replacements are applied in iteration order (JSON key order, Python 3.7+ insertion order).
4. Instantiate `googletrans.Translator()`.
5. Call `translator.translate(text, dest=dest_lang)`.
6. Apply `apply_case_correction(original_text, translated.text)`.
7. Apply `escape_single_quotes(result)`.
8. Return result.
9. On any `Exception`: log `f"Error translating '{text}' to {dest_lang}: {e}"`, return `text` (the lowercased+dict-substituted version — **not** the original).

**Performance note:** A new `Translator()` instance is created per string. For 50 strings × 26 languages = 1,300 instantiations. Recommended future fix: instantiate once per language loop iteration.

---

### `normalize_chinese_iso(iso_code: str) -> str`

- If `iso_code.startswith('zh')` → return `'zh'`.
- Otherwise → return `iso_code` unchanged.

**Effect:** All Chinese variants (`zh-CN`, `zh-TW`, `zh-HK`) resolve to a single output folder `values-zh/`.

---

### `save_translated_xml(root: Element, isoCode: str, output_dir: str) -> None`

1. Apply `normalize_chinese_iso(isoCode)`.
2. Build `folder_path = output_dir / values-{isoCode}`.
3. `os.makedirs(folder_path, exist_ok=True)`.
4. Write `strings.xml` with `encoding="utf-8"`, `xml_declaration=True`.

**Output file path:** `{output_dir}/values-{isoCode}/strings.xml`

---

### `process_strings(...) -> None`

Orchestrator. Execution flow:

```
load_manual_dict()
load_languages_from_json()
for lang in languages:
    print_progress_start()
    start_time = time.time()
    root = load_xml()               ← fresh parse each iteration
    for string_elem in root.findall('string'):
        if translatable == "false": continue
        string_elem.text = translate_text(...)
    save_translated_xml(root, ...)
    duration = (time.time() - start_time) * 1000
    print_progress_done(..., duration)
```

**Parameters:**

| Param | Type | Description |
|-------|------|-------------|
| `input_xml_path` | `str` | Path to source `strings.xml` |
| `languages_json_path` | `str` | Path to `languages.json` |
| `manual_dict_path` | `str` | Path to `manual_dict.json` |
| `output_dir` | `str` | Root output directory |

---

## 3. Data Contracts

### `languages.json`

```json
[
  {
    "isoCode": "string",   // BCP-47 code used by Android and googletrans
    "name": "string"       // Display name for logging only
  }
]
```

Current languages (26 total): `ar, cs, da, de, el, en, es, et, eu, fi, fr, hi, hr, hu, hy, id, it, ja, ka, ko, mn, ms, nl, pl, pt, zh-CN`

### `manual_dict.json`

```json
{
  "<iso_code>": {
    "<source_word>": "<target_word>"
  }
}
```

- All source words should be **lowercase** (matching is done on lowercased source text).
- Current languages with overrides: `vi, en, es, fr, de, pt, ru, zh, ar, it, ja, ko, tr, uk, zh-CN, ms, my, nb, ne, nl, or, pa, pl, ro, sr-Latn, sv, sw`
- Current override keys: `save, delete, share, next, cancel, edit, settings, exit`

### `strings.xml` (source)

```xml
<resources>
    <string name="key" [translatable="false"]>value</string>
    ...
</resources>
```

- Root element must be `<resources>`.
- Tool processes only direct `<string>` children.
- `translatable="false"` (case-insensitive check) skips the element.

---

## 4. Output File Format

```xml
<?xml version='1.0' encoding='utf-8'?>
<resources>
    <string name="txt_save">enregistrer</string>
    ...
</resources>
```

- Encoding: UTF-8 with BOM-less XML declaration.
- Element tree is the same structure as input; only `.text` values are mutated.
- Attribute `name` is preserved unchanged.
- Attribute `translatable` is preserved if present (though the element was skipped during translation).

---

## 5. Error Handling Matrix

| Error Scenario | Current Behaviour | Recommended Improvement |
|---------------|-------------------|------------------------|
| `strings.xml` not found | `ET.parse` raises `FileNotFoundError` — uncaught, run aborts | Wrap in try/except with clear error message |
| `languages.json` malformed | `json.JSONDecodeError` — uncaught, run aborts | Same |
| Google Translate network error | Caught in `translate_text`, logs error, returns (lowercased) source text | Return `original_text` instead of lowercased text |
| Google Translate rate-limit | Same as above — silently uses English text | Add exponential backoff retry (max 3 attempts) |
| Output directory not writable | `OSError` — uncaught | Wrap `os.makedirs` / `ET.write` |
| `manual_dict` key not in language | Silently skipped (dict lookup miss) | Intended behaviour — no change needed |

---

## 6. Dependency Table

| Package | Version constraint | Purpose |
|---------|--------------------|---------|
| `googletrans` | `==4.0.0rc1` recommended | Google Translate unofficial wrapper |
| `xml.etree.ElementTree` | stdlib | XML parsing/writing |
| `json` | stdlib | Config file loading |
| `os` | stdlib | Directory creation |
| `time` | stdlib | Duration measurement |

**Install:**
```bash
pip install googletrans==4.0.0rc1
```

---

## 7. Entry Point Configuration

Defined in `__main__` block:

```python
input_xml_path    = "mnt/data/strings.xml"
languages_json_path = "mnt/data/languages.json"
manual_dict_path  = "mnt/data/manual_dict.json"
output_dir        = "mnt/data/output"
```

All paths are relative to the working directory where `python translate.py` is invoked.

---

## 8. Proposed CLI Interface (Phase 2)

```
usage: translate.py [-h] [-i INPUT] [-l LANGUAGES] [-d DICT] [-o OUTPUT]

Translate Android strings.xml to multiple languages.

optional arguments:
  -h, --help            show this help message and exit
  -i, --input INPUT     Path to source strings.xml  [default: mnt/data/strings.xml]
  -l, --languages LANGUAGES
                        Path to languages.json       [default: mnt/data/languages.json]
  -d, --dict DICT       Path to manual_dict.json     [default: mnt/data/manual_dict.json]
  -o, --output OUTPUT   Output directory             [default: mnt/data/output]
```

---

## 9. Test Plan (Phase 2)

| Test | Function under test | Input | Expected output |
|------|-------------------|-------|-----------------|
| Apostrophe escaping | `escape_single_quotes` | `"Don't"` | `"Don\'t"` |
| No-op on clean string | `escape_single_quotes` | `"Hello"` | `"Hello"` |
| Title case preserved | `apply_case_correction` | `("Save", "enregistrer")` | `"Enregistrer"` |
| Lowercase forced | `apply_case_correction` | `("save", "ENREGISTRER")` | `"enregistrer"` |
| Chinese normalisation | `normalize_chinese_iso` | `"zh-CN"` | `"zh"` |
| Chinese normalisation | `normalize_chinese_iso` | `"zh-TW"` | `"zh"` |
| Non-Chinese passthrough | `normalize_chinese_iso` | `"fr"` | `"fr"` |
| Manual dict applied | `translate_text` (mocked translator) | `"save"` → `fr` | uses `"enregistrer"` from dict |
| translatable=false skipped | `process_strings` | `app_name` element | unchanged in output |
| Output file created | `save_translated_xml` | any root + `"fr"` | file exists at `output/values-fr/strings.xml` |
