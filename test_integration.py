"""
Integration tests for the full translation pipeline.
Exercises main() → translate_language() with mock API, covering:
  - plain strings, string-array, plurals
  - CDATA sections, HTML tags, format specifiers
  - translatable="false" skipping
  - incremental translation (existing target file preserved)
  - XML comments in source
"""
import json
import os
import shutil
import sys
import tempfile
import xml.etree.ElementTree as ET
from unittest.mock import patch

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

sys.path.insert(0, os.path.dirname(__file__))

from translate import (
    main,
    preprocess_cdata,
    get_inner_xml,
)

PASS = "✅ PASS"
FAIL = "❌ FAIL"
results = []


def check(name, actual, expected):
    ok = actual == expected
    status = PASS if ok else FAIL
    results.append((status, name))
    if not ok:
        print(f"{FAIL} {name}")
        print(f"       expected: {repr(expected)}")
        print(f"       actual  : {repr(actual)}")
    else:
        print(f"{PASS} {name}")


def check_true(name, condition):
    check(name, condition, True)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

SOURCE_XML = """\
<?xml version="1.0" encoding="utf-8"?>
<resources>
    <!-- App identity -->
    <string name="app_name" translatable="false">MyApp</string>

    <!-- Plain strings -->
    <string name="txt_hello">Hello world</string>
    <string name="txt_greeting">Good morning</string>

    <!-- HTML string -->
    <string name="txt_bold">Click <b>here</b> to start</string>

    <!-- Format specifier -->
    <string name="txt_score">Score: %1$d points</string>

    <!-- CDATA string -->
    <string name="txt_terms"><![CDATA[<a href="https://example.com">Terms</a> & Conditions]]></string>

    <!-- String array -->
    <string-array name="days">
        <item>Monday</item>
        <item>Tuesday</item>
        <item>Wednesday</item>
    </string-array>

    <!-- Plurals -->
    <plurals name="messages">
        <item quantity="one">%d message</item>
        <item quantity="other">%d messages</item>
    </plurals>
</resources>
"""

LANGUAGES_2 = [
    {"isoCode": "vi", "name": "Vietnamese"},
    {"isoCode": "fr", "name": "French"},
]


def mock_translate(text, dest):
    """Deterministic mock: prepend language code in brackets."""
    return f"[{dest}]{text}"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def setup_workspace():
    """Create a temporary workspace with source XML and languages JSON."""
    tmp = tempfile.mkdtemp(prefix="translate_test_")
    # Source dir structure: tmp/res/values/strings.xml
    values_dir = os.path.join(tmp, "res", "values")
    os.makedirs(values_dir)
    source_path = os.path.join(values_dir, "strings.xml")
    with open(source_path, "w", encoding="utf-8") as f:
        f.write(SOURCE_XML)

    # Languages JSON
    lang_path = os.path.join(tmp, "languages.json")
    with open(lang_path, "w", encoding="utf-8") as f:
        json.dump(LANGUAGES_2, f)

    return tmp, source_path, lang_path, os.path.join(tmp, "res")


def iso_to_android_folder(iso_code):
    """Convert ISO code to Android resource folder name."""
    if '-' in iso_code:
        lang, region = iso_code.split('-', 1)
        return f"values-{lang}-r{region.upper()}"
    return f"values-{iso_code}"


def read_output(res_dir, iso_code):
    """Read a translated output file and return (raw_text, parsed_root)."""
    dest = os.path.join(res_dir, iso_to_android_folder(iso_code), "strings.xml")
    if not os.path.exists(dest):
        return None, None
    with open(dest, "r", encoding="utf-8") as f:
        raw = f.read()
    clean = preprocess_cdata(raw)
    root = ET.fromstring(clean)
    return raw, root


def get_string_text(root, name):
    """Get the inner XML of a <string name="..."> element."""
    elem = root.find(f'.//string[@name="{name}"]')
    if elem is None:
        return None
    return get_inner_xml(elem)


def get_array_items(root, name):
    """Get list of item texts from a <string-array>."""
    arr = root.find(f'.//string-array[@name="{name}"]')
    if arr is None:
        return None
    return [get_inner_xml(item) for item in arr.findall("item")]


def get_plural_items(root, name):
    """Get list of item texts from a <plurals>."""
    plu = root.find(f'.//plurals[@name="{name}"]')
    if plu is None:
        return None
    return [get_inner_xml(item) for item in plu.findall("item")]


