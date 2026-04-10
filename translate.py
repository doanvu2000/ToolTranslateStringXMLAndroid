import argparse
import html as html_lib
import json
import logging
import os
import re
import sqlite3
import sys
import threading
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor

try:
    from deep_translator import GoogleTranslator
except ModuleNotFoundError:
    GoogleTranslator = None

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

# --- ERROR TYPES ---
class TranslationAPIError(Exception):
    """Error from Google Translate API (network, rate limit, etc.)."""

class XMLProcessingError(Exception):
    """Error parsing or writing XML."""

class FileIOError(Exception):
    """Error reading/writing files on disk."""

# --- CONFIG ---
MAX_THREADS = 8
TRANSLATE_DELAY = 0.5  # minimum seconds between any two API calls (global, across all threads)

# --- LOGGING ---
logger = logging.getLogger("translate")


def setup_logging(log_file=None):
    """Configure logger. Always logs to console; optionally also to a file."""
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.INFO)
    console.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(console)

    if log_file:
        os.makedirs(os.path.dirname(os.path.abspath(log_file)), exist_ok=True)
        fh = logging.FileHandler(log_file, encoding="utf-8", mode="w")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s",
                                          datefmt="%Y-%m-%d %H:%M:%S"))
        logger.addHandler(fh)

# --- PATTERNS ---
# Android format specifiers: %s, %d, %1$s, %2$d, %.2f, %%  etc.
_FORMAT_SPEC_RE = re.compile(r'%(\d+\$)?[-+#0]*(\.\d+)?[sdfegxXon%]')
# HTML tags (opening, closing, self-closing, SGML comments)
_HTML_TAG_RE = re.compile(r'<[/?!\w][^>]*>')
# CDATA sections inside <string> elements
_CDATA_RE = re.compile(
    r'(<string\b[^>]*>)\s*<!\[CDATA\[(.*?)\]\]>\s*(</string>)',
    re.DOTALL
)
# Match any <string> element in the written output (for CDATA re-wrapping)
_STRING_ELEM_RE = re.compile(
    r'(<string\b[^>]*name="([^"]+)"[^>]*>)(.*?)(</string>)',
    re.DOTALL
)

progress_lock = threading.Lock()
thread_status = {i: "" for i in range(MAX_THREADS)}

# --- GLOBAL RATE LIMITER ---
_rate_lock = threading.Lock()
_last_call_time = 0.0

class TranslationCache:
    def __init__(self, db_path):
        self.db_path = db_path
        self.lock = threading.Lock()
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS translations (
                iso_code TEXT NOT NULL,
                source_text TEXT NOT NULL,
                translated_text TEXT NOT NULL,
                PRIMARY KEY (iso_code, source_text)
            )
        """)
        self.conn.commit()

    def get(self, iso_code, source_text):
        with self.lock:
            row = self.conn.execute(
                "SELECT translated_text FROM translations WHERE iso_code = ? AND source_text = ?",
                (iso_code, source_text),
            ).fetchone()
        return row[0] if row else None

    def set(self, iso_code, source_text, translated_text):
        with self.lock:
            self.conn.execute(
                """
                INSERT INTO translations (iso_code, source_text, translated_text)
                VALUES (?, ?, ?)
                ON CONFLICT(iso_code, source_text)
                DO UPDATE SET translated_text = excluded.translated_text
                """,
                (iso_code, source_text, translated_text),
            )
            self.conn.commit()

    def stats(self):
        """Return cache statistics: total entries, per-language counts."""
        with self.lock:
            total = self.conn.execute("SELECT COUNT(*) FROM translations").fetchone()[0]
            langs = self.conn.execute(
                "SELECT iso_code, COUNT(*) FROM translations GROUP BY iso_code ORDER BY iso_code"
            ).fetchall()
        return total, langs

    def clear(self):
        """Delete all cached translations."""
        with self.lock:
            self.conn.execute("DELETE FROM translations")
            self.conn.commit()

    def clear_language(self, iso_code):
        """Delete cached translations for a specific language."""
        with self.lock:
            deleted = self.conn.execute(
                "DELETE FROM translations WHERE iso_code = ?", (iso_code,)
            ).rowcount
            self.conn.commit()
        return deleted

    def close(self):
        with self.lock:
            self.conn.close()


def throttled_translate(text, dest, retries=3):
    """Call Google Translate with a global minimum delay between requests.
    Retries up to `retries` times with exponential backoff on failure.
    Raises TranslationAPIError on persistent failure."""
    if GoogleTranslator is None:
        raise ModuleNotFoundError(
            "No module named 'deep_translator'. Install it with: pip install deep-translator"
        )
    global _last_call_time
    last_exc = None
    for attempt in range(retries):
        with _rate_lock:
            now = time.time()
            wait = TRANSLATE_DELAY - (now - _last_call_time)
            if wait > 0:
                time.sleep(wait)
            _last_call_time = time.time()
        try:
            return GoogleTranslator(source='en', target=dest).translate(text)
        except (ConnectionError, TimeoutError, OSError) as e:
            last_exc = e
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
        except Exception as e:
            # Catch-all for unexpected API errors (bad response, auth, etc.)
            last_exc = e
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
    raise TranslationAPIError(f"API failed after {retries} retries: {last_exc}") from last_exc


def load_json(file_path):
    if not os.path.exists(file_path): return {}
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except (json.JSONDecodeError, ValueError) as e:
        print(f"⚠️  Cảnh báo: file JSON lỗi '{file_path}': {e}")
        return {}


def escape_android_chars(text):
    """Escape ' and \" as required by Android string resources."""
    if not text:
        return ""
    return text.replace("'", "\\'").replace('"', '\\"')


