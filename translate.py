import os
import xml.etree.ElementTree as ET
import json
import time
import sys
import threading
import re
import html as html_lib
import argparse
from concurrent.futures import ThreadPoolExecutor
from deep_translator import GoogleTranslator

# --- CONFIG ---
MAX_THREADS = 10
TRANSLATE_DELAY = 0.5  # minimum seconds between any two API calls (global, across all threads)

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

# --- TRANSLATION MEMORY LOCK ---
_memory_lock = threading.Lock()


def throttled_translate(text, dest, retries=3):
    """Call Google Translate with a global minimum delay between requests.
    Retries up to `retries` times with exponential backoff on failure."""
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
        except Exception as e:
            last_exc = e
            if attempt < retries - 1:
                time.sleep(2 ** attempt)  # 1s, 2s before retry 2, 3
    raise last_exc


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


def save_json(data, file_path):
    with open(file_path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=4)


def escape_android_chars(text):
    """Escape ' and \" as required by Android string resources."""
    if not text:
        return ""
    return text.replace("'", "\\'").replace('"', '\\"')


# ---------------------------------------------------------------------------
# Format specifier + HTML tag protection
# ---------------------------------------------------------------------------

def protect_translatables(text):
    """
    Replace HTML tags and Android format specifiers with [[N]] placeholders
    so Google Translate won't mangle them.
    Returns (protected_text, {placeholder: original}).
    """
    counter = [0]
    ph_map = {}

    def make_ph(m):
        ph = f'[[{counter[0]}]]'
        ph_map[ph] = m.group(0)
        counter[0] += 1
        return ph

    # Order matters: protect HTML tags first, then format specs
    protected = _HTML_TAG_RE.sub(make_ph, text)
    protected = _FORMAT_SPEC_RE.sub(make_ph, protected)
    return protected, ph_map


def restore_translatables(text, ph_map):
    for ph, original in ph_map.items():
        text = text.replace(ph, original)
    return text


# ---------------------------------------------------------------------------
# Manual dictionary
# ---------------------------------------------------------------------------

def apply_manual_dict(text, iso_code, manual_dict):
    """Apply manual dictionary overrides before calling Google Translate.
    Uses word-boundary matching (case-insensitive) to avoid false positives."""
    if iso_code not in manual_dict:
        return text
    result = text
    for word, translated_word in manual_dict[iso_code].items():
        pattern = re.compile(r'\b' + str(re.escape(word)) + r'\b', re.IGNORECASE)
        result = pattern.sub(translated_word, result)
    return result


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