# ===========================================================================
# Test 1: Full pipeline — fresh translation (no existing target)
# ===========================================================================
print("\n── TEST 1: Fresh translation pipeline ──────────────────────")

tmp_dir, source_path, lang_path, res_dir = setup_workspace()
try:
    with patch("translate.throttled_translate", side_effect=mock_translate):
        # Patch cache DB path to use temp dir
        cache_db = os.path.join(tmp_dir, "test_cache.db")
        with patch("translate.os.path.dirname", return_value=tmp_dir):
            main(source_path, lang_path=lang_path, output_dir=res_dir, threads=2)

    # -- Vietnamese output --
    raw_vi, root_vi = read_output(res_dir, "vi")
    check_true("Vietnamese output file created", root_vi is not None)

    # translatable="false" should still appear (copied from source) but NOT translated
    app_name = get_string_text(root_vi, "app_name")
    check("translatable=false preserved as-is", app_name, "MyApp")

    # Plain string translated
    hello = get_string_text(root_vi, "txt_hello")
    check_true("plain string translated (vi)", hello is not None and "[vi]" in hello)

    greeting = get_string_text(root_vi, "txt_greeting")
    check_true("second plain string translated (vi)", greeting is not None and "[vi]" in greeting)

    # HTML string: tags should be preserved
    bold = get_string_text(root_vi, "txt_bold")
    check_true("HTML <b> tag preserved in translation", bold is not None and "<b>" in bold and "</b>" in bold)

    # Format specifier preserved
    score = get_string_text(root_vi, "txt_score")
    check_true("format spec %1$d preserved", score is not None and "%1$d" in score)

    # CDATA string
    terms_raw = raw_vi
    check_true("CDATA re-wrapped in output", "<![CDATA[" in terms_raw)
    terms_text = get_string_text(root_vi, "txt_terms")
    check_true("CDATA content translated", terms_text is not None and "[vi]" in terms_text)

    # String array
    days = get_array_items(root_vi, "days")
    check_true("string-array has 3 items", days is not None and len(days) == 3)
    check_true("string-array items translated", all("[vi]" in d for d in days))

    # Plurals
    msgs = get_plural_items(root_vi, "messages")
    check_true("plurals has 2 items", msgs is not None and len(msgs) == 2)
    check_true("plural items translated", all("[vi]" in m for m in msgs))
    check_true("plural format spec preserved", all("%d" in m for m in msgs))

    # -- French output --
    raw_fr, root_fr = read_output(res_dir, "fr")
    check_true("French output file created", root_fr is not None)
    hello_fr = get_string_text(root_fr, "txt_hello")
    check_true("French plain string translated", hello_fr is not None and "[fr]" in hello_fr)

finally:
    shutil.rmtree(tmp_dir, ignore_errors=True)


# ===========================================================================
# Test 2: Incremental translation — existing target file preserved
# ===========================================================================
print("\n── TEST 2: Incremental translation ─────────────────────────")

tmp_dir, source_path, lang_path, res_dir = setup_workspace()
try:
    # Pre-create a Vietnamese target with a manual edit for txt_hello
    vi_dir = os.path.join(res_dir, "values-vi")
    os.makedirs(vi_dir)
    existing_xml = """\
<?xml version="1.0" encoding="utf-8"?>
<resources>
    <string name="txt_hello">Xin chào thế giới (manual edit)</string>
    <string name="txt_greeting">Good morning</string>
</resources>
"""
    with open(os.path.join(vi_dir, "strings.xml"), "w", encoding="utf-8") as f:
        f.write(existing_xml)

    with patch("translate.throttled_translate", side_effect=mock_translate):
        cache_db = os.path.join(tmp_dir, "test_cache.db")
        with patch("translate.os.path.dirname", return_value=tmp_dir):
            main(source_path, lang_path=lang_path, output_dir=res_dir, threads=2)

    raw_vi, root_vi = read_output(res_dir, "vi")
    check_true("incremental: output file exists", root_vi is not None)

    # txt_hello was manually edited (differs from source) → should be KEPT
    hello = get_string_text(root_vi, "txt_hello")
    check("incremental: manual edit preserved",
          hello, "Xin chào thế giới (manual edit)")

    # txt_greeting was same as source → should be RE-TRANSLATED
    greeting = get_string_text(root_vi, "txt_greeting")
    check_true("incremental: unchanged string re-translated",
               greeting is not None and "[vi]" in greeting)

    # New strings (not in old target) should be translated
    score = get_string_text(root_vi, "txt_score")
    check_true("incremental: new string translated", score is not None and "[vi]" in score)