# ---------------------------------------------------------------------------
# Format specifier + HTML tag protection
# ---------------------------------------------------------------------------

def protect_translatables(text, overrides=None):
    """
    Replace HTML tags, Android format specifiers, and manual override tokens
    with [[N]] placeholders so Google Translate won't mangle them.
    Returns (protected_text, {placeholder: original}).

    *overrides* is an optional dict {token: replacement} — matched tokens are
    protected as placeholders and restored to the *replacement* value afterwards.
    Matching is case-sensitive to avoid false positives (e.g. "AM" won't match "am").
    """
    counter = [0]
    ph_map = {}

    def make_ph(m):
        ph = f'[[{counter[0]}]]'
        ph_map[ph] = m.group(0)
        counter[0] += 1
        return ph

    # 1. Protect manual override tokens first (case-sensitive, whole-word)
    protected = text
    if overrides:
        for token, replacement in overrides.items():
            if token in protected:
                ph = f'[[{counter[0]}]]'
                ph_map[ph] = replacement
                counter[0] += 1
                protected = protected.replace(token, ph)

    # 2. Protect HTML tags, then format specs
    protected = _HTML_TAG_RE.sub(make_ph, protected)
    protected = _FORMAT_SPEC_RE.sub(make_ph, protected)
    return protected, ph_map


def restore_translatables(text, ph_map):
    for ph, original in ph_map.items():
        text = text.replace(ph, original)
    return text


# ---------------------------------------------------------------------------
# Comment-preserving XML parser
# ---------------------------------------------------------------------------

class CommentedTreeBuilder(ET.TreeBuilder):
    """TreeBuilder subclass that preserves XML comments."""
    def comment(self, data):
        self.start(ET.Comment, {})
        self.data(data)
        self.end(ET.Comment)


def parse_xml_with_comments(xml_text):
    """Parse XML string preserving comments. Returns root Element."""
    parser = ET.XMLParser(target=CommentedTreeBuilder())
    parser.feed(xml_text)
    return parser.close()


# ---------------------------------------------------------------------------
# HTML-aware inner-XML helpers
# ---------------------------------------------------------------------------

def get_inner_xml(elem):
    """Return the full inner XML of an element (text + child tags as string)."""
    parts = [elem.text or '']
    for child in elem:
        parts.append(ET.tostring(child, encoding='unicode'))
    return ''.join(parts)