def translate_string(raw_text, iso_code, manual_dict):
    """
    Translate a string, preserving HTML tags and format specifiers.
    Returns raw translated text (NOT yet Android-escaped).
    """
    if not raw_text.strip():
        return raw_text
    protected, ph_map = protect_translatables(raw_text)
    text_for_api = apply_manual_dict(protected, iso_code, manual_dict)
    try:
        translated = throttled_translate(text_for_api, iso_code) or protected
    except Exception:
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
                       translation_memory, manual_dict, cdata_names, lang_results):
    android_iso = 'zh' if iso_code.startswith('zh') else iso_code
    dest_folder = os.path.join(res_dir, f"values-{android_iso}")
    dest_file = os.path.join(dest_folder, "strings.xml")

    with _memory_lock:
        if iso_code not in translation_memory:
            translation_memory[iso_code] = {}
    mem = translation_memory[iso_code]

    try:
        # --- BƯỚC 1: ĐỌC FILE GỐC (CDATA đã được tiền xử lý) ---
        with open(input_xml_path, 'r', encoding='utf-8') as f:
            raw_source = f.read()
        clean_source = preprocess_cdata(raw_source)
        base_root = ET.fromstring(clean_source)

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
            target_root = ET.fromstring(dest_clean)

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
        new_root = ET.fromstring(clean_source)  # fresh copy from preprocessed source

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
                elif not is_html and raw_content in mem:
                    # Plain text cache hit — apply Android escaping unless CDATA
                    cached = mem[raw_content]
                    set_inner_xml(elem, cached if is_cdata else escape_android_chars(cached))
                    update_count += 1
                else:
                    try:
                        translated = translate_string(raw_content, iso_code, manual_dict)
                        if not is_html:
                            mem[raw_content] = translated  # cache raw (unescaped)
                        # HTML and CDATA elements must NOT have Android char escaping
                        # (escaping would corrupt tag attributes or CDATA content)
                        if is_html or is_cdata:
                            set_inner_xml(elem, translated)
                        else:
                            set_inner_xml(elem, escape_android_chars(translated))
                        new_count += 1
                    except Exception as e:
                        with progress_lock:
                            sys.stdout.write(f"\n⚠ [{iso_code}] '{raw_content[:30]}': {e}\033[K\n")
                            sys.stdout.flush()

            elif etype == 'arr':
                arr_items = elem.findall('item')
                old_items = existing_translated.get(f"arr_{name}", [])
                base_arr = base_keys.get(f"arr_{name}", [])

                for i, item in enumerate(arr_items):
                    raw_item = get_inner_xml(item).strip()
                    if i < len(old_items) and old_items[i] and old_items[i] != (base_arr[i] if i < len(base_arr) else ""):
                        set_inner_xml(item, old_items[i])
                        old_keep_count += 1
                    elif raw_item in mem:
                        set_inner_xml(item, escape_android_chars(mem[raw_item]))
                        update_count += 1
                    else:
                        try:
                            translated = translate_string(raw_item, iso_code, manual_dict)
                            mem[raw_item] = translated
                            set_inner_xml(item, escape_android_chars(translated))
                            new_count += 1
                        except Exception as e:
                            with progress_lock:
                                sys.stdout.write(f"\n⚠ [{iso_code}] '{raw_item[:30]}': {e}\033[K\n")
                                sys.stdout.flush()

            elif etype == 'plu':
                plu_items = elem.findall('item')
                old_items = existing_translated.get(f"plu_{name}", [])
                base_plu = base_keys.get(f"plu_{name}", [])

                for i, item in enumerate(plu_items):
                    raw_item = get_inner_xml(item).strip()
                    if i < len(old_items) and old_items[i] and old_items[i] != (base_plu[i] if i < len(base_plu) else ""):
                        set_inner_xml(item, old_items[i])
                        old_keep_count += 1
                    elif raw_item in mem:
                        set_inner_xml(item, escape_android_chars(mem[raw_item]))
                        update_count += 1
                    else:
                        try:
                            translated = translate_string(raw_item, iso_code, manual_dict)
                            mem[raw_item] = translated
                            set_inner_xml(item, escape_android_chars(translated))
                            new_count += 1
                        except Exception as e:
                            with progress_lock:
                                sys.stdout.write(f"\n⚠ [{iso_code}] '{raw_item[:30]}': {e}\033[K\n")
                                sys.stdout.flush()

        # --- BƯỚC 4: GHI FILE VÀO THƯ MỤC NGÔN NGỮ ĐÍCH ---
        os.makedirs(dest_folder, exist_ok=True)
        with open(dest_file, 'wb') as f:
            f.write(b'<?xml version="1.0" encoding="utf-8"?>\n')
            ET.ElementTree(new_root).write(f, encoding="utf-8", xml_declaration=False)

        # Re-wrap CDATA elements that were stripped by ET during parsing
        postprocess_cdata(dest_file, cdata_names)

        lang_results[iso_code] = ('pass', language_name)
        with progress_lock:
            thread_status[thread_idx] = ""
            sys.stdout.write(
                f"\r✅ {language_name:12} | Mới: {new_count:2} | Up: {update_count:2} | Cũ: {old_keep_count:2} | Xoá: {deleted_count:2}\033[K\n")
            sys.stdout.flush()

    except Exception as e:
        lang_results[iso_code] = ('fail', language_name, str(e))
        with progress_lock:
            thread_status[thread_idx] = ""
            sys.stdout.write(f"\r❌ {language_name:12} | Lỗi: {str(e)[:60]}\033[K\n")


