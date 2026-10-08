"""`fanbase site`: a site to browse a registry, made of files; none of its text is trusted."""

import hashlib
import json
import re
from pathlib import Path

import pytest
import yaml
from conftest import build_registry, republish

from fanbase import quality, site
from fanbase.cli import main

EVIL = '<script>alert("x")</script>'
SHA = "a" * 64


def run(capsys, *argv):
    code = main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


@pytest.fixture
def reg(tmp_path):
    root = build_registry(tmp_path / "reg", {
        ("png", "png"): dict(version="1.0", description="Valid PNGs", status="stable", extensions=["png"], license="Apache-2.0",
                             authors=["Grace Hopper", {"name": "Ada Lovelace", "orcid": "0000-0002-1825-0097"}], doi="10.5281/zenodo.111"),
        ("png", "png-apng"): dict(version="1.2", description="Animated", status="stable", extends=["png"], extensions=["png"]),
        ("png", "png-evil"): dict(version="0.1", description=EVIL, status="draft", title=EVIL, license='"><img src=x onerror=alert(1)>',
                                  source="javascript:alert(1)", authors=[EVIL, {"name": "X", "orcid": "0000-0002-1825-009X"}],
                                  derived_from="fanbase:png/png@1.0", extensions=['"><b>']),
        ("gif", "gif"): dict(version="1.0", description="GIFs"),
    })
    (root / "specs/png/format.yml").write_text(yaml.safe_dump({"title": "Portable Network Graphics", "mime": "image/png",
                                                               "reference": "https://www.w3.org/TR/png-3/"}))
    (root / "specs/gif/format.yml").write_text(yaml.safe_dump({"reference": "javascript:alert(2)"}))
    (root / "specs/png/png-apng/png-apng.fan").write_text('include("png/png.fan")\n<start> ::= <png>  # ' + EVIL + "\n", newline="\n")
    republish(root, "png-apng")
    return root


def build(capsys, reg, out, *extra):
    return run(capsys, "--registry", str(reg), "site", str(out), *extra)


def read(out, name):
    return (Path(out) / name).read_text(encoding="utf-8")


def sha_of(reg, kind):
    from fanbase.manager import entry_sha
    from fanbase.registry import Registry

    r = Registry(reg)
    return entry_sha(r, r.resolve(kind))


def results_for(reg, tmp_path):
    report = {"schema": 1, "fanbase": "0.5.0", "fandango": "1.3.0", "seed": 1, "specs": [
        {"spec": "png/png", "version": "1.0", "sha256": sha_of(reg, "png"), "generated": {"produced": 100, "per_second": 15.5}, "decodes": "always",
         "targets": [{"name": "pillow", "status": "ok", "version": "12.3.0", "total": 100, "accepted": 100},
                     {"name": "libpng-cov", "status": "ok", "version": "libpng 1.6.59", "total": 100, "accepted": 100,
                      "coverage": {"lines_total": 8000, "seeds": {"files": 6, "lines": 1300, "lines_share": 0.1625},
                                   "curve": [{"inputs": 1, "lines": 1200, "lines_share": 0.15}, {"inputs": 10, "lines": 1600, "lines_share": 0.2},
                                             {"inputs": 100, "lines": 1800, "lines_share": 0.225}],
                                   "only_generated_lines": 700, "only_seed_lines": 200}}]},
        {"spec": "png/png-apng", "version": "1.2", "sha256": SHA, "generated": {"produced": 50, "per_second": 3.0}, "decodes": None,
         "targets": [{"name": "pillow", "status": "ok", "version": "12.3.0", "total": 50, "accepted": 25}]}]}
    path = tmp_path / "quality.json"
    path.write_text(json.dumps(quality.build([report])))
    return path


# --- what is made

def test_a_site_of_pages(capsys, reg, tmp_path):
    out = tmp_path / "site"
    code, stdout, _ = build(capsys, reg, out)
    assert code == 0 and "7 pages in" in stdout
    names = sorted(p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file())
    assert names == [".fanbase-site", "gif/gif.html", "gif/index.html", "index.html", "index.json", "png/index.html", "png/png-apng.html",
                     "png/png-evil.html", "png/png.html", "site.js", "style.css"]


def test_the_front_page_lists_every_spec_and_can_be_filtered(capsys, reg, tmp_path):
    build(capsys, reg, tmp_path / "s")
    page = read(tmp_path / "s", "index.html")
    for spec in ("png/png", "png/png-apng", "png/png-evil", "gif/gif"):
        assert f'href="{spec.split("/")[0]}/{spec.split("/")[1]}.html"' in page
    assert 'id="q"' in page and "Portable Network Graphics" in page and 'data-format="png"' in page and "4 Fandango input specifications of 2 formats" in page


