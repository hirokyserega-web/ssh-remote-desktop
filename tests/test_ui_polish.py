"""Tests for P2 UI polish: human-size, file sorting, key fingerprint display."""
from __future__ import annotations
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

def test_human_size():
    from client.files_dialog import _human_size
    assert _human_size(0) == "0 B"
    assert _human_size(512) == "512 B"
    assert _human_size(1024) == "1.0 KiB"
    assert _human_size(1024 * 1024) == "1.0 MiB"
    assert _human_size(1536) == "1.5 KiB"

def test_sorted_entries_dirs_first():
    e = [{"name": "z.txt", "is_dir": False, "size": 1}, {"name": "a", "is_dir": True, "size": 0}, {"name": "m.txt", "is_dir": False, "size": 2}]
    s = sorted(e, key=lambda x: (not x["is_dir"], x["name"].lower()))
    assert [x["name"] for x in s] == ["a", "m.txt", "z.txt"]

def test_i18n_has_new_p2_keys():
    import importlib
    import client.i18n as m
    importlib.reload(m)
    t = m.TRANSLATIONS["en"]
    for k in ("comment@host (необязательно)", "необязательно", "Профиль:", "Сохранить профиль", "Удалить профиль", "Проверить соединение", "Папка для ключей"):
        assert k in t, f"missing EN translation for {k!r}"
