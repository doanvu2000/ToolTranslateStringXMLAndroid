import os
import xml.etree.ElementTree as ET
import json
import time
import sys
import threading
import copy
from concurrent.futures import ThreadPoolExecutor
from deep_translator import GoogleTranslator

# --- CONFIG ---
MAX_THREADS = 6
TRANSLATE_DELAY = 0.5  # minimum seconds between any two API calls (global, across all threads)

progress_lock = threading.Lock()
thread_status = {i: "" for i in range(MAX_THREADS)}

# --- GLOBAL RATE LIMITER ---
_rate_lock = threading.Lock()
_last_call_time = 0.0


def throttled_translate(text, dest):
    """Call Google Translate with a global minimum delay between requests."""
    global _last_call_time
    with _rate_lock:
        now = time.time()
        wait = TRANSLATE_DELAY - (now - _last_call_time)
        if wait > 0:
            time.sleep(wait)
        _last_call_time = time.time()
    return GoogleTranslator(source='en', target=dest).translate(text)


def load_json(file_path):
    if not os.path.exists(file_path): return {}
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except:
        return {}


def save_json(data, file_path):
    with open(file_path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=4)


def escape_special_chars(text):
    if not text: return ""
    return text.replace("'", "\\'").replace('"', '\\"')


def apply_manual_dict(text, iso_code, manual_dict):
    """Apply manual dictionary overrides before calling Google Translate."""
    if iso_code not in manual_dict:
        return text
    lowered = text.lower()
    for word, translated_word in manual_dict[iso_code].items():
        lowered = lowered.replace(word, translated_word)
    return lowered


def refresh_console():
    with progress_lock:
        output = [thread_status[i] for i in range(MAX_THREADS) if thread_status[i]]
        if output:
            sys.stdout.write("\r" + " | ".join(output) + "\033[K")
            sys.stdout.flush()


