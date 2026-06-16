"""File jail: must confine every path under the configured root."""

from pathlib import Path

import pytest

from server.files import FileJail, JailError


def test_listdir(tmp_path: Path):
    (tmp_path / "a.txt").write_text("hi")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.txt").write_text("ok")
    j = FileJail(tmp_path)
    entries = {e["name"]: e for e in j.listdir("")}
    assert entries["a.txt"]["is_dir"] is False
    assert entries["sub"]["is_dir"] is True
    assert "b.txt" in {e["name"] for e in j.listdir("sub")}


def test_blocks_traversal_outside_root(tmp_path: Path):
    j = FileJail(tmp_path)
    with pytest.raises(JailError):
        j.listdir("../etc")
    with pytest.raises(JailError):
        j.resolve("/etc/passwd")
    with pytest.raises(JailError):
        j.mkdir("../escape")


def test_mkdir_and_remove(tmp_path: Path):
    j = FileJail(tmp_path)
    j.mkdir("newdir")
    assert (tmp_path / "newdir").is_dir()
    j.remove("newdir")
    assert not (tmp_path / "newdir").exists()


def test_remove_root_refused(tmp_path: Path):
    j = FileJail(tmp_path)
    with pytest.raises(JailError):
        j.remove("")
