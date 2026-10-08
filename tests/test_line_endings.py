"""Bytes are bytes: a spec is hashed and signed as it is, so nothing here may change a line ending."""

from conftest import build_registry, republish

from fanbase.cli import main
from fanbase.manager import installed_copy


def run(capsys, *argv):
    code = main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


def test_diff_of_a_spec_with_windows_line_endings_is_empty(capsys, tmp_path, monkeypatch):
    # A registry may hold a spec whose lines end with CRLF. The installed copy is the same bytes, and diff must say so;
    # reading one side as text would turn its CRLF into LF and show every line as changed.
    monkeypatch.setenv("FANDANGO_PATH", str(tmp_path / "fandango"))
    reg = build_registry(tmp_path / "reg", {("png", "png"): dict(version="1.0", description="Valid PNGs")})
    (reg / "specs/png/png/png.fan").write_bytes(b"<start> ::= 'a'\r\n<b> ::= 'c'\r\n")
    republish(reg, "png")
    assert run(capsys, "--registry", str(reg), "install", "png")[0] == 0
    assert installed_copy("png/png").path.read_bytes() == (reg / "specs/png/png/png.fan").read_bytes()
    code, out, _ = run(capsys, "--registry", str(reg), "diff", "png")
    assert code == 0 and out == ""


def test_the_files_of_a_registry_are_written_with_unix_line_endings(capsys, tmp_path):
    # index.yml is what a maintainer signs; metadata.yml and a new spec are committed. On Windows a file written in text
    # mode would get CRLF, which changes the bytes (and every hash and signature of them).
    reg = build_registry(tmp_path / "reg", {("png", "png"): dict(version="1.0", description="Valid PNGs", extensions=["png"])})
    assert run(capsys, "--registry", str(reg), "new", "png-fancy", "--extends", "png", "--description", "Fancy")[0] == 0
    assert run(capsys, "--registry", str(reg), "reindex")[0] == 0
    for name in ("index.yml", "specs/png/png-fancy/png-fancy.fan", "specs/png/png-fancy/metadata.yml", "specs/png/png/metadata.yml"):
        assert b"\r" not in (reg / name).read_bytes(), name
