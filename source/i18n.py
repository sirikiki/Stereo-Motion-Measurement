"""Exact bilingual UI catalog. Measurement field names and values are never translated."""
import json
import sys
from pathlib import Path
_language = 'en'
_catalog = json.loads((Path(__file__).parent / 'translations.json').read_text(encoding='utf-8'))
def set_language(language):
    global _language
    _language = language if language in ('en','zh_CN') else 'en'
def tr(message):
    return _catalog.get(message,message) if _language == 'en' else message
