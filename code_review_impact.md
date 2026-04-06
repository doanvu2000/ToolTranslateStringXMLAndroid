# Code Review Impact

Target: `test_special_cases.py`
Source lỗi: `translate.py`
Issue: `bug: Traceback (most recent call last): File \ D:\work\python\ToolTranslateStringXML\translate.py\, line 11, in <module> from deep_translator import GoogleTranslator ModuleNotFoundError: No module named 'deep_translator'`

## Impact Summary

- Mức ảnh hưởng: cao với runtime.
- Lỗi xảy ra ở import-time của `translate.py`, nên mọi file import module này sẽ fail trước khi vào test logic.
- Đây là lỗi dependency môi trường, không phải bug nghiệp vụ trong `test_special_cases.py`.

## Direct Import Impact

| Module bị ảnh hưởng | Chuỗi ảnh hưởng | Trạng thái |
| --- | --- |
| `test_special_cases.py` | `translate -> test_special_cases` | bị chặn ở import-time |

## Impacted Functions

| Function | Why impacted |
| --- | --- |
| `test_special_cases.py:check()` @ L33 | Import `translate.py` fail trước khi function này được gọi |
| `test_special_cases.py:mock_translate()` @ L208 | Import `translate.py` fail trước khi function này được gọi |

## Review Notes

- `test_special_cases.py` import trực tiếp nhiều helper từ `translate.py`, nên test bị chặn hoàn toàn.
- Muốn test độc lập hơn, có thể tách lớp import `GoogleTranslator` ra lazy import hoặc inject dependency.
- Nếu chỉ cần chạy test unit helper, nên tránh hard dependency vào package ngoài ngay tại top-level import.

## Suggested Fix

1. Cài `deep_translator` trong môi trường test, hoặc
2. Chuyển `from deep_translator import GoogleTranslator` vào trong `throttled_translate()`, hoặc
3. Bao import bằng fallback rõ ràng để test helper không phụ thuộc package ngoài.