def translate_language(thread_idx, iso_code, language_name, input_xml_path, res_dir, translation_memory, manual_dict):
    android_iso = 'zh' if iso_code.startswith('zh') else iso_code
    dest_folder = os.path.join(res_dir, f"values-{android_iso}")
    dest_file = os.path.join(dest_folder, "strings.xml")

    if iso_code not in translation_memory:
        translation_memory[iso_code] = {}
    mem = translation_memory[iso_code]

    try:
        # --- BƯỚC 1: ĐỌC FILE GỐC (CHỈ ĐỌC) ---
        base_tree = ET.parse(input_xml_path)
        base_root = base_tree.getroot()

        base_keys = {}  # key -> text_goc
        for s in base_root.findall('string'):
            name = s.get('name')
            if name: base_keys[name] = s.text
        for arr in base_root.findall('string-array'):
            name = arr.get('name')
            if name: base_keys[f"arr_{name}"] = [item.text for item in arr.findall('item')]

        # --- BƯỚC 2: ĐỌC FILE ĐÍCH ĐỂ SO SÁNH (NẾU CÓ) ---
        existing_translated = {}
        deleted_count = 0
        if os.path.exists(dest_file):
            target_tree = ET.parse(dest_file)
            target_root = target_tree.getroot()
            for s in target_root.findall('string'):
                name = s.get('name')
                if name in base_keys:
                    if s.text and s.text != base_keys[name]:
                        existing_translated[name] = s.text
                else:
                    deleted_count += 1

            for arr in target_root.findall('string-array'):
                name = arr.get('name')
                if f"arr_{name}" in base_keys:
                    items = [item.text for item in arr.findall('item')]
                    existing_translated[f"arr_{name}"] = items
                else:
                    deleted_count += 1

        # --- BƯỚC 3: TẠO FILE DỊCH MỚI (DỰA TRÊN CLONE CỦA GỐC) ---
        new_root = copy.deepcopy(base_root)

        all_elements = []
        for s in new_root.findall('string'):
            if s.get('translatable') != "false": all_elements.append(('str', s))
        for arr in new_root.findall('string-array'):
            if arr.get('translatable') != "false": all_elements.append(('arr', arr))

        total_task = len(all_elements)
        new_count = 0
        update_count = 0
        old_keep_count = 0

        for idx, (etype, elem) in enumerate(all_elements, start=1):
            percent = int((idx / total_task) * 100)
            thread_status[thread_idx] = f"⏳ {language_name[:3]}: {percent}%"
            refresh_console()

            name = elem.get('name')
            if etype == 'str':
                raw_text = elem.text.strip() if elem.text else ""
                if name in existing_translated:
                    elem.text = existing_translated[name]
                    old_keep_count += 1
                elif raw_text in mem:
                    elem.text = escape_special_chars(mem[raw_text])
                    update_count += 1
                else:
                    try:
                        text_to_translate = apply_manual_dict(raw_text, iso_code, manual_dict)
                        res = throttled_translate(text_to_translate, iso_code)
                        mem[raw_text] = res
                        elem.text = escape_special_chars(res)
                        new_count += 1
                    except Exception as e:
                        with progress_lock:
                            sys.stdout.write(f"\n⚠ [{iso_code}] '{raw_text[:30]}': {e}\033[K\n")
                            sys.stdout.flush()

            elif etype == 'arr':
                arr_items = elem.findall('item')
                old_items = existing_translated.get(f"arr_{name}", [])
                base_arr_texts = base_keys.get(f"arr_{name}", [])

                for i, item in enumerate(arr_items):
                    raw_item = item.text.strip() if item.text else ""
                    if i < len(old_items) and old_items[i] and old_items[i] != base_arr_texts[i]:
                        item.text = old_items[i]
                        old_keep_count += 1
                    elif raw_item in mem:
                        item.text = escape_special_chars(mem[raw_item])
                        update_count += 1
                    else:
                        try:
                            text_to_translate = apply_manual_dict(raw_item, iso_code, manual_dict)
                            res = throttled_translate(text_to_translate, iso_code)
                            mem[raw_item] = res
                            item.text = escape_special_chars(res)
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

        with progress_lock:
            thread_status[thread_idx] = ""
            sys.stdout.write(
                f"\r✅ {language_name:12} | Mới: {new_count:2} | Up: {update_count:2} | Cũ: {old_keep_count:2} | Xoá: {deleted_count:2}\033[K\n")
            sys.stdout.flush()

    except Exception as e:
        with progress_lock:
            thread_status[thread_idx] = ""
            sys.stdout.write(f"\r❌ {language_name:12} | Lỗi: {str(e)[:20]}\033[K\n")


def main(input_arg):
    input_xml = os.path.join(input_arg, "strings.xml") if os.path.isdir(input_arg) else input_arg
    base_dir = os.path.dirname(os.path.abspath(__file__))
    lang_path = os.path.join(base_dir, "all_languages.json")
    memory_path = os.path.join(base_dir, "translation_memory.json")
    manual_dict_path = os.path.join(base_dir, "mnt/data/manual_dict.json")
    res_dir = os.path.dirname(os.path.dirname(input_xml))

    languages = load_json(lang_path)
    memory = load_json(memory_path)
    manual_dict = load_json(manual_dict_path)

    print(f"🚀 SYNC MODE (PROTECTED ORIGIN)")
    print("=" * 80)

    with ThreadPoolExecutor(max_workers=MAX_THREADS) as executor:
        for i, lang in enumerate(languages):
            executor.submit(
                translate_language,
                i % MAX_THREADS,
                lang["isoCode"],
                lang["name"],
                input_xml,
                res_dir,
                memory,
                manual_dict,
            )

    save_json(memory, memory_path)
    print("\n" + "=" * 80 + "\n✨ HOÀN THÀNH!")


if __name__ == "__main__":
    if len(sys.argv) > 1:
        main(sys.argv[1])
