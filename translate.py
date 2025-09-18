import os
import json
import xml.etree.ElementTree as ET
from googletrans import Translator
import time


# Đọc cấu hình manual_dict từ tệp JSON
def load_manual_dict(file_path):
    with open(file_path, 'r', encoding='utf-8') as f:
        return json.load(f)


# Đọc danh sách ngôn ngữ từ tệp JSON
def load_languages_from_json(file_path):
    with open(file_path, 'r', encoding='utf-8') as f:
        return json.load(f)


# Load tệp XML gốc
def load_xml(file_path):
    tree = ET.parse(file_path)
    return tree.getroot()


# Chuyển đổi văn bản về chữ thường để tránh phân biệt hoa thường
def apply_case_correction(original, translated):
    # Nếu từ gốc có chữ hoa đầu tiên, giữ lại chữ hoa cho từ dịch
    if original.istitle():
        return translated.capitalize()
    return translated.lower()


# Dịch văn bản (kiểm tra trong từ điển theo từng ngôn ngữ)
def translate_text(text, dest_lang, manual_dict):
    # Lưu văn bản gốc để kiểm tra chữ hoa
    original_text = text
    text = text.lower()  # Chuyển văn bản sang chữ thường

    # Kiểm tra xem văn bản có trong từ điển của ngôn ngữ không, nếu có thì thay thế
    if dest_lang in manual_dict:
        for word, translated_word in manual_dict[dest_lang].items():
            text = text.replace(word, translated_word)

    # Dịch văn bản còn lại nếu cần thiết
    translator = Translator()
    try:
        translated = translator.translate(text, dest=dest_lang)
        translated_text = translated.text

        # Áp dụng lại chữ hoa đầu từ nếu cần
        return apply_case_correction(original_text, translated_text)
    except Exception as e:
        print(f"Error translating '{text}' to {dest_lang}: {e}")
        return text  # Trả về văn bản gốc nếu có lỗi


# Lưu kết quả dịch vào tệp XML trong thư mục tương ứng với isoCode
def save_translated_xml(root, isoCode, output_dir):
    # Tạo thư mục values-isoCode nếu chưa có
    folder_path = os.path.join(output_dir, f"values-{isoCode}")
    os.makedirs(folder_path, exist_ok=True)

    # Tạo cây XML mới và lưu lại
    new_tree = ET.ElementTree(root)

    # Lưu tệp vào thư mục values-isoCode
    output_path = os.path.join(folder_path, "strings.xml")
    new_tree.write(output_path, encoding="utf-8", xml_declaration=True)


# In thông báo bắt đầu và kết thúc tiến trình dịch cho từng ngôn ngữ
def print_progress_start(language_name, iso_code):
    print(f"---------------{language_name}({iso_code})==> START---------------")


def print_progress_done(language_name, iso_code, duration):
    print(f"---------------{language_name}({iso_code})==> done in values-{iso_code}---------------({duration:.2f}ms)")


def process_strings(input_xml_path, languages_json_path, manual_dict_path, output_dir):
    # Đọc cấu hình manual_dict từ tệp JSON
    manual_dict = load_manual_dict(manual_dict_path)

    # Đọc danh sách ngôn ngữ từ tệp JSON
    languages = load_languages_from_json(languages_json_path)

    # Lặp qua từng ngôn ngữ để dịch riêng biệt
    for lang in languages:
        iso_code = lang["isoCode"]
        language_name = lang["name"]

        # In thông báo bắt đầu tiến trình
        print_progress_start(language_name, iso_code)

        # Ghi thời gian bắt đầu
        start_time = time.time()

        # Load tệp XML gốc cho mỗi ngôn ngữ
        root = load_xml(input_xml_path)

        # Lặp qua từng phần tử <string> trong XML và dịch riêng biệt
        for string_elem in root.findall('string'):
            # Kiểm tra thuộc tính translatable, nếu là "false" thì bỏ qua
            translatable = string_elem.get('translatable')
            if translatable and translatable.lower() == "false":
                continue  # Bỏ qua các string không thể dịch

            # Dịch văn bản cho ngôn ngữ hiện tại
            translated_text = translate_text(string_elem.text, iso_code, manual_dict)
            string_elem.text = translated_text  # Cập nhật văn bản dịch

        # Lưu tệp dịch vào thư mục tương ứng với isoCode sau khi dịch hoàn thành
        save_translated_xml(root, iso_code, output_dir)

        # Ghi thời gian kết thúc và tính toán thời gian
        end_time = time.time()
        duration = (end_time - start_time) * 1000  # Thời gian tính bằng milliseconds

        # In thông báo hoàn thành
        print_progress_done(language_name, iso_code, duration)

if __name__ == "__main__":
    input_xml_path = "mnt/data/strings.xml"  # Đường dẫn tệp XML gốc
    languages_json_path = "mnt/data/languages.json"  # Đường dẫn tệp JSON chứa ngôn ngữ
    manual_dict_path = "mnt/data/manual_dict.json"  # Đường dẫn tệp JSON chứa manual_dict
    output_dir = "mnt/data/output"  # Thư mục lưu các tệp strings.xml dịch

    process_strings(input_xml_path, languages_json_path, manual_dict_path, output_dir)
    print("Hoàn thành dịch. Kiểm tra các thư mục kết quả.")