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


def read_output(res_dir, iso_code):
    """Read a translated output file and return (raw_text, parsed_root)."""
    dest = os.path.join(res_dir, f"values-{iso_code}", "strings.xml")
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