finally:
    shutil.rmtree(tmp_dir, ignore_errors=True)


# ===========================================================================
# Test 3: Cache hit — second run uses cache instead of API
# ===========================================================================
print("\n── TEST 3: Cache hit on second run ─────────────────────────")

tmp_dir, source_path, lang_path, res_dir = setup_workspace()
# Use only Vietnamese for this test
lang1 = [{"isoCode": "vi", "name": "Vietnamese"}]
lang_path_1 = os.path.join(tmp_dir, "lang1.json")
with open(lang_path_1, "w", encoding="utf-8") as f:
    json.dump(lang1, f)

api_call_count = [0]
original_mock = mock_translate

def counting_mock(text, dest):
    api_call_count[0] += 1
    return original_mock(text, dest)

try:
    # First run: populates cache
    with patch("translate.throttled_translate", side_effect=counting_mock):
        with patch("translate.os.path.dirname", return_value=tmp_dir):
            main(source_path, lang_path=lang_path_1, output_dir=res_dir, threads=1)

    first_run_calls = api_call_count[0]
    check_true("first run made API calls", first_run_calls > 0)

    # Delete output so second run doesn't use existing target as "already translated"
    vi_dir = os.path.join(res_dir, "values-vi")
    if os.path.exists(vi_dir):
        shutil.rmtree(vi_dir)

    # Second run: should use cache, zero API calls
    api_call_count[0] = 0
    with patch("translate.throttled_translate", side_effect=counting_mock):
        with patch("translate.os.path.dirname", return_value=tmp_dir):
            main(source_path, lang_path=lang_path_1, output_dir=res_dir, threads=1)

    # HTML strings (txt_bold has <b> child elements) skip cache by design,
    # so exactly 1 API call is expected for the HTML string on second run.
    check("second run uses cache (only HTML re-translated)", api_call_count[0], 1)

    # Output should still be correct
    _, root_vi = read_output(res_dir, "vi")
    hello = get_string_text(root_vi, "txt_hello")
    check_true("cache hit produces correct translation", hello is not None and "[vi]" in hello)

finally:
    shutil.rmtree(tmp_dir, ignore_errors=True)


# ===========================================================================
# Test 4: Mixed XML — strings deleted from source are removed from output
# ===========================================================================
print("\n── TEST 4: Deleted strings removed from output ──────────────")

tmp_dir, source_path, lang_path, res_dir = setup_workspace()
try:
    # Pre-create target with an extra string that no longer exists in source
    vi_dir = os.path.join(res_dir, "values-vi")
    os.makedirs(vi_dir)
    existing_xml = """\
<?xml version="1.0" encoding="utf-8"?>
<resources>
    <string name="txt_hello">Xin chào (old)</string>
    <string name="txt_obsolete">This string was removed from source</string>
</resources>
"""
    with open(os.path.join(vi_dir, "strings.xml"), "w", encoding="utf-8") as f:
        f.write(existing_xml)

    with patch("translate.throttled_translate", side_effect=mock_translate):
        with patch("translate.os.path.dirname", return_value=tmp_dir):
            # Only Vietnamese
            lang1 = [{"isoCode": "vi", "name": "Vietnamese"}]
            lang_path_1 = os.path.join(tmp_dir, "lang1.json")
            with open(lang_path_1, "w", encoding="utf-8") as f:
                json.dump(lang1, f)
            main(source_path, lang_path=lang_path_1, output_dir=res_dir, threads=1)

    _, root_vi = read_output(res_dir, "vi")
    obsolete = root_vi.find('.//string[@name="txt_obsolete"]')
    check("deleted string not in output", obsolete, None)

    # txt_hello should still be preserved (was manually edited)
    hello = get_string_text(root_vi, "txt_hello")
    check("existing edited string kept", hello, "Xin chào (old)")

finally:
    shutil.rmtree(tmp_dir, ignore_errors=True)


# ===========================================================================
# Test 5: Manual overrides — AM/PM preserved through full pipeline
# ===========================================================================
print("\n── TEST 5: Manual overrides (AM/PM preserved) ───────────────")