def set_inner_xml(elem, inner_xml_str):
    """Rebuild an element's content from a raw inner-XML string."""
    elem.text = None
    for child in list(elem):
        elem.remove(child)
    try:
        wrapped = ET.fromstring(f'<_>{inner_xml_str}</_>')
        elem.text = wrapped.text
        for child in wrapped:
            elem.append(child)
    except ET.ParseError:
        # Fallback: store as plain text (tags will be XML-escaped by ET on write)
        elem.text = inner_xml_str


# ---------------------------------------------------------------------------
# CDATA pre/post processing
# ---------------------------------------------------------------------------

def extract_cdata_names(xml_text):
    """Return the set of <string> element names whose value is CDATA in source."""
    names = set()
    for m in _CDATA_RE.finditer(xml_text):
        name_m = re.search(r'name="([^"]+)"', m.group(1))
        if name_m:
            names.add(name_m.group(1))
    return names


def preprocess_cdata(xml_text):
    """
    Strip CDATA wrappers and XML-escape their content so ET can parse the file
    without losing the text value.
    """
    def replace(m):
        content = html_lib.escape(m.group(2))
        return f'{m.group(1)}{content}{m.group(3)}'
    return _CDATA_RE.sub(replace, xml_text)


def postprocess_cdata(dest_file, cdata_names):
    """
    After ET writes the output file, re-wrap the content of CDATA elements
    inside <![CDATA[...]]> and unescape XML entities inside them.
    """
    if not cdata_names:
        return
    with open(dest_file, 'r', encoding='utf-8') as f:
        content = f.read()

    def rewrap(m):
        name = m.group(2)
        if name not in cdata_names:
            return m.group(0)
        # Unescape XML entities (e.g. &amp; → &) before wrapping in CDATA
        raw_inner = html_lib.unescape(m.group(3))
        return f'{m.group(1)}<![CDATA[{raw_inner}]]>{m.group(4)}'

    new_content = _STRING_ELEM_RE.sub(rewrap, content)
    with open(dest_file, 'w', encoding='utf-8') as f:
        f.write(new_content)


# ---------------------------------------------------------------------------
# Core translate helper
# ---------------------------------------------------------------------------

def translate_string(raw_text, iso_code, overrides=None):
    """
    Translate a string, preserving HTML tags, format specifiers, and override tokens.
    Returns raw translated text (NOT yet Android-escaped).
    """
    if not raw_text.strip():
        return raw_text
    protected, ph_map = protect_translatables(raw_text, overrides=overrides)
    try:
        translated = throttled_translate(protected, iso_code) or protected
    except TranslationAPIError:
        translated = protected  # fallback: giữ nguyên bản gốc, không trả về text đã lowercase
    if ph_map:
        translated = restore_translatables(translated, ph_map)
    return translated


# ---------------------------------------------------------------------------
# Console helpers
# ---------------------------------------------------------------------------

def format_duration(seconds):
    """Format seconds into human-readable string."""
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    m, s = divmod(seconds, 60)
    if m < 60:
        return f"{m}m{s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h{m:02d}m{s:02d}s"


def refresh_console():
    with progress_lock:
        output = [thread_status[i] for i in range(MAX_THREADS) if thread_status[i]]
        if output:
            sys.stdout.write("\r" + " | ".join(output) + "\033[K")
            sys.stdout.flush()


# ---------------------------------------------------------------------------
# Per-language translation worker
# ---------------------------------------------------------------------------

