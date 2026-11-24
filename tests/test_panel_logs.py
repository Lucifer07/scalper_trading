"""LogTail: tail awal, polling inkremental, rotasi/truncate, baris parsial."""
import sys

sys.path.insert(0, "/home/jayz/xau-monitor")

from xau_monitor.panel.logs import LogTail  # noqa: E402


def test_missing_file_reports_missing(tmp_path):
    data = LogTail(tmp_path / "nope.log").read()
    assert data["missing"] is True
    assert data["lines"] == []


def test_initial_tail_then_incremental(tmp_path):
    path = tmp_path / "app.log"
    path.write_text("a\nb\nc\n", encoding="utf-8")
    tail = LogTail(path)

    first = tail.read(None, limit=10)
    assert first["lines"] == ["a", "b", "c"]
    assert first["cursor"] == 6
    assert first["reset"] is True

    with path.open("a", encoding="utf-8") as fh:
        fh.write("d\ne\n")
    second = tail.read(first["cursor"], limit=10)
    assert second["lines"] == ["d", "e"]
    assert second["reset"] is False
    assert second["cursor"] == path.stat().st_size


def test_partial_line_is_not_emitted_twice(tmp_path):
    path = tmp_path / "app.log"
    path.write_text("abc", encoding="utf-8")          # belum ada newline
    tail = LogTail(path)
    assert tail.read(None)["lines"] == []
    with path.open("a", encoding="utf-8") as fh:
        fh.write("def\n")
    data = tail.read(0)
    assert data["lines"] == ["abcdef"]


def test_truncate_or_rotation_resets_cursor(tmp_path):
    path = tmp_path / "app.log"
    path.write_text("satu\ndua\n", encoding="utf-8")
    tail = LogTail(path)
    old = tail.read(None)["cursor"]
    path.write_text("baru\n", encoding="utf-8")       # rotasi: file lebih kecil
    data = tail.read(old)
    assert data["reset"] is True
    assert data["lines"] == ["baru"]


def test_limit_applies_to_tail(tmp_path):
    path = tmp_path / "app.log"
    path.write_text("".join(f"line{i}\n" for i in range(50)), encoding="utf-8")
    data = LogTail(path).read(None, limit=5)
    assert data["lines"] == [f"line{i}" for i in range(45, 50)]