def test_a_spec_page_says_what_it_is(capsys, reg, tmp_path):
    build(capsys, reg, tmp_path / "s")
    page = read(tmp_path / "s", "png/png.html")
    assert "<h1>png/png</h1>" in page and "Valid PNGs" in page and "Apache-2.0" in page and ">version 1.0<" in page
    assert 'href="https://orcid.org/0000-0002-1825-0097"' in page and "Hopper" in page
    assert 'href="https://doi.org/10.5281/zenodo.111"' in page
    assert "fanbase install png" in page and "fandango fuzz -F png -n 10" in page
    assert "@software{fanbase_png_png_1_0" in page and "doi     = {10.5281/zenodo.111}" in page
    assert 'href="https://github.com/fandango-fuzzer/fanbase/blob/main/specs/png/png/png.fan"' in page


def test_what_extends_what_is_linked_both_ways(capsys, reg, tmp_path):
    build(capsys, reg, tmp_path / "s")
    assert '<dt>Extends</dt><dd><a href="../png/png.html">png</a></dd>' in read(tmp_path / "s", "png/png-apng.html")
    assert '<dt>Extended by</dt><dd><a href="../png/png-apng.html">png/png-apng</a></dd>' in read(tmp_path / "s", "png/png.html")
    assert 'Forked from</dt><dd><a href="../png/png.html">fanbase:png/png@1.0</a>' in read(tmp_path / "s", "png/png-evil.html")


def test_the_source_is_shown(capsys, reg, tmp_path):
    build(capsys, reg, tmp_path / "s")
    page = read(tmp_path / "s", "png/png-apng.html")
    assert "<summary>Source (2 lines)</summary>" in page and 'include(&quot;png/png.fan&quot;)' in page


