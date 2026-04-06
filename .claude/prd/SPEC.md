# Technical Specification: ToolTranslateStringXMLAndroid

**Date**: 2026-04-02  
**Status**: Draft  
**Linked PRD**: [PRD.md](PRD.md)

---

## 1. Module Map

```
translate.py
├── TranslationCache(db_path)
├── load_json(file_path) → dict | list
├── escape_android_chars(text) → str
├── protect_translatables(text) → tuple[str, dict]
├── restore_translatables(text, ph_map) → str
├── preprocess_cdata(xml_text) → str
├── postprocess_cdata(dest_file, cdata_names) → None
├── translate_string(text, dest_lang) → str
├── translate_language(...) → None
└── main(source, lang_path, output_dir, threads) → None
```

---

## 2. Function Specifications

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

### `TranslationCache(db_path: str)`

- SQLite-backed cache for translated text.
- Table schema:

```sql
CREATE TABLE translations (
  iso_code TEXT NOT NULL,
  source_text TEXT NOT NULL,
  translated_text TEXT NOT NULL,
  PRIMARY KEY (iso_code, source_text)
)
```

- Supports:
  - `get(iso_code, source_text)` for cache lookup
  - `set(iso_code, source_text, translated_text)` for upsert

---

### `translate_string(text: str, dest_lang: str) -> str`

**Algorithm:**

1. Return early if the input is blank.
2. Replace HTML tags and Android format specifiers with placeholders.
3. Call the translation backend with protected text.
4. Restore placeholders into translated text.
5. On any `Exception`, return protected source text so output generation can continue.

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
load_languages_from_json()
for lang in languages:
    print_progress_start()
    start_time = time.time()
    root = load_xml()               ← fresh parse each iteration
    for string_elem in root.findall('string'):
        if translatable == "false": continue
        cached = TranslationCache.get(...)
        if not cached:
            cached = translate_string(...)
            TranslationCache.set(...)
        string_elem.text = cached
    save_translated_xml(root, ...)
    duration = (time.time() - start_time) * 1000
    print_progress_done(..., duration)
```

**Parameters:**

| Param | Type | Description |
|-------|------|-------------|
| `input_xml_path` | `str` | Path to source `strings.xml` |
| `languages_json_path` | `str` | Path to `languages.json` |
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

### `translation_cache.db`

- SQLite database in project root.
- New translations are persisted incrementally during processing.

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
| Google Translate network error | Caught in `translate_string`, returns source text | Add explicit warning count / retry metrics |
| Google Translate rate-limit | Same as above — silently uses English text | Add exponential backoff retry (max 3 attempts) |
| Output directory not writable | `OSError` — uncaught | Wrap `os.makedirs` / `ET.write` |
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

CLI arguments:

```bash
python translate.py SOURCE [-l LANGUAGES] [-o OUTPUT] [-t THREADS]
```

---

## 8. Proposed CLI Interface (Phase 2)

```
usage: translate.py [-h] [-l LANGUAGES] [-o OUTPUT] [-t THREADS] source

Translate Android strings.xml to multiple languages.

optional arguments:
  -h, --help            show this help message and exit
  source                Path to source strings.xml or folder containing it
  -l, --languages LANGUAGES
                        Path to languages.json       [default: all_languages.json]
  -o, --output OUTPUT   Output directory             [default: parent folder of source]
  -t, --threads THREADS Number of worker threads     [default: 10]
```

---

## 9. Test Plan (Phase 2)

| Test | Function under test | Input | Expected output |
|------|-------------------|-------|-----------------|
| Apostrophe escaping | `escape_single_quotes` | `"Don't"` | `"Don\'t"` |
| No-op on clean string | `escape_single_quotes` | `"Hello"` | `"Hello"` |
| Chinese normalisation | `normalize_chinese_iso` | `"zh-CN"` | `"zh"` |
| Chinese normalisation | `normalize_chinese_iso` | `"zh-TW"` | `"zh"` |
| Non-Chinese passthrough | `normalize_chinese_iso` | `"fr"` | `"fr"` |
| translatable=false skipped | `process_strings` | `app_name` element | unchanged in output |
| Output file created | `save_translated_xml` | any root + `"fr"` | file exists at `output/values-fr/strings.xml` |
