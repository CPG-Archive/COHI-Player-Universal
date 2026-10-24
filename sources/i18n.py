####################################################
# COHI-Player-Universal
# V1.0.0, 24.10.2026
# Author: Claus Peter Gallenmiller, cpg-radio@gmx.de
####################################################

import locale
import os

SUPPORTED_LANGUAGES = ("en", "de")
DEFAULT_LANGUAGE = "en"
CONFIG_FILE_NAME = "config_sdr.yaml"

_current_language = DEFAULT_LANGUAGE
_table = {}


def _detect_system_language():
    candidates = [os.environ.get(name) for name in ("LC_ALL", "LC_MESSAGES", "LANG")]
    try:
        candidates.append(locale.getlocale()[0])
    except (ValueError, TypeError):
        pass
    for value in candidates:
        if value and str(value).lower().startswith("de"):
            return "de"
    return "en"


def normalize_language(raw_value):
    if raw_value is None:
        return DEFAULT_LANGUAGE
    value = str(raw_value).strip().lower()
    if value == "auto":
        return _detect_system_language()
    if value in SUPPORTED_LANGUAGES:
        return value
    return DEFAULT_LANGUAGE


def set_language(language):
    global _current_language, _table
    language = normalize_language(language)
    _current_language = language
    if language == "de":
        from lang_de import TEXTS
        _table = TEXTS
    else:
        _table = {}
    return language


def get_language():
    return _current_language


def tr(text):
    return _table.get(text, text)


def read_language_from_config(config_path=None):
    paths = [config_path] if config_path else [
        CONFIG_FILE_NAME,
        os.path.join(os.path.dirname(os.path.abspath(__file__)), CONFIG_FILE_NAME),
    ]
    try:
        import yaml
    except ImportError:
        return DEFAULT_LANGUAGE
    for path in paths:
        try:
            with open(path, "r", encoding="utf-8") as stream:
                raw_config = yaml.safe_load(stream) or {}
        except (OSError, yaml.YAMLError, UnicodeDecodeError):
            continue
        if isinstance(raw_config, dict):
            return normalize_language(raw_config.get("language"))
    return DEFAULT_LANGUAGE


def init_from_config(config_path=None):
    return set_language(read_language_from_config(config_path))


def install_qt_translator(app):
    if _current_language != "de":
        return None
    try:
        from PyQt5.QtCore import QLibraryInfo, QTranslator
    except ImportError:
        return None
    translator = QTranslator(app)
    path = QLibraryInfo.location(QLibraryInfo.TranslationsPath)
    for name in ("qtbase_de", "qt_de"):
        if translator.load(name, path):
            app.installTranslator(translator)
            app._cohi_qt_translator = translator
            return translator
    return None


init_from_config()
