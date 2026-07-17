import os
import json
import unicodedata
from config import HISTORY_FILE


def remove_accents(input_str):
    if not input_str:
        return ""
    nfkd_form = unicodedata.normalize('NFKD', str(input_str))
    return "".join([c for c in nfkd_form if not unicodedata.combining(c)])


def normalize_name(name: str) -> str:
    if not name:
        return ""
    return " ".join(remove_accents(name).split()).strip().rstrip(".").lower()


def read_download_history():
    if not os.path.exists(HISTORY_FILE):
        return []
    try:
        with open(HISTORY_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return []


def write_download_history(exam_history_id):
    history = read_download_history()
    if exam_history_id not in history:
        history.append(exam_history_id)
        with open(HISTORY_FILE, 'w', encoding='utf-8') as f:
            json.dump(history, f, indent=4, ensure_ascii=False)