OVERRIDE_SOURCE_XML = """\
<?xml version="1.0" encoding="utf-8"?>
<resources>
    <string name="txt_time">From 9 AM to 5 PM</string>
    <string name="txt_wifi">Connect to Wi-Fi network</string>
    <string name="txt_plain">Hello world</string>
</resources>
"""

tmp_dir, _, _, res_dir = setup_workspace()
try:
    # Write custom source
    source_path = os.path.join(os.path.dirname(res_dir), "res", "values", "strings.xml")
    with open(source_path, "w", encoding="utf-8") as f:
        f.write(OVERRIDE_SOURCE_XML)

    # Write overrides.json
    overrides = {"AM": "AM", "PM": "PM", "Wi-Fi": "Wi-Fi"}
    overrides_path = os.path.join(tmp_dir, "overrides.json")
    with open(overrides_path, "w", encoding="utf-8") as f:
        json.dump(overrides, f)

    # Languages: Vietnamese only
    lang1 = [{"isoCode": "vi", "name": "Vietnamese"}]
    lang_path_1 = os.path.join(tmp_dir, "lang1.json")
    with open(lang_path_1, "w", encoding="utf-8") as f:
        json.dump(lang1, f)

    with patch("translate.throttled_translate", side_effect=mock_translate):
        with patch("translate.os.path.dirname", return_value=tmp_dir):
            main(source_path, lang_path=lang_path_1, output_dir=res_dir,
                 threads=1, overrides_path=overrides_path)

    _, root_vi = read_output(res_dir, "vi")
    check_true("overrides: output file created", root_vi is not None)

    time_str = get_string_text(root_vi, "txt_time")
    check_true("overrides: AM preserved in output", time_str is not None and "AM" in time_str)
    check_true("overrides: PM preserved in output", time_str is not None and "PM" in time_str)

    wifi_str = get_string_text(root_vi, "txt_wifi")
    check_true("overrides: Wi-Fi preserved in output", wifi_str is not None and "Wi-Fi" in wifi_str)

    plain = get_string_text(root_vi, "txt_plain")
    check_true("overrides: non-override string still translated", plain is not None and "[vi]" in plain)

finally:
    shutil.rmtree(tmp_dir, ignore_errors=True)


# ===========================================================================
# Test 6: XML validation — corrupt output restores backup
# ===========================================================================
print("\n── TEST 6: XML output validation ───────────────────────────")

from translate import postprocess_cdata as real_postprocess

tmp_dir, source_path, lang_path, res_dir = setup_workspace()
try:
    # Pre-create a valid Vietnamese target (the "backup")
    vi_dir = os.path.join(res_dir, "values-vi")
    os.makedirs(vi_dir)
    backup_xml = '<?xml version="1.0" encoding="utf-8"?>\n<resources><string name="txt_hello">Backup</string></resources>'
    with open(os.path.join(vi_dir, "strings.xml"), "w", encoding="utf-8") as f:
        f.write(backup_xml)

    def corrupt_postprocess(dest_file, cdata_names):
        """Simulate CDATA postprocess that produces invalid XML."""
        real_postprocess(dest_file, cdata_names)
        # Inject corruption: unclosed tag
        with open(dest_file, "r", encoding="utf-8") as f:
            content = f.read()
        content = content.replace("</resources>", "<broken></resources>")  # unclosed
        # Actually break it more:
        content = content.replace("<broken>", "<broken")  # truly invalid XML
        with open(dest_file, "w", encoding="utf-8") as f:
            f.write(content)

    lang1 = [{"isoCode": "vi", "name": "Vietnamese"}]
    lang_path_1 = os.path.join(tmp_dir, "lang1.json")
    with open(lang_path_1, "w", encoding="utf-8") as f:
        json.dump(lang1, f)

    with patch("translate.throttled_translate", side_effect=mock_translate):
        with patch("translate.postprocess_cdata", side_effect=corrupt_postprocess):
            with patch("translate.os.path.dirname", return_value=tmp_dir):
                main(source_path, lang_path=lang_path_1, output_dir=res_dir, threads=1)

    # The output should be the backup (restored after validation failure)
    dest = os.path.join(vi_dir, "strings.xml")
    check_true("validation: file still exists after corruption", os.path.exists(dest))
    with open(dest, "r", encoding="utf-8") as f:
        content = f.read()
    check_true("validation: backup restored after corrupt output", "Backup" in content)
    check_true("validation: corrupt content not in file", "<broken" not in content)

