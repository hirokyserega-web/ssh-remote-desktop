"""Tests for the runtime i18n layer (RU/EN) and DictTranslator."""
from __future__ import annotations

import importlib



def _reload_i18n():
    # Each test wants a clean module state (the active language is global).
    import client.i18n as mod
    return importlib.reload(mod)


def test_supported_langs():
    mod = _reload_i18n()
    assert "ru" in mod.SUPPORTED_LANGS
    assert "en" in mod.SUPPORTED_LANGS


def test_en_table_has_known_entries():
    mod = _reload_i18n()
    table = mod.TRANSLATIONS["en"]
    assert table["Подключиться"] == "Connect"
    assert table["Файлы"] == "Files"
    # every EN value must be a non-empty string
    for k, v in table.items():
        assert isinstance(k, str) and k
        assert isinstance(v, str) and v


def test_tr_returns_source_for_ru():
    mod = _reload_i18n()
    mod._ACTIVE_LANG = "ru"
    assert mod.tr("Подключиться") == "Подключиться"


def test_tr_translates_for_en():
    mod = _reload_i18n()
    mod._ACTIVE_LANG = "en"
    assert mod.tr("Подключиться") == "Connect"
    # unknown string falls back to itself
    assert mod.tr("definitely not in table") == "definitely not in table"


def test_dict_translator_ru_returns_source():
    mod = _reload_i18n()
    t = mod.DictTranslator("ru")
    assert t.translate("Ctx", "Подключиться") == "Подключиться"


def test_dict_translator_en_translates():
    mod = _reload_i18n()
    t = mod.DictTranslator("en")
    assert t.translate("Ctx", "Подключиться") == "Connect"


def test_set_language_installs_translator(qapp):
    mod = _reload_i18n()
    mod.set_language(qapp, "en")
    assert mod.active_language() == "en"
    mod.set_language(qapp, "ru")
    assert mod.active_language() == "ru"
    # unknown lang falls back to ru
    mod.set_language(qapp, "xx")
    assert mod.active_language() == "ru"
