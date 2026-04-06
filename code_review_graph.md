# Code Review Graph

Artifact này được sinh tự động từ mã Python trong repo để hỗ trợ review nhanh theo dependency, call flow và hotspot rủi ro.

## File Dependency Graph

```mermaid
flowchart LR
    test_special_cases["test_special_cases.py"]
    tools_build_code_review_graph["tools/build_code_review_graph.py"]
    translate["translate.py"]
    test_special_cases --> translate
```

## Function Call Graph

```mermaid
flowchart TD
    test_special_cases_check["test_special_cases.py:check"]
    test_special_cases_mock_translate["test_special_cases.py:mock_translate"]
    tools_build_code_review_graph_parse_module["build_code_review_graph.py:parse_module"]
    tools_build_code_review_graph_build_markdown["build_code_review_graph.py:build_markdown"]
    tools_build_code_review_graph_build_mermaid_function_graph["build_code_review_graph.py:build_mermaid_function_graph"]
    tools_build_code_review_graph_generate_graph["build_code_review_graph.py:generate_graph"]
    translate_translate_language["translate.py:translate_language"]
    translate_main["translate.py:main"]
    translate_throttled_translate["translate.py:throttled_translate"]
    translate_translate_string["translate.py:translate_string"]
    tools_build_code_review_graph_build_markdown --> tools_build_code_review_graph_build_mermaid_function_graph
    tools_build_code_review_graph_generate_graph --> tools_build_code_review_graph_build_markdown
    tools_build_code_review_graph_generate_graph --> tools_build_code_review_graph_parse_module
    translate_translate_string --> translate_throttled_translate
    translate_translate_language --> translate_translate_string
```

## Detailed Function Graph: translate.py

```mermaid
flowchart LR
    translate_throttled_translate["throttled_translate()\nL44 [broad_except, global_state, network]"]
    translate_load_json["load_json()\nL65 [io]"]
    translate_save_json["save_json()\nL77 [io]"]
    translate_escape_android_chars["escape_android_chars()\nL82"]
    translate_protect_translatables["protect_translatables()\nL93 [regex]"]
    translate_restore_translatables["restore_translatables()\nL114"]
    translate_apply_manual_dict["apply_manual_dict()\nL124 [regex]"]
    translate_get_inner_xml["get_inner_xml()\nL140 [xml]"]
    translate_set_inner_xml["set_inner_xml()\nL148 [xml]"]
    translate_extract_cdata_names["extract_cdata_names()\nL167 [regex]"]
    translate_preprocess_cdata["preprocess_cdata()\nL177 [regex]"]
    translate_postprocess_cdata["postprocess_cdata()\nL188 [io, regex]"]
    translate_translate_string["translate_string()\nL215 [broad_except, network]"]
    translate_format_duration["format_duration()\nL237"]
    translate_refresh_console["refresh_console()\nL249 [io]"]
    translate_translate_language["translate_language()\nL261 [broad_except, io, network, xml]"]
    translate_main["main()\nL459 [io, threading, xml]"]
    %% Internal calls
    translate_translate_string --> translate_apply_manual_dict
    translate_translate_string --> translate_protect_translatables
    translate_translate_string --> translate_restore_translatables
    translate_translate_string --> translate_throttled_translate
    translate_translate_language --> translate_escape_android_chars
    translate_translate_language --> translate_get_inner_xml
    translate_translate_language --> translate_postprocess_cdata
    translate_translate_language --> translate_preprocess_cdata
    translate_translate_language --> translate_refresh_console
    translate_translate_language --> translate_save_json
    translate_translate_language --> translate_set_inner_xml
    translate_translate_language --> translate_translate_string
    translate_main --> translate_extract_cdata_names
    translate_main --> translate_format_duration
    translate_main --> translate_load_json
    translate_main --> translate_preprocess_cdata
    translate_main --> translate_save_json
```

## Review Hotspots

| Module | Score | Tags | Notes |
| --- | ---: | --- | --- |
| `translate.py` | 22 | broad_except, global_state, io, network, regex, threading, xml | entrypoint, 591 lines, concurrency, external API |
| `tools/build_code_review_graph.py` | 8 | io | entrypoint, 421 lines |
| `test_special_cases.py` | 3 | - | 242 lines |

## Function Hotspots

| Function | Score | Tags |
| --- | ---: | --- |
| `translate.py:translate_language()` @ L261 | 19 | broad_except, io, network, xml |
| `translate.py:main()` @ L459 | 13 | io, threading, xml |
| `translate.py:throttled_translate()` @ L44 | 8 | broad_except, global_state, network |
| `translate.py:translate_string()` @ L215 | 6 | broad_except, network |
| `build_code_review_graph.py:parse_module()` @ L136 | 5 | io |
| `build_code_review_graph.py:build_markdown()` @ L299 | 4 | - |
| `translate.py:postprocess_cdata()` @ L188 | 4 | io, regex |
| `build_code_review_graph.py:build_mermaid_function_graph()` @ L211 | 3 | - |
| `build_code_review_graph.py:generate_graph()` @ L399 | 3 | io |
| `translate.py:load_json()` @ L65 | 3 | io |
| `translate.py:set_inner_xml()` @ L148 | 3 | xml |
| `translate.py:refresh_console()` @ L249 | 3 | io |

## Review Order

1. `translate.py:translate_language()` vì đây là luồng chính, có I/O, XML transform, cache và concurrency.
2. `translate.py:throttled_translate()` vì đụng API ngoài, retry và global rate limit.
3. `translate.py:translate_string()` vì là lớp bảo toàn placeholder/HTML trước khi gọi dịch.
4. `translate.py:postprocess_cdata()` vì sửa nội dung XML sau khi serialize, dễ gây hỏng output.
5. `test_special_cases.py` để kiểm tra coverage hiện tại và khoảng trống test.

## Coverage Gaps Suggested For Review

- Chưa thấy test race condition quanh `_last_call_time`, `_memory_lock` và `thread_status`.
- Chưa thấy test end-to-end cho `main()` với file đích đã tồn tại và dữ liệu bị lệch schema.
- Chưa thấy validation cho trường hợp parse XML lỗi ở file nguồn hoặc file đích.