finally:
    shutil.rmtree(tmp_dir, ignore_errors=True)


# ===========================================================================
# Test 7: XML comments & attributes preserved in output
# ===========================================================================
print("\n── TEST 7: XML comments & attributes preserved ──────────────")

COMMENT_SOURCE_XML = """\
<?xml version="1.0" encoding="utf-8"?>
<resources>
    <!-- Section: identity -->
    <string name="app_name" translatable="false">MyApp</string>

    <!-- Section: greetings -->
    <string name="txt_hello" formatted="false">Hello world</string>
    <string name="txt_bye">Goodbye</string>

    <!-- Section: arrays -->
    <string-array name="colors">
        <item>Red</item>
        <item>Blue</item>
    </string-array>
</resources>
"""

tmp_dir, _, _, res_dir = setup_workspace()
try:
    source_path = os.path.join(os.path.dirname(res_dir), "res", "values", "strings.xml")
    with open(source_path, "w", encoding="utf-8") as f:
        f.write(COMMENT_SOURCE_XML)

    lang1 = [{"isoCode": "vi", "name": "Vietnamese"}]
    lang_path_1 = os.path.join(tmp_dir, "lang1.json")
    with open(lang_path_1, "w", encoding="utf-8") as f:
        json.dump(lang1, f)

    with patch("translate.throttled_translate", side_effect=mock_translate):
        with patch("translate.os.path.dirname", return_value=tmp_dir):
            main(source_path, lang_path=lang_path_1, output_dir=res_dir, threads=1)

    dest = os.path.join(res_dir, "values-vi", "strings.xml")
    with open(dest, "r", encoding="utf-8") as f:
        output_raw = f.read()

    # Comments preserved
    check_true("comments: 'Section: identity' in output",
               "<!-- Section: identity -->" in output_raw)
    check_true("comments: 'Section: greetings' in output",
               "<!-- Section: greetings -->" in output_raw)
    check_true("comments: 'Section: arrays' in output",
               "<!-- Section: arrays -->" in output_raw)

    # Attributes preserved
    check_true("attributes: translatable='false' preserved",
               'translatable="false"' in output_raw)
    check_true("attributes: formatted='false' preserved",
               'formatted="false"' in output_raw)

    # Strings still translated correctly
    root_vi = ET.fromstring(output_raw)
    hello = get_string_text(root_vi, "txt_hello")
    check_true("comments test: string translated", hello is not None and "[vi]" in hello)

    app_name = get_string_text(root_vi, "app_name")
    check("comments test: translatable=false not translated", app_name, "MyApp")

finally:
    shutil.rmtree(tmp_dir, ignore_errors=True)


# ===========================================================================
# Test 8: File logging — --log-file writes detailed log
# ===========================================================================
print("\n── TEST 8: File logging ─────────────────────────────────────")

tmp_dir, source_path, lang_path, res_dir = setup_workspace()
try:
    lang1 = [{"isoCode": "vi", "name": "Vietnamese"}]
    lang_path_1 = os.path.join(tmp_dir, "lang1.json")
    with open(lang_path_1, "w", encoding="utf-8") as f:
        json.dump(lang1, f)

    log_path = os.path.join(tmp_dir, "translate.log")

    with patch("translate.throttled_translate", side_effect=mock_translate):
        with patch("translate.os.path.dirname", return_value=tmp_dir):
            main(source_path, lang_path=lang_path_1, output_dir=res_dir,
                 threads=1, log_file=log_path)

    check_true("log file created", os.path.exists(log_path))

    with open(log_path, "r", encoding="utf-8") as f:
        log_content = f.read()

    # Log file should have timestamps
    check_true("log has timestamps", "[INFO]" in log_content or "[DEBUG]" in log_content)
    # Log file should contain pass info
    check_true("log contains PASS result", "PASS" in log_content)
    # Log file should contain summary
    check_true("log contains summary", "HOÀN THÀNH" in log_content or "Pass" in log_content)

finally:
    shutil.rmtree(tmp_dir, ignore_errors=True)


# ===========================================================================
# Test 9: Regional language variants (zh-CN, pt-BR → values-zh-rCN, values-pt-rBR)
# ===========================================================================
print("\n── TEST 9: Regional language variants ───────────────────────")