def translate_language(thread_idx, iso_code, language_name, input_xml_path, res_dir,
                       translation_cache, cdata_names, lang_results, overrides=None,
                       dry_run=False):
    # Map ISO codes to Android resource folder names (e.g. zh-CN → zh-rCN, pt-BR → pt-rBR)
    if '-' in iso_code:
        lang, region = iso_code.split('-', 1)
        android_folder = f"values-{lang}-r{region.upper()}"
    else:
        android_folder = f"values-{iso_code}"
    dest_folder = os.path.join(res_dir, android_folder)
    dest_file = os.path.join(dest_folder, "strings.xml")

    try:
        # --- BƯỚC 1: ĐỌC FILE GỐC (CDATA đã được tiền xử lý) ---
        with open(input_xml_path, 'r', encoding='utf-8') as f:
            raw_source = f.read()
        clean_source = preprocess_cdata(raw_source)
        base_root = parse_xml_with_comments(clean_source)

        # base_keys stores inner XML (captures both plain text and HTML children)
        base_keys = {}
        for s in base_root.findall('string'):
            name = s.get('name')
            if name:
                base_keys[name] = get_inner_xml(s)
        for arr in base_root.findall('string-array'):
            name = arr.get('name')
            if name:
                base_keys[f"arr_{name}"] = [get_inner_xml(item) for item in arr.findall('item')]
        for plu in base_root.findall('plurals'):
            name = plu.get('name')
            if name:
                base_keys[f"plu_{name}"] = [get_inner_xml(item) for item in plu.findall('item')]

        # --- BƯỚC 2: ĐỌC FILE ĐÍCH ĐỂ SO SÁNH (NẾU CÓ) ---
        existing_translated = {}
        deleted_count = 0
        if os.path.exists(dest_file):
            with open(dest_file, 'r', encoding='utf-8') as f:
                dest_raw = f.read()
            dest_clean = preprocess_cdata(dest_raw)
            target_root = parse_xml_with_comments(dest_clean)

            for s in target_root.findall('string'):
                name = s.get('name')
                if name in base_keys:
                    inner = get_inner_xml(s)
                    if inner and inner != base_keys[name]:
                        existing_translated[name] = inner
                else:
                    deleted_count += 1

            for arr in target_root.findall('string-array'):
                name = arr.get('name')
                if f"arr_{name}" in base_keys:
                    items_inner = [get_inner_xml(item) for item in arr.findall('item')]
                    existing_translated[f"arr_{name}"] = items_inner
                else:
                    deleted_count += 1

            for plu in target_root.findall('plurals'):
                name = plu.get('name')
                if f"plu_{name}" in base_keys:
                    items_inner = [get_inner_xml(item) for item in plu.findall('item')]
                    existing_translated[f"plu_{name}"] = items_inner
                else:
                    deleted_count += 1

        # --- BƯỚC 3: TẠO FILE DỊCH MỚI (DỰA TRÊN CLONE CỦA GỐC) ---
        new_root = parse_xml_with_comments(clean_source)  # fresh copy preserving comments

        all_elements = []
        for s in new_root.findall('string'):
            if s.get('translatable') != "false":
                all_elements.append(('str', s))
        for arr in new_root.findall('string-array'):
            if arr.get('translatable') != "false":
                all_elements.append(('arr', arr))
        for plu in new_root.findall('plurals'):
            if plu.get('translatable') != "false":
                all_elements.append(('plu', plu))

        total_task = len(all_elements)
        new_count = update_count = old_keep_count = 0

        for idx, (etype, elem) in enumerate(all_elements, start=1):
            percent = int((idx / total_task) * 100)
            thread_status[thread_idx] = f"⏳ {language_name[:3]}: {percent}%"
            refresh_console()

            name = elem.get('name')

            if etype == 'str':
                is_html = len(elem) > 0          # has child elements (HTML tags)
                is_cdata = name in cdata_names   # source used CDATA
                raw_content = get_inner_xml(elem).strip()

                if name in existing_translated:
                    set_inner_xml(elem, existing_translated[name])
                    old_keep_count += 1
                elif not is_html and (cached := translation_cache.get(iso_code, raw_content)) is not None:
                    # Plain text cache hit — apply Android escaping unless CDATA
                    set_inner_xml(elem, cached if is_cdata else escape_android_chars(cached))
                    update_count += 1
                else:
                    try:
                        translated = translate_string(raw_content, iso_code, overrides=overrides)
                        if not is_html:
                            translation_cache.set(iso_code, raw_content, translated)
                        # HTML and CDATA elements must NOT have Android char escaping
                        # (escaping would corrupt tag attributes or CDATA content)
                        if is_html or is_cdata:
                            set_inner_xml(elem, translated)
                        else:
                            set_inner_xml(elem, escape_android_chars(translated))
                        new_count += 1
                    except TranslationAPIError as e:
                        logger.warning(f"[{iso_code}] '{raw_content[:30]}': {e}")

            elif etype == 'arr':
                arr_items = elem.findall('item')
                old_items = existing_translated.get(f"arr_{name}", [])
                base_arr = base_keys.get(f"arr_{name}", [])

                for i, item in enumerate(arr_items):
                    raw_item = get_inner_xml(item).strip()
                    if i < len(old_items) and old_items[i] and old_items[i] != (base_arr[i] if i < len(base_arr) else ""):
                        set_inner_xml(item, old_items[i])
                        old_keep_count += 1
                    elif (cached := translation_cache.get(iso_code, raw_item)) is not None:
                        set_inner_xml(item, escape_android_chars(cached))
                        update_count += 1
                    else:
                        try:
                            translated = translate_string(raw_item, iso_code, overrides=overrides)
                            translation_cache.set(iso_code, raw_item, translated)
                            set_inner_xml(item, escape_android_chars(translated))
                            new_count += 1
                        except TranslationAPIError as e:
                            logger.warning(f"[{iso_code}] '{raw_item[:30]}': {e}")

            elif etype == 'plu':
                plu_items = elem.findall('item')
                old_items = existing_translated.get(f"plu_{name}", [])
                base_plu = base_keys.get(f"plu_{name}", [])

                for i, item in enumerate(plu_items):
                    raw_item = get_inner_xml(item).strip()
                    if i < len(old_items) and old_items[i] and old_items[i] != (base_plu[i] if i < len(base_plu) else ""):
                        set_inner_xml(item, old_items[i])
                        old_keep_count += 1
                    elif (cached := translation_cache.get(iso_code, raw_item)) is not None:
                        set_inner_xml(item, escape_android_chars(cached))
                        update_count += 1
                    else:
                        try:
                            translated = translate_string(raw_item, iso_code, overrides=overrides)
                            translation_cache.set(iso_code, raw_item, translated)
                            set_inner_xml(item, escape_android_chars(translated))
                            new_count += 1
                        except TranslationAPIError as e:
                            logger.warning(f"[{iso_code}] '{raw_item[:30]}': {e}")

        # --- BƯỚC 4: GHI FILE VÀO THƯ MỤC NGÔN NGỮ ĐÍCH ---
        if not dry_run:
            os.makedirs(dest_folder, exist_ok=True)

            # Back up existing file so we can restore on validation failure
            backup_content = None
            if os.path.exists(dest_file):
                with open(dest_file, 'r', encoding='utf-8') as f:
                    backup_content = f.read()

            with open(dest_file, 'wb') as f:
                f.write(b'<?xml version="1.0" encoding="utf-8"?>\n')
                ET.ElementTree(new_root).write(f, encoding="utf-8", xml_declaration=False)

            # Re-wrap CDATA elements that were stripped by ET during parsing
            postprocess_cdata(dest_file, cdata_names)

            # --- BƯỚC 5: VALIDATE OUTPUT XML ---
            try:
                with open(dest_file, 'r', encoding='utf-8') as f:
                    ET.fromstring(preprocess_cdata(f.read()))
            except ET.ParseError as ve:
                # Restore backup if available, otherwise remove broken file
                if backup_content is not None:
                    with open(dest_file, 'w', encoding='utf-8') as f:
                        f.write(backup_content)
                else:
                    os.remove(dest_file)
                raise RuntimeError(f"Output XML validation failed: {ve}") from ve

        lang_results[iso_code] = ('pass', language_name, new_count, update_count, old_keep_count, deleted_count)
        prefix = "🔍" if dry_run else "✅"
        msg = f"{prefix} {language_name:12} | Mới: {new_count:2} | Up: {update_count:2} | Cũ: {old_keep_count:2} | Xoá: {deleted_count:2}"
        logger.debug(f"[{iso_code}] PASS new={new_count} cache={update_count} kept={old_keep_count} deleted={deleted_count}")
        with progress_lock:
            thread_status[thread_idx] = ""
            sys.stdout.write(f"\r{msg}\033[K\n")
            sys.stdout.flush()

    except (OSError, IOError) as e:
        lang_results[iso_code] = ('fail', language_name, 'FileIOError', str(e))
        logger.error(f"[{iso_code}] FileIOError: {e}")
        with progress_lock:
            thread_status[thread_idx] = ""
            sys.stdout.write(f"\r❌ {language_name:12} | File I/O: {str(e)[:60]}\033[K\n")
    except ET.ParseError as e:
        lang_results[iso_code] = ('fail', language_name, 'XMLProcessingError', str(e))
        logger.error(f"[{iso_code}] XMLProcessingError: {e}")
        with progress_lock:
            thread_status[thread_idx] = ""
            sys.stdout.write(f"\r❌ {language_name:12} | XML lỗi: {str(e)[:60]}\033[K\n")
    except RuntimeError as e:
        lang_results[iso_code] = ('fail', language_name, 'XMLProcessingError', str(e))
        logger.error(f"[{iso_code}] XMLValidation: {e}")
        with progress_lock:
            thread_status[thread_idx] = ""
            sys.stdout.write(f"\r❌ {language_name:12} | Validation: {str(e)[:60]}\033[K\n")
    except TranslationAPIError as e:
        lang_results[iso_code] = ('fail', language_name, 'TranslationAPIError', str(e))
        logger.error(f"[{iso_code}] TranslationAPIError: {e}")
        with progress_lock:
            thread_status[thread_idx] = ""
            sys.stdout.write(f"\r❌ {language_name:12} | API lỗi: {str(e)[:60]}\033[K\n")
    except Exception as e:
        lang_results[iso_code] = ('fail', language_name, 'UnexpectedError', str(e))
        logger.error(f"[{iso_code}] UnexpectedError: {e}")
        with progress_lock:
            thread_status[thread_idx] = ""
            sys.stdout.write(f"\r❌ {language_name:12} | Lỗi: {str(e)[:60]}\033[K\n")


