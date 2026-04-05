"""
Test script for special translation cases.
Uses a mock translator to avoid real API calls.
"""
import sys
import os
import xml.etree.ElementTree as ET
import re
import html as html_lib
from unittest.mock import patch

# Add project root to path
sys.path.insert(0, os.path.dirname(__file__))

# Import helpers from translate.py
from translate import (
    escape_android_chars,
    protect_translatables,
    restore_translatables,
    get_inner_xml,
    set_inner_xml,
    preprocess_cdata,
    postprocess_cdata,
    extract_cdata_names,
    translate_string,
    apply_manual_dict,
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


# ===========================================================================
# 1. Single quote escaping
# ===========================================================================
print("\n── 1. SINGLE QUOTE ──────────────────────────────────────────")
check("escape single quote",
      escape_android_chars("Let's play"),
      "Let\\'s play")

check("escape double quote",
      escape_android_chars('Say "hello"'),
      'Say \\"hello\\"')

check("no double-escape on clean text",
      escape_android_chars("Hello world"),
      "Hello world")

# ===========================================================================
# 2. Format specifier protection
# ===========================================================================
print("\n── 2. FORMAT SPECIFIERS ─────────────────────────────────────")

protected, ph_map = protect_translatables("Your score is %1$d points and %s")
check("protect %1$d and %s",
      ("%1$d" in ph_map.values() and "%s" in ph_map.values()),
      True)
check("placeholders in protected text",
      "%1$d" not in protected and "%s" not in protected,
      True)

restored = restore_translatables(protected, ph_map)
check("restore format specifiers",
      restored,
      "Your score is %1$d points and %s")

# Simulate: what Google Translate does (moves placeholder around in sentence)
protected2, ph_map2 = protect_translatables("Bạn có %1$d tin nhắn mới")
# simulate translate just leaves placeholders in place
restored2 = restore_translatables(protected2, ph_map2)
check("restore after simulated translate",
      "%1$d" in restored2,
      True)

# ===========================================================================
# 3. HTML strings
# ===========================================================================
print("\n── 3. HTML STRINGS ──────────────────────────────────────────")

# get_inner_xml
elem = ET.fromstring('<string name="x">Hello <b>World</b>!</string>')
check("get_inner_xml with child tags",
      get_inner_xml(elem),
      "Hello <b>World</b>!")

# set_inner_xml
elem2 = ET.fromstring('<string name="x">old</string>')
set_inner_xml(elem2, "Xin chào <b>Thế giới</b>!")
check("set_inner_xml rebuilds element",
      get_inner_xml(elem2),
      "Xin chào <b>Thế giới</b>!")
check("set_inner_xml child count",
      len(elem2) == 1,
      True)

# HTML tag protection
text = "Click <b>here</b> to continue"
protected, ph_map = protect_translatables(text)
check("HTML tags replaced with placeholders",
      "<b>" not in protected and "</b>" not in protected,
      True)
check("placeholder count for HTML",
      len(ph_map) == 2,   # <b> and </b>
      True)
restored = restore_translatables(protected, ph_map)
check("HTML tags restored",
      restored,
      "Click <b>here</b> to continue")

# HTML + format spec together
mixed = 'Bạn có <b>%1$d</b> thông báo'
protected_m, ph_map_m = protect_translatables(mixed)
check("HTML + format spec both protected",
      "<b>" not in protected_m and "%1$d" not in protected_m,
      True)
restored_m = restore_translatables(protected_m, ph_map_m)
check("HTML + format spec both restored",
      restored_m,
      'Bạn có <b>%1$d</b> thông báo')

# ===========================================================================
# 4. CDATA
# ===========================================================================
print("\n── 4. CDATA ─────────────────────────────────────────────────")

source_xml = '''<?xml version="1.0" encoding="utf-8"?>
<resources>
    <string name="app_name" translatable="false">MyApp</string>
    <string name="txt_plain">Hello world</string>
    <string name="txt_html"><![CDATA[<b>Bold</b> & great]]></string>
    <string name="txt_ampersand"><![CDATA[Tom & Jerry]]></string>
</resources>'''

# extract_cdata_names
cdata_names = extract_cdata_names(source_xml)
check("extract CDATA names",
      cdata_names,
      {"txt_html", "txt_ampersand"})

# preprocess_cdata: strip CDATA, XML-escape content
clean = preprocess_cdata(source_xml)
check("CDATA wrapper removed after preprocess",
      "<![CDATA[" not in clean,
      True)
check("& escaped to &amp; after preprocess",
      "&amp;" in clean,
      True)
check("plain strings unchanged after preprocess",
      "Hello world" in clean,
      True)

# ET can parse preprocessed XML
root = ET.fromstring(clean)
html_elem = root.find('.//string[@name="txt_html"]')
amp_elem  = root.find('.//string[@name="txt_ampersand"]')
check("ET reads CDATA text correctly (html)",
      get_inner_xml(html_elem),
      "<b>Bold</b> & great")
check("ET reads CDATA text correctly (ampersand)",
      amp_elem.text,
      "Tom & Jerry")

# postprocess_cdata: write a temp file, run postprocess, read back
import tempfile, os

tmp = tempfile.NamedTemporaryFile(mode='w', suffix='.xml', delete=False, encoding='utf-8')
tmp.write('''<?xml version="1.0" encoding="utf-8"?>
<resources>
<string name="txt_plain">Xin chào thế giới</string>
<string name="txt_html">&lt;b&gt;Đậm&lt;/b&gt; &amp; tuyệt</string>
<string name="txt_ampersand">Tom &amp; Jerry</string>
</resources>''')
tmp.close()

postprocess_cdata(tmp.name, {"txt_html", "txt_ampersand"})

with open(tmp.name, 'r', encoding='utf-8') as f:
    result_xml = f.read()
os.unlink(tmp.name)

check("postprocess: plain string NOT wrapped in CDATA",
      'name="txt_plain"' in result_xml and "<![CDATA[" not in result_xml.split('name="txt_plain"')[1].split('</string>')[0],
      True)
check("postprocess: CDATA re-wrapped for txt_html",
      '<![CDATA[<b>Đậm</b> & tuyệt]]>' in result_xml,
      True)
check("postprocess: CDATA re-wrapped for txt_ampersand",
      '<![CDATA[Tom & Jerry]]>' in result_xml,
      True)

# ===========================================================================
# 5. translate_string (mock API)
# ===========================================================================
print("\n── 5. translate_string (mock) ───────────────────────────────")

def mock_translate(text, dest):
    # Simulate Google Translate: keep placeholders, translate readable words
    return text.replace("Hello", "Xin chào").replace("world", "thế giới")

with patch('translate.throttled_translate', side_effect=mock_translate):
    result = translate_string("Hello world", 'vi', {})
    check("translate plain text",
          result,
          "Xin chào thế giới")

    result2 = translate_string("Hello %1$s world", 'vi', {})
    check("translate with format spec preserved",
          "%1$s" in result2,
          True)

    result3 = translate_string("Hello <b>world</b>", 'vi', {})
    check("translate with HTML tags preserved",
          "<b>" in result3 and "</b>" in result3,
          True)
    check("translate with HTML tags: text translated",
          "Xin chào" in result3 or "thế giới" in result3,
          True)

# ===========================================================================
# Summary
# ===========================================================================
print("\n" + "=" * 60)
passed = sum(1 for s, _ in results if s == PASS)
failed = sum(1 for s, _ in results if s == FAIL)
print(f"  Kết quả: {passed}/{len(results)} test passed", end="")
if failed:
    print(f"  ({failed} FAILED)")
    sys.exit(1)
else:
    print(" — tất cả OK ✨")