REGIONAL_SOURCE_XML = """\
<?xml version="1.0" encoding="utf-8"?>
<resources>
    <string name="txt_hello">Hello world</string>
</resources>
"""

tmp_dir, _, _, res_dir = setup_workspace()
try:
    source_path = os.path.join(os.path.dirname(res_dir), "res", "values", "strings.xml")
    with open(source_path, "w", encoding="utf-8") as f:
        f.write(REGIONAL_SOURCE_XML)

    regional_langs = [
        {"isoCode": "zh-CN", "name": "Chinese Simplified"},
        {"isoCode": "zh-TW", "name": "Chinese Traditional"},
        {"isoCode": "pt-BR", "name": "Portuguese Brazilian"},
        {"isoCode": "pt", "name": "Portuguese"},
        {"isoCode": "fr", "name": "French"},
    ]
    lang_path_r = os.path.join(tmp_dir, "regional_langs.json")
    with open(lang_path_r, "w", encoding="utf-8") as f:
        json.dump(regional_langs, f)

    with patch("translate.throttled_translate", side_effect=mock_translate):
        with patch("translate.os.path.dirname", return_value=tmp_dir):
            main(source_path, lang_path=lang_path_r, output_dir=res_dir, threads=2)

    # Check Android folder naming
    check_true("zh-CN → values-zh-rCN",
               os.path.exists(os.path.join(res_dir, "values-zh-rCN", "strings.xml")))
    check_true("zh-TW → values-zh-rTW",
               os.path.exists(os.path.join(res_dir, "values-zh-rTW", "strings.xml")))
    check_true("pt-BR → values-pt-rBR",
               os.path.exists(os.path.join(res_dir, "values-pt-rBR", "strings.xml")))
    check_true("pt → values-pt (no region)",
               os.path.exists(os.path.join(res_dir, "values-pt", "strings.xml")))
    check_true("fr → values-fr (no region)",
               os.path.exists(os.path.join(res_dir, "values-fr", "strings.xml")))

    # Content translated with correct language code
    _, root_zh_cn = read_output(res_dir, "zh-CN")
    hello_cn = get_string_text(root_zh_cn, "txt_hello")
    check_true("zh-CN content translated", hello_cn is not None and "[zh-CN]" in hello_cn)

    _, root_pt_br = read_output(res_dir, "pt-BR")
    hello_br = get_string_text(root_pt_br, "txt_hello")
    check_true("pt-BR content translated", hello_br is not None and "[pt-BR]" in hello_br)

finally:
    shutil.rmtree(tmp_dir, ignore_errors=True)


# ===========================================================================
# Test 10: Dry-run mode — no output files written
# ===========================================================================
print("\n── TEST 10: Dry-run mode ────────────────────────────────────")

tmp_dir, source_path, lang_path, res_dir = setup_workspace()
try:
    lang1 = [{"isoCode": "vi", "name": "Vietnamese"}]
    lang_path_1 = os.path.join(tmp_dir, "lang1.json")
    with open(lang_path_1, "w", encoding="utf-8") as f:
        json.dump(lang1, f)

    with patch("translate.throttled_translate", side_effect=mock_translate):
        with patch("translate.os.path.dirname", return_value=tmp_dir):
            main(source_path, lang_path=lang_path_1, output_dir=res_dir,
                 threads=1, dry_run=True)

    # No output file should be created
    vi_dir = os.path.join(res_dir, "values-vi")
    check_true("dry-run: no output folder created",
               not os.path.exists(vi_dir))

    # But pipeline still reports pass
    check_true("dry-run: completed without error", True)

    # Run again without dry-run to confirm files ARE written normally
    with patch("translate.throttled_translate", side_effect=mock_translate):
        with patch("translate.os.path.dirname", return_value=tmp_dir):
            main(source_path, lang_path=lang_path_1, output_dir=res_dir,
                 threads=1, dry_run=False)

    check_true("non-dry-run: output file created",
               os.path.exists(os.path.join(vi_dir, "strings.xml")))

finally:
    shutil.rmtree(tmp_dir, ignore_errors=True)


# ===========================================================================
# Test 11: --only language filter
# ===========================================================================
print("\n── TEST 11: --only language filter ──────────────────────────")