def main(input_arg, lang_path=None, manual_dict_path=None, output_dir=None, threads=None):
    input_xml = os.path.join(input_arg, "strings.xml") if os.path.isdir(input_arg) else input_arg
    base_dir = os.path.dirname(os.path.abspath(__file__))

    if lang_path is None:
        lang_path = os.path.join(base_dir, "all_languages.json")
    if manual_dict_path is None:
        manual_dict_path = os.path.join(base_dir, "mnt/data/manual_dict.json")
    if output_dir is None:
        output_dir = os.path.dirname(os.path.dirname(input_xml))
    if threads is None:
        threads = MAX_THREADS

    memory_path = os.path.join(base_dir, "translation_memory.json")

    if not os.path.isfile(input_xml):
        print(f"❌ Không tìm thấy file nguồn: {input_xml}")
        sys.exit(1)

    languages = load_json(lang_path)
    if not languages:
        print(f"❌ Danh sách ngôn ngữ trống hoặc không đọc được: {lang_path}")
        sys.exit(1)

    memory = load_json(memory_path)
    manual_dict = load_json(manual_dict_path)

    # Detect CDATA element names once from the source file
    try:
        with open(input_xml, 'r', encoding='utf-8') as f:
            source_xml_text = f.read()
    except OSError as e:
        print(f"❌ Không đọc được file nguồn: {e}")
        sys.exit(1)
    cdata_names = extract_cdata_names(source_xml_text)

    # Count translatable strings from source
    source_tree = ET.fromstring(preprocess_cdata(source_xml_text))
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

    print(f"🚀 SYNC MODE (PROTECTED ORIGIN)")
    print("=" * 80)
    print(f"📊 Ngôn ngữ: {num_languages} | Luồng đồng thời: {threads} | Strings/ngôn ngữ: {translatable_count}")
    print(f"⏱  Thời gian dự kiến (worst-case): {format_duration(estimated_seconds)}")
    print("=" * 80)

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
                memory,
                manual_dict,
                cdata_names,
                lang_results,
            )

    save_json(memory, memory_path)

    actual_seconds = time.time() - start_time
    diff_seconds = actual_seconds - estimated_seconds
    diff_str = (f"+{format_duration(diff_seconds)} (chậm hơn)" if diff_seconds > 0
                else f"-{format_duration(abs(diff_seconds))} (nhanh hơn)")

    passed = [v[1] for v in lang_results.values() if v[0] == 'pass']
    failed = [(v[1], v[2]) for v in lang_results.values() if v[0] == 'fail']

    print("\n" + "=" * 80)
    print(f"✨ HOÀN THÀNH!")
    print(f"   ✅ Pass : {len(passed)}/{num_languages} ngôn ngữ")
    if failed:
        print(f"   ❌ Fail : {len(failed)} ngôn ngữ")
        for name, err in failed:
            print(f"      • {name}: {err[:80]}")
    print(f"   ⏱  Thực tế: {format_duration(actual_seconds)} | Dự kiến: {format_duration(estimated_seconds)} | Lệch: {diff_str}")
    print("=" * 80)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Dịch Android strings.xml sang nhiều ngôn ngữ qua Google Translate."
    )
    parser.add_argument(
        "source",
        help="Đường dẫn tới strings.xml hoặc thư mục chứa nó",
    )
    parser.add_argument(
        "--languages", "-l",
        metavar="PATH",
        help="Đường dẫn tới languages.json (mặc định: all_languages.json kế bên script)",
    )
    parser.add_argument(
        "--manual-dict", "-m",
        metavar="PATH",
        dest="manual_dict",
        help="Đường dẫn tới manual_dict.json (mặc định: mnt/data/manual_dict.json)",
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
    args = parser.parse_args()
    main(args.source, lang_path=args.languages, manual_dict_path=args.manual_dict,
         output_dir=args.output, threads=args.threads)
