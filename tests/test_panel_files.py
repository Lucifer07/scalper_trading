"""FileRegistry: sensor rahasia .env, backup, konflik mtime, izin file."""
import os
import sys

import pytest

sys.path.insert(0, "/home/jayz/xau-monitor")

from xau_monitor.panel.files import (  # noqa: E402
    KEEP, FileConflict, FileRegistry, FileTooLarge, UnknownFile,
)

ENV = ("SYMBOL=XAUUSD\n"
       "TELEGRAM_BOT_TOKEN=supersecret123\n"
       "ADVISOR_API_KEY=sk-live-abc\n"
       "# komentar tetap\n")


@pytest.fixture
def repo(tmp_path):
    (tmp_path / ".env").write_text(ENV, encoding="utf-8")
    (tmp_path / ".env.example").write_text("SYMBOL=XAUUSD\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("# xau\n", encoding="utf-8")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "gui.md").write_text("# gui\n", encoding="utf-8")
    rules = tmp_path.parent / "rules-xau.md"
    rules.write_text("# aturan\n", encoding="utf-8")
    return tmp_path, rules


def test_secrets_masked_and_restored(repo):
    root, rules = repo
    reg = FileRegistry(root, str(rules))
    read = reg.read("env")
    assert "supersecret123" not in read["content"]
    assert "sk-live-abc" not in read["content"]
    assert read["content"].count(KEEP) == 2
    assert "SYMBOL=XAUUSD" in read["content"]

    # edit baris non-rahasia; baris sensor harus utuh kembali
    edited = read["content"].replace("SYMBOL=XAUUSD", "SYMBOL=GOLD")
    reg.write("env", edited, mtime_ns=read["mtime_ns"])
    text = (root / ".env").read_text(encoding="utf-8")
    assert "SYMBOL=GOLD" in text
    assert "TELEGRAM_BOT_TOKEN=supersecret123" in text
    assert "ADVISOR_API_KEY=sk-live-abc" in text
    assert KEEP not in text


def test_explicit_secret_rotation_is_kept(repo):
    root, rules = repo
    reg = FileRegistry(root, str(rules))
    content = reg.read("env")["content"]
    # nilai sensor diganti user secara eksplisit -> tidak dipulihkan
    content = content.replace(f"TELEGRAM_BOT_TOKEN={KEEP}", "TELEGRAM_BOT_TOKEN=newtoken")
    reg.write("env", content)
    assert "TELEGRAM_BOT_TOKEN=newtoken" in (root / ".env").read_text(encoding="utf-8")


def test_env_written_with_restrictive_mode(repo):
    root, rules = repo
    reg = FileRegistry(root, str(rules))
    reg.write("env", reg.read("env")["content"])
    mode = os.stat(root / ".env").st_mode & 0o777
    assert mode == 0o600


def test_conflict_detected_and_forced(repo):
    root, rules = repo
    reg = FileRegistry(root, str(rules))
    read = reg.read("readme")
    (root / "README.md").write_text("# diubah di luar\n", encoding="utf-8")
    with pytest.raises(FileConflict):
        reg.write("readme", "# panel\n", mtime_ns=read["mtime_ns"])
    result = reg.write("readme", "# panel\n", mtime_ns=read["mtime_ns"], force=True)
    assert result["ok"] is True
    assert "# panel" in (root / "README.md").read_text(encoding="utf-8")


def test_backup_created_on_change(repo):
    root, rules = repo
    reg = FileRegistry(root, str(rules))
    read = reg.read("readme")
    result = reg.write("readme", "# baru\n", mtime_ns=read["mtime_ns"])
    assert result["backup"].startswith(".backups/")
    assert (root / result["backup"]).exists()


def test_no_backup_when_content_identical(repo):
    root, rules = repo
    reg = FileRegistry(root, str(rules))
    content = reg.read("readme")["content"]
    assert reg.write("readme", content)["backup"] == ""


def test_registry_lists_rules_and_docs(repo):
    root, rules = repo
    ids = {item["id"] for item in FileRegistry(root, str(rules)).list()}
    assert {"env", "env_example", "readme", "rules", "md:docs/gui.md"} <= ids


def test_unknown_file_id_rejected(repo):
    root, rules = repo
    reg = FileRegistry(root, str(rules))
    with pytest.raises(UnknownFile):
        reg.read("../../etc/passwd")
    with pytest.raises(UnknownFile):
        reg.write("docs/../secret.md", "x")


def test_oversize_content_rejected(repo):
    root, rules = repo
    reg = FileRegistry(root, str(rules))
    with pytest.raises(FileTooLarge):
        reg.write("readme", "x" * (600 * 1024))