def test_the_same_registry_gives_the_same_files(capsys, reg, tmp_path):
    build(capsys, reg, tmp_path / "a")
    build(capsys, reg, tmp_path / "b")
    digests = lambda d: {str(p.relative_to(d)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(Path(d).rglob("*")) if p.is_file()}  # noqa: E731
    assert digests(tmp_path / "a") == digests(tmp_path / "b")


def test_index_json_is_for_machines(capsys, reg, tmp_path):
    build(capsys, reg, tmp_path / "s")
    data = json.loads(read(tmp_path / "s", "index.json"))
    rows = {r["spec"]: r for r in data["specs"]}
    assert set(rows) == {"png/png", "png/png-apng", "png/png-evil", "gif/gif"}
    assert rows["png/png"]["page"] == "png/png.html" and rows["png/png"]["doi"] == "10.5281/zenodo.111" and rows["png/png-apng"]["extends"] == ["png"]


# --- every link goes somewhere

def test_every_link_between_pages_leads_to_a_page(capsys, reg, tmp_path):
    out = tmp_path / "s"
    build(capsys, reg, out, "--quality", str(results_for(reg, tmp_path)))
    broken = []
    for page in out.rglob("*.html"):
        for href in re.findall(r'(?:href|src)="([^"]+)"', page.read_text()):
            if href.startswith(("http://", "https://")):
                continue
            if not (page.parent / href).resolve().is_file():
                broken.append((str(page.relative_to(out)), href))
    assert broken == []


# --- nothing in it is trusted

def test_what_a_pull_request_wrote_is_text_and_never_markup(capsys, reg, tmp_path):
    out = tmp_path / "s"
    build(capsys, reg, out)
    for page in out.rglob("*.html"):
        text = page.read_text()
        assert re.findall(r"<script[^>]*>", text) in (['<script src="site.js">'], ['<script src="../site.js">']), page  # the site's own, and no other
        assert "<img" not in text and "<b>" not in text and "onerror=" not in re.sub(r"&quot;|&gt;|&lt;", "", text.replace("onerror=alert(1)", "")), page
    evil = read(out, "png/png-evil.html")
    assert "&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt;" in evil  # what was written, as the text it is
    assert "&quot;&gt;&lt;img src=x onerror=alert(1)&gt;" in evil


def test_only_web_addresses_become_links(capsys, reg, tmp_path):
    out = tmp_path / "s"
    build(capsys, reg, out)
    assert "javascript:" not in " ".join(re.findall(r'href="([^"]*)"', read(out, "png/png-evil.html")))
    assert "javascript:alert(1)" in read(out, "png/png-evil.html")  # (shown as the text it is)
    assert 'href="javascript' not in read(out, "gif/index.html")
    assert 'href="https://www.w3.org/TR/png-3/"' in read(out, "png/index.html")


def test_every_link_to_the_web_does_not_pass_the_page_on(capsys, reg, tmp_path):
    out = tmp_path / "s"
    build(capsys, reg, out)
    for page in out.rglob("*.html"):
        for tag in re.findall(r'<a [^>]*href="https?://[^"]*"[^>]*>', page.read_text()):
            assert 'rel="noopener noreferrer nofollow"' in tag, tag


def test_each_page_takes_nothing_from_anywhere_else(capsys, reg, tmp_path):
    out = tmp_path / "s"
    build(capsys, reg, out, "--quality", str(results_for(reg, tmp_path)))
    for page in out.rglob("*.html"):
        text = page.read_text()
        assert "default-src 'none'" in text and "script-src 'self'" in text and "style-src 'self'" in text
        assert 'style="' not in text and "<style" not in text and "<iframe" not in text and "<link rel=\"stylesheet\" href=\"http" not in text
        assert "http://" not in text, page
    js = read(out, "site.js")
    assert "fetch(" not in js and "XMLHttpRequest" not in js and "eval(" not in js and "innerHTML" not in js


def test_control_characters_are_cleaned(capsys, reg, tmp_path):
    republish(reg, "png-evil", description="red \x1b[31m text \x07 bell")
    out = tmp_path / "s"
    build(capsys, reg, out)
    assert "\x1b" not in read(out, "png/png-evil.html") and "\x07" not in read(out, "index.html")


# --- how well they do

def test_quality_is_shown_where_there_is_some(capsys, reg, tmp_path):
    out = tmp_path / "s"
    code, _, _ = build(capsys, reg, out, "--quality", str(results_for(reg, tmp_path)))
    assert code == 0
    page = read(out, "png/png.html")
    assert "How it does" in page and "accepts 100%, covers 22.5%, 16/s" in page and "<td>pillow</td><td class=\"num\">100%</td><td>12.3.0</td>" in page
    assert "libpng-cov" in page and "700 lines" in page and "as expected" in page
    assert '<svg class="curve"' in page and "<polyline" in page and "22.5% after 100" in page
    assert "accepts 100%" in read(out, "index.html") and (out / "quality.json").is_file()


def test_results_for_an_earlier_version_are_said_to_be(capsys, reg, tmp_path):
    out = tmp_path / "s"
    build(capsys, reg, out, "--quality", str(results_for(reg, tmp_path)))
    page = read(out, "png/png-apng.html")
    assert "for an earlier version of the spec" in page and "accepts 50%" in page


def test_results_that_do_not_cover_a_spec_say_so(capsys, reg, tmp_path):
    out = tmp_path / "s"
    build(capsys, reg, out, "--quality", str(results_for(reg, tmp_path)))
    assert "do not cover this spec yet" in read(out, "gif/gif.html")


def test_without_results_there_is_no_section(capsys, reg, tmp_path):
    out = tmp_path / "s"
    build(capsys, reg, out)
    assert "How it does" not in read(out, "png/png.html") and not (out / "quality.json").exists()


def test_a_curve_picture_needs_two_points():
    assert Site_curve([[1, 0.2]]) == ""
    assert "<polyline" in Site_curve([[1, 0.2], [100, 0.4]], real=0.3) and 'class="real"' in Site_curve([[1, 0.2], [100, 0.4]], real=0.3)


def Site_curve(points, real=None):
    return site.Site.curve_svg(points, real)


# --- the folder it writes to

def test_a_folder_with_other_things_in_it_is_left_alone(capsys, reg, tmp_path):
    out = tmp_path / "mine"
    out.mkdir()
    (out / "precious.txt").write_text("keep")
    code, _, err = build(capsys, reg, out)
    assert code == 2 and "not a site made by fanbase" in err and (out / "precious.txt").read_text() == "keep"


def test_a_site_it_made_before_is_replaced(capsys, reg, tmp_path):
    out = tmp_path / "s"
    build(capsys, reg, out)
    (out / "old-page.html").write_text("stale")
    republish(reg, "gif", description="GIFs, changed")
    assert build(capsys, reg, out)[0] == 0
    assert not (out / "old-page.html").exists() and "GIFs, changed" in read(out, "gif/gif.html")


def test_a_file_is_not_a_folder(capsys, reg, tmp_path):
    (tmp_path / "file").write_text("x")
    assert build(capsys, reg, tmp_path / "file")[0] == 2


def test_a_registry_of_its_own_is_not_presented_as_the_public_one(capsys, tmp_path):
    root = build_registry(tmp_path / "acme", {("png", "png-top"): dict(version="1.0", description="Top")}, name="acme")
    out = tmp_path / "s"
    code, _, _ = build(capsys, root, out, "--title", "Acme specs")
    page = read(out, "png/png-top.html")
    assert code == 0 and "Acme specs" in read(out, "index.html") and "fanbase install acme:png-top" in page
    assert "github.com/fandango-fuzzer" not in page  # no repository to link to, and none made up
    assert build(capsys, root, tmp_path / "t", "--repo", "javascript:alert(1)")[0] == 2
    assert build(capsys, root, tmp_path / "u", "--repo", "https://example.org/acme/specs")[0] == 0
    assert 'href="https://example.org/acme/specs/blob/main/specs/png/png-top/png-top.fan"' in read(tmp_path / "u", "png/png-top.html")