tmp_dir, source_path, lang_path, res_dir = setup_workspace()
try:
    # languages.json has vi and fr
    with patch("translate.throttled_translate", side_effect=mock_translate):
        with patch("translate.os.path.dirname", return_value=tmp_dir):
            main(source_path, lang_path=lang_path, output_dir=res_dir,
                 threads=2, only=["vi"])

    # Only Vietnamese should be created
    check_true("--only vi: Vietnamese output exists",
               os.path.exists(os.path.join(res_dir, "values-vi", "strings.xml")))
    check_true("--only vi: French output NOT created",
               not os.path.exists(os.path.join(res_dir, "values-fr", "strings.xml")))

    # Clean up for next sub-test
    shutil.rmtree(os.path.join(res_dir, "values-vi"), ignore_errors=True)

    # Test multiple --only
    with patch("translate.throttled_translate", side_effect=mock_translate):
        with patch("translate.os.path.dirname", return_value=tmp_dir):
            main(source_path, lang_path=lang_path, output_dir=res_dir,
                 threads=2, only=["vi", "fr"])

    check_true("--only vi fr: both outputs exist",
               os.path.exists(os.path.join(res_dir, "values-vi", "strings.xml")) and
               os.path.exists(os.path.join(res_dir, "values-fr", "strings.xml")))

finally:
    shutil.rmtree(tmp_dir, ignore_errors=True)


# ===========================================================================
# Test 12: Translation report JSON
# ===========================================================================
print("\n── TEST 12: Translation report JSON ─────────────────────────")

tmp_dir, source_path, lang_path, res_dir = setup_workspace()
try:
    report_path = os.path.join(tmp_dir, "report.json")

    with patch("translate.throttled_translate", side_effect=mock_translate):
        with patch("translate.os.path.dirname", return_value=tmp_dir):
            main(source_path, lang_path=lang_path, output_dir=res_dir,
                 threads=2, report_path=report_path)

    check_true("report: JSON file created", os.path.exists(report_path))

    with open(report_path, "r", encoding="utf-8") as f:
        report = json.load(f)

    check("report: languages_total", report["languages_total"], 2)
    check("report: languages_passed", report["languages_passed"], 2)
    check("report: languages_failed", report["languages_failed"], 0)
    check_true("report: duration_seconds is number",
               isinstance(report["duration_seconds"], (int, float)))

    # Check per-language details
    vi_result = report["results"].get("vi")
    check_true("report: vi result exists", vi_result is not None)
    check("report: vi status", vi_result["status"], "pass")
    check_true("report: vi has new count", "new" in vi_result)
    check_true("report: vi has cache count", "cache" in vi_result)

    fr_result = report["results"].get("fr")
    check_true("report: fr result exists", fr_result is not None)
    check("report: fr status", fr_result["status"], "pass")

finally:
    shutil.rmtree(tmp_dir, ignore_errors=True)


# ===========================================================================
# Test 13: Config file support
# ===========================================================================
print("\n── TEST 13: Config file support ─────────────────────────────")

tmp_dir, source_path, lang_path, res_dir = setup_workspace()
try:
    # Create a config that sets --only to vi
    config = {"only": ["vi"], "threads": 1}
    config_path = os.path.join(tmp_dir, "translate.config.json")
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(config, f)

    # Run with config (pass only= via main, simulating what CLI would do after loading config)
    with patch("translate.throttled_translate", side_effect=mock_translate):
        with patch("translate.os.path.dirname", return_value=tmp_dir):
            main(source_path, lang_path=lang_path, output_dir=res_dir,
                 threads=config["threads"], only=config["only"])

    check_true("config: vi output exists (from config only=[vi])",
               os.path.exists(os.path.join(res_dir, "values-vi", "strings.xml")))
    check_true("config: fr NOT created (filtered by config)",
               not os.path.exists(os.path.join(res_dir, "values-fr", "strings.xml")))

finally:
    shutil.rmtree(tmp_dir, ignore_errors=True)


# ===========================================================================
# Summary
# ===========================================================================
print("\n" + "=" * 60)
passed = sum(1 for s, _ in results if s == PASS)
failed = sum(1 for s, _ in results if s == FAIL)
print(f"  Integration tests: {passed}/{len(results)} passed", end="")
if failed:
    print(f"  ({failed} FAILED)")
    for s, name in results:
        if s == FAIL:
            print(f"    ❌ {name}")
    sys.exit(1)
else:
    print(" — all OK ✨")