def main(input_arg, lang_path=None, output_dir=None, threads=None, overrides_path=None,
         log_file=None, dry_run=False, only=None, report_path=None):
    setup_logging(log_file)

    input_xml = os.path.join(input_arg, "strings.xml") if os.path.isdir(input_arg) else input_arg
    base_dir = os.path.dirname(os.path.abspath(__file__))

    if lang_path is None:
        lang_path = os.path.join(base_dir, "all_languages.json")
    if output_dir is None:
        output_dir = os.path.dirname(os.path.dirname(input_xml))
    if threads is None:
        threads = MAX_THREADS

    cache_db_path = os.path.join(base_dir, "translation_cache.db")

    if not os.path.isfile(input_xml):
        logger.error(f"Không tìm thấy file nguồn: {input_xml}")
        sys.exit(1)

    languages = load_json(lang_path)
    if not languages:
        logger.error(f"Danh sách ngôn ngữ trống hoặc không đọc được: {lang_path}")
        sys.exit(1)

    # Filter languages if --only specified
    if only:
        only_set = {code.strip().lower() for code in only}
        filtered = [l for l in languages if l["isoCode"].lower() in only_set]
        skipped = len(languages) - len(filtered)
        if not filtered:
            logger.error(f"Không tìm thấy ngôn ngữ nào khớp --only: {', '.join(only)}")
            sys.exit(1)
        if skipped:
            logger.info(f"🔍 --only filter: {len(filtered)} ngôn ngữ (bỏ qua {skipped})")
        languages = filtered

    translation_cache = TranslationCache(cache_db_path)

    # Load manual overrides (tokens that bypass translation, e.g. "AM" → "AM")
    overrides = None
    if overrides_path is None:
        default_overrides = os.path.join(base_dir, "overrides.json")
        if os.path.isfile(default_overrides):
            overrides_path = default_overrides
    if overrides_path:
        overrides = load_json(overrides_path)
        if overrides:
            logger.info(f"📋 Overrides: {len(overrides)} token(s) from {os.path.basename(overrides_path)}")

    # Detect CDATA element names once from the source file
    try:
        with open(input_xml, 'r', encoding='utf-8') as f:
            source_xml_text = f.read()
    except OSError as e:
        logger.error(f"Không đọc được file nguồn: {e}")
        sys.exit(1)
    cdata_names = extract_cdata_names(source_xml_text)

    # Count translatable strings from source
    source_tree = parse_xml_with_comments(preprocess_cdata(source_xml_text))
    translatable_count = sum(
        1 for s in source_tree.findall('string') if s.get('translatable') != 'false'
    ) + sum(
        len(arr.findall('item'))
        for arr in source_tree.findall('string-array') if arr.get('translatable') != 'false'
    ) + sum(
        len(plu.findall('item'))
        for plu in source_tree.findall('plurals') if plu.get('translatable') != 'false'
    )

    num_languages = len(languages)
    # Worst-case estimate: all strings need API calls, rate limited globally
    estimated_seconds = translatable_count * num_languages * TRANSLATE_DELAY

    mode_label = "🔍 DRY-RUN MODE" if dry_run else "🚀 SYNC MODE (PROTECTED ORIGIN)"
    logger.info(mode_label)
    logger.info("=" * 80)
    logger.info(f"📊 Ngôn ngữ: {num_languages} | Luồng đồng thời: {threads} | Strings/ngôn ngữ: {translatable_count}")
    logger.info(f"⏱  Thời gian dự kiến (worst-case): {format_duration(estimated_seconds)}")
    logger.info("=" * 80)

    start_time = time.time()
    lang_results = {}

    with ThreadPoolExecutor(max_workers=threads) as executor:
        for i, lang in enumerate(languages):
            executor.submit(
                translate_language,
                i % threads,
                lang["isoCode"],
                lang["name"],
                input_xml,
                output_dir,
                translation_cache,
                cdata_names,
                lang_results,
                overrides,
                dry_run,
            )
    translation_cache.close()

    actual_seconds = time.time() - start_time
    diff_seconds = actual_seconds - estimated_seconds
    diff_str = (f"+{format_duration(diff_seconds)} (chậm hơn)" if diff_seconds > 0
                else f"-{format_duration(abs(diff_seconds))} (nhanh hơn)")

    passed_results = {k: v for k, v in lang_results.items() if v[0] == 'pass'}
    failed_results = {k: v for k, v in lang_results.items() if v[0] == 'fail'}

    # Count errors by type
    error_counts = {}
    for v in failed_results.values():
        error_type = v[2]
        error_counts[error_type] = error_counts.get(error_type, 0) + 1

    logger.info("\n" + "=" * 80)
    logger.info(f"✨ HOÀN THÀNH!")
    logger.info(f"   ✅ Pass : {len(passed_results)}/{num_languages} ngôn ngữ")
    if failed_results:
        logger.info(f"   ❌ Fail : {len(failed_results)} ngôn ngữ")
        for error_type, count in sorted(error_counts.items()):
            logger.info(f"      [{error_type}] × {count}")
        for v in failed_results.values():
            logger.info(f"      • {v[1]} ({v[2]}): {v[3][:70]}")
    logger.info(f"   ⏱  Thực tế: {format_duration(actual_seconds)} | Dự kiến: {format_duration(estimated_seconds)} | Lệch: {diff_str}")
    logger.info("=" * 80)

    # Generate JSON report if requested
    if report_path:
        report = {
            "duration_seconds": round(actual_seconds, 2),
            "languages_total": num_languages,
            "languages_passed": len(passed_results),
            "languages_failed": len(failed_results),
            "results": {},
        }
        for iso, v in passed_results.items():
            report["results"][iso] = {
                "status": "pass", "language": v[1],
                "new": v[2], "cache": v[3], "kept": v[4], "deleted": v[5],
            }
        for iso, v in failed_results.items():
            report["results"][iso] = {
                "status": "fail", "language": v[1],
                "error_type": v[2], "error": v[3],
            }
        with open(report_path, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        logger.info(f"📊 Report saved to: {report_path}")

    if log_file:
        logger.info(f"📝 Log saved to: {log_file}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Dịch Android strings.xml sang nhiều ngôn ngữ qua Google Translate."
    )
    parser.add_argument(
        "source",
        nargs="?",
        default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "mnt/data/strings.xml"),
        help="Đường dẫn tới strings.xml hoặc thư mục chứa nó (mặc định: mnt/data/strings.xml)",
    )
    parser.add_argument(
        "--languages", "-l",
        metavar="PATH",
        help="Đường dẫn tới languages.json (mặc định: all_languages.json kế bên script)",
    )
    parser.add_argument(
        "--output", "-o",
        metavar="DIR",
        help="Thư mục output chứa các values-xx/ (mặc định: thư mục cha của source)",
    )
    parser.add_argument(
        "--threads", "-t",
        metavar="N",
        type=int,
        default=MAX_THREADS,
        help=f"Số luồng dịch song song (mặc định: {MAX_THREADS})",
    )
    parser.add_argument(
        "--overrides",
        metavar="PATH",
        help="Đường dẫn tới overrides.json — các token giữ nguyên không dịch (mặc định: overrides.json kế bên script)",
    )
    parser.add_argument(
        "--log-file",
        metavar="PATH",
        help="Ghi log chi tiết ra file (timestamps + DEBUG level)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Chạy thử — dịch và đếm nhưng không ghi file output",
    )
    parser.add_argument(
        "--only",
        nargs="+",
        metavar="LANG",
        help="Chỉ dịch các ngôn ngữ chỉ định (vd: --only vi fr zh-CN)",
    )
    parser.add_argument(
        "--report",
        metavar="PATH",
        help="Ghi báo cáo kết quả dạng JSON (per-language pass/fail, counts)",
    )
    parser.add_argument(
        "--cache",
        choices=["stats", "clear"],
        help="Quản lý cache: stats (xem thống kê), clear (xoá toàn bộ)",
    )
    parser.add_argument(
        "--cache-clear-lang",
        metavar="LANG",
        help="Xoá cache cho ngôn ngữ chỉ định (vd: --cache-clear-lang vi)",
    )
    parser.add_argument(
        "--config",
        metavar="PATH",
        help="Đường dẫn tới config JSON (mặc định: translate.config.json kế bên script)",
    )
    args = parser.parse_args()

    # Load config file: defaults that CLI args can override
    base_dir = os.path.dirname(os.path.abspath(__file__))
    config_path = args.config or os.path.join(base_dir, "translate.config.json")
    config = load_json(config_path) if os.path.isfile(config_path) else {}
    if config and not args.config:
        print(f"⚙  Config loaded: {os.path.basename(config_path)}")

    # Apply config defaults (CLI args take precedence)
    if args.languages is None and "languages" in config:
        args.languages = config["languages"]
    if args.output is None and "output" in config:
        args.output = config["output"]
    if args.threads == MAX_THREADS and "threads" in config:
        args.threads = config["threads"]
    if args.overrides is None and "overrides" in config:
        args.overrides = config["overrides"]
    if args.log_file is None and "log_file" in config:
        args.log_file = config["log_file"]
    if not args.dry_run and config.get("dry_run", False):
        args.dry_run = True
    if args.only is None and "only" in config:
        args.only = config["only"]
    if args.report is None and "report" in config:
        args.report = config["report"]

    # Handle cache commands
    if args.cache or args.cache_clear_lang:
        base_dir = os.path.dirname(os.path.abspath(__file__))
        cache_db_path = os.path.join(base_dir, "translation_cache.db")
        cache = TranslationCache(cache_db_path)
        if args.cache == "stats":
            total, langs = cache.stats()
            print(f"📊 Cache: {total} entries total")
            for iso, count in langs:
                print(f"   {iso}: {count}")
        elif args.cache == "clear":
            total, _ = cache.stats()
            cache.clear()
            print(f"🗑  Đã xoá {total} entries từ cache")
        if args.cache_clear_lang:
            deleted = cache.clear_language(args.cache_clear_lang)
            print(f"🗑  Đã xoá {deleted} entries cho [{args.cache_clear_lang}]")
        cache.close()
        sys.exit(0)

    main(args.source, lang_path=args.languages, output_dir=args.output,
         threads=args.threads, overrides_path=args.overrides, log_file=args.log_file,
         dry_run=args.dry_run, only=args.only, report_path=args.report)
