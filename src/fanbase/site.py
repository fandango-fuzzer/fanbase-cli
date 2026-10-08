"""`fanbase site OUTDIR`: a site to browse a registry, made of plain files.

One page for the registry (every spec, with a filter), one for each format, one for each spec: what it is and who
made it, what it extends and what extends it, how to use it, how well it does (from quality.json: how much of its
files each parser accepts, how much of a library they reach, how fast they are made), how to cite it, and its source.

The metadata of a spec comes from pull requests, so none of it is trusted: every text is cleaned of control
characters and escaped, a link is only made to an http(s) address, and each page says in a Content-Security-Policy
that it takes nothing from anywhere else (the style and the filter are two files of the site itself). Nothing is
run: a spec is read, never executed. The same registry and results give the same files, byte for byte (nothing
is dated), so that the site can be rebuilt and compared.
"""

from __future__ import annotations

import html
import json
import math
import re
import shutil
from pathlib import Path

from fanbase import __version__, quality as quality_results
from fanbase.context import Context
from fanbase.contrib import checkout
from fanbase.deps import dependencies, self_names
from fanbase.discover import _author_names, _bib, bib_author
from fanbase.manager import entry_sha
from fanbase.output import clean
from fanbase.registry import DEFAULT_REGISTRY_NAME, Entry, Registry, RegistryError, split_prefix

MARKER = ".fanbase-site"
SOURCE_LIMIT = 200_000  # characters of a spec shown on its page
DEFAULT_REPO = "https://github.com/fandango-fuzzer/fanbase"
_ORCID = re.compile(r"\d{4}-\d{4}-\d{4}-\d{3}[\dX]")
_CSP = "default-src 'none'; style-src 'self'; script-src 'self'; img-src 'self' data:; base-uri 'none'; form-action 'none'"

STYLE = """
:root { --bg: #fff; --fg: #1b1f24; --muted: #59636e; --line: #d0d7de; --accent: #0550ae; --soft: #f6f8fa; --ok: #1a7f37; --bad: #cf222e; }
@media (prefers-color-scheme: dark) { :root { --bg: #0d1117; --fg: #e6edf3; --muted: #9198a1; --line: #30363d; --accent: #58a6ff; --soft: #161b22; --ok: #3fb950; --bad: #f85149; } }
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--fg); font: 16px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif; }
header, main, footer { max-width: 62rem; margin: 0 auto; padding: 0 1rem; }
header { padding-top: 1rem; padding-bottom: .5rem; border-bottom: 1px solid var(--line); }
header a.brand { font-weight: 700; font-size: 1.25rem; color: var(--fg); text-decoration: none; }
nav { color: var(--muted); font-size: .9rem; }
a { color: var(--accent); }
h1 { font-size: 1.6rem; margin: 1rem 0 .25rem; } h2 { font-size: 1.2rem; margin: 1.5rem 0 .5rem; }
.muted { color: var(--muted); } .small { font-size: .85rem; }
table { border-collapse: collapse; width: 100%; font-size: .93rem; }
th, td { text-align: left; padding: .35rem .6rem; border-bottom: 1px solid var(--line); vertical-align: top; }
th { color: var(--muted); font-weight: 600; } td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; }
.table-wrap { overflow-x: auto; }
code, pre { font: .88rem/1.45 ui-monospace, SFMono-Regular, Menlo, monospace; } pre { background: var(--soft); padding: .75rem; overflow-x: auto; border-radius: 6px; }
.badge { display: inline-block; padding: 0 .5rem; border: 1px solid var(--line); border-radius: 1rem; font-size: .8rem; color: var(--muted); margin-right: .25rem; }
.badge.draft { color: var(--bad); } .badge.stable { color: var(--ok); }
.filter { display: flex; gap: .5rem; flex-wrap: wrap; margin: 1rem 0; } .filter input, .filter select { font: inherit; padding: .35rem .5rem; background: var(--bg); color: var(--fg); border: 1px solid var(--line); border-radius: 6px; }
.filter input { flex: 1 1 14rem; }
dl.facts { display: grid; grid-template-columns: max-content 1fr; gap: .25rem 1rem; } dl.facts dt { color: var(--muted); } dl.facts dd { margin: 0; }
.good { color: var(--ok); } .bad { color: var(--bad); }
svg.curve { width: 14rem; height: 4.5rem; } svg.curve .axis { stroke: var(--line); } svg.curve .line { stroke: var(--accent); fill: none; stroke-width: 2; } svg.curve .real { stroke: var(--muted); stroke-dasharray: 3 3; }
footer { color: var(--muted); font-size: .85rem; padding-top: 1.5rem; padding-bottom: 2rem; }
"""

SCRIPT = """
(function () {
  var rows = document.querySelectorAll("tr[data-search]");
  var box = document.getElementById("q");
  var format = document.getElementById("format");
  var status = document.getElementById("status");
  var count = document.getElementById("count");
  if (!box || !rows.length) return;
  function apply() {
    var words = box.value.toLowerCase().split(/\\s+/).filter(Boolean);
    var shown = 0;
    rows.forEach(function (row) {
      var text = row.getAttribute("data-search");
      var ok = words.every(function (w) { return text.indexOf(w) !== -1; });
      if (ok && format && format.value && row.getAttribute("data-format") !== format.value) ok = false;
      if (ok && status && status.value && row.getAttribute("data-status") !== status.value) ok = false;
      row.hidden = !ok;
      if (ok) shown++;
    });
    if (count) count.textContent = shown + " of " + rows.length;
  }
  [box, format, status].forEach(function (el) { if (el) el.addEventListener("input", apply); });
  apply();
})();
"""


def esc(value: object) -> str:
    return html.escape(clean(str(value)), quote=True)


def web_link(url: object, label: object | None = None) -> str:
    """A link to an http(s) address, or just the text: what a pull request wrote is never a link to anything else."""
    text = clean(str(url)).strip()
    if re.fullmatch(r"https?://[^\s\"'<>`\\]+", text):
        return f'<a href="{esc(text)}" rel="noopener noreferrer nofollow">{esc(label if label is not None else text)}</a>'
    return esc(label if label is not None else text)


def percent(share: object, digits: int = 0) -> str:
    return "" if not isinstance(share, (int, float)) else f"{share * 100:.{digits}f}%"


class Site:
    def __init__(self, reg: Registry, results: dict | None, repo: str | None, title: str) -> None:
        self.reg, self.results, self.repo, self.title = reg, results, repo, title
        self.owner = reg.info.get("name", "")
        self.entries = [e for fmt in reg.formats() for e in reg.kinds(fmt)]
        self.by_key = {str(e): e for e in self.entries}
        self.extends: dict[str, list[tuple[str, Entry | None]]] = {}
        self.extended_by: dict[str, list[Entry]] = {str(e): [] for e in self.entries}
        for e in self.entries:
            found: list[tuple[str, Entry | None]] = []
            try:
                deps = dependencies(e, self.owner, self_names(reg) | {self.owner or DEFAULT_REGISTRY_NAME})
            except RegistryError:
                deps = []
            for dep in deps:
                target = None
                if dep.registry == self.owner:
                    try:
                        target = reg.resolve(dep.ref)
                    except RegistryError:
                        target = None
                found.append((dep.text, target))
                if target is not None and str(target) in self.extended_by:
                    self.extended_by[str(target)].append(e)
            self.extends[str(e)] = found

    # --- pieces

    def quality_of(self, e: Entry) -> tuple[dict, bool] | None:
        return quality_results.of(self.results, e, entry_sha(self.reg, e)) if self.results else None

    def page(self, title: str, body: str, up: str, description: str = "") -> str:
        return (
            "<!doctype html>\n<html lang=\"en\">\n<head>\n<meta charset=\"utf-8\">\n"
            f"<meta http-equiv=\"Content-Security-Policy\" content=\"{_CSP}\">\n"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
            "<meta name=\"referrer\" content=\"no-referrer\">\n"
            f"<title>{esc(title)}</title>\n"
            + (f"<meta name=\"description\" content=\"{esc(description)[:300]}\">\n" if description else "")
            + f"<link rel=\"stylesheet\" href=\"{up}style.css\">\n</head>\n<body>\n"
            f"<header><a class=\"brand\" href=\"{up}index.html\">{esc(self.title)}</a></header>\n"
            f"<main>\n{body}\n</main>\n"
            f"<footer>Made by fanbase {esc(__version__)} from the registry"
            + (f"; quality results of {esc(self.results['made'])}" if self.results and self.results.get("made") else "")
            + f".</footer>\n<script src=\"{up}site.js\"></script>\n</body>\n</html>\n"
        )

    def spec_href(self, e: Entry, up: str = "") -> str:
        return f"{up}{e.format}/{e.kind}.html"

    def status_badge(self, e: Entry) -> str:
        status = e.meta.get("status")
        if not isinstance(status, str):
            return ""
        return f'<span class="badge {esc(status) if status in ("draft", "stable", "deprecated") else ""}">{esc(status)}</span>'

    def badges(self, e: Entry) -> str:
        version = f'<span class="badge">version {esc(e.meta["version"])}</span>' if e.meta.get("version") is not None else ""
        return self.status_badge(e) + version

    def quality_cell(self, e: Entry) -> str:
        found = self.quality_of(e)
        if not found:
            return ""
        row, current = found
        text = quality_results.summary(row, current)
        return f'<span class="{"" if current else "muted"}">{esc(text)}</span>'

    def spec_rows(self, entries: list[Entry], up: str) -> str:
        rows = []
        for e in entries:
            meta = e.meta
            status = meta.get("status") if isinstance(meta.get("status"), str) else ""
            search = " ".join(str(x) for x in (e, meta.get("description"), meta.get("title"), " ".join(map(str, meta.get("extensions") or [])),
                                               " ".join(_author_names(meta)), status)).lower()
            rows.append(
                f'<tr data-search="{esc(search)}" data-format="{esc(e.format)}" data-status="{esc(status)}">'
                f'<td><a href="{self.spec_href(e, up)}">{esc(e)}</a></td>'
                f'<td>{esc(meta.get("description") or "")}</td>'
                f'<td>{esc(meta.get("version") if meta.get("version") is not None else "")}</td>'
                f"<td>{self.status_badge(e)}</td>"
                f"<td>{self.quality_cell(e)}</td></tr>")
        return "\n".join(rows)

    def table(self, entries: list[Entry], up: str) -> str:
        return ('<div class="table-wrap"><table>\n<thead><tr><th>Spec</th><th>What it is</th><th>Version</th><th>Status</th>'
                '<th>How it does</th></tr></thead>\n<tbody>\n' + self.spec_rows(entries, up) + "\n</tbody></table></div>")

    def source_url(self, e: Entry) -> str | None:
        return f"{self.repo}/blob/main/{e.path}" if self.repo else None

    # --- pages

    def index_page(self) -> str:
        formats = sorted({e.format for e in self.entries})
        options = "".join(f'<option value="{esc(f)}">{esc(f)}</option>' for f in formats)
        statuses = sorted({e.meta["status"] for e in self.entries if isinstance(e.meta.get("status"), str)})
        status_options = "".join(f'<option value="{esc(s)}">{esc(s)}</option>' for s in statuses)
        listing = "".join(
            f'<li><a href="{esc(f)}/index.html">{esc(self.reg.format_info(f).get("title") or f)}</a> '
            f'<span class="muted">({esc(f)}, {len(self.reg.kinds(f))} spec{"" if len(self.reg.kinds(f)) == 1 else "s"})</span></li>'
            for f in formats)
        body = (
            f"<h1>{esc(self.title)}</h1>\n"
            f"<p class=\"muted\">{len(self.entries)} Fandango input specifications of {len(formats)} formats: grammars that make files of a format, "
            "to share, install, combine and improve. <a href=\"index.json\">index.json</a> has the same for machines.</p>\n"
            f"<h2>Formats</h2>\n<ul>{listing}</ul>\n<h2>All specs</h2>\n"
            "<div class=\"filter\"><input id=\"q\" type=\"search\" placeholder=\"Filter: name, description, extension, author\" aria-label=\"Filter the specs\">"
            f"<select id=\"format\" aria-label=\"Format\"><option value=\"\">every format</option>{options}</select>"
            f"<select id=\"status\" aria-label=\"Status\"><option value=\"\">any status</option>{status_options}</select>"
            "<span id=\"count\" class=\"muted small\"></span></div>\n" + self.table(self.entries, ""))
        return self.page(self.title, body, "", f"Fandango input specifications: {len(self.entries)} specs")

    def format_page(self, fmt: str) -> str:
        info = self.reg.format_info(fmt)
        entries = self.reg.kinds(fmt)
        facts = []
        for label, key in (("Media type", "mime"), ("Extensions", "extensions")):
            value = info.get(key)
            if value:
                facts.append(f"<dt>{label}</dt><dd>{esc(', '.join(map(str, value)) if isinstance(value, list) else value)}</dd>")
        if info.get("reference"):
            facts.append(f"<dt>Specification</dt><dd>{web_link(info['reference'])}</dd>")
        if info.get("targets"):
            facts.append(f"<dt>Judged by</dt><dd>{esc(', '.join(map(str, info['targets'])))}</dd>")
        body = (f"<h1>{esc(info.get('title') or fmt)}</h1>\n<p class=\"muted\"><code>{esc(fmt)}</code>: {len(entries)} specs</p>\n"
                + (f'<dl class="facts">{"".join(facts)}</dl>\n' if facts else "")
                + "<h2>Specs</h2>\n" + self.table(entries, "../"))
        return self.page(f"{info.get('title') or fmt} · {self.title}", body, "../")

    def links(self, entries: list[Entry], up: str) -> str:
        return ", ".join(f'<a href="{self.spec_href(x, up)}">{esc(x)}</a>' for x in entries)

    @staticmethod
    def curve_svg(points: list[list], real: float | None) -> str:
        """The share of a library's lines reached after 1, 10, 100... inputs, as a small picture."""
        if len(points) < 2:
            return ""
        width, height, left, top = 224, 72, 6, 6
        top_share = max([p[1] for p in points] + ([real] if real else []) + [0.01]) * 1.1
        low, high = math.log10(max(points[0][0], 1)), math.log10(max(points[-1][0], 2))

        def xy(n: float, share: float) -> tuple[float, float]:
            x = left + (width - 2 * left) * ((math.log10(max(n, 1)) - low) / (high - low or 1))
            return round(x, 1), round(top + (height - 2 * top) * (1 - share / top_share), 1)

        line = " ".join(f"{x},{y}" for x, y in (xy(n, s) for n, s in points))
        extra = ""
        if real:
            y = xy(1, real)[1]
            extra = f'<line class="real" x1="{left}" x2="{width - left}" y1="{y}" y2="{y}"><title>real files: {percent(real, 1)}</title></line>'
        alt = f"{percent(points[0][1], 1)} after {points[0][0]} inputs, {percent(points[-1][1], 1)} after {points[-1][0]}"
        return (f'<svg class="curve" viewBox="0 0 {width} {height}" role="img" aria-label="{esc(alt)}"><title>{esc(alt)}</title>'
                f'<line class="axis" x1="{left}" x2="{width - left}" y1="{height - top}" y2="{height - top}"/>{extra}'
                f'<polyline class="line" points="{line}"/></svg>')

    def quality_section(self, e: Entry) -> str:
        found = self.quality_of(e)
        if self.results is None:
            return ""
        if not found:
            return "<h2>How it does</h2>\n<p class=\"muted\">The quality results do not cover this spec yet.</p>"
        row, current = found
        out = ["<h2>How it does</h2>"]
        if not current:
            out.append('<p class="muted">These results are for an earlier version of the spec.</p>')
        out.append(f'<p>{esc(quality_results.summary(row))}. {esc(row["count"]) if row.get("count") else ""} inputs'
                   + (f", seed {esc(row['seed'])}" if row.get("seed") is not None else "") + ".</p>")
        if row["targets"]:
            out.append('<div class="table-wrap"><table><thead><tr><th>Parser</th><th class="num">Accepts</th><th>Version</th></tr></thead><tbody>'
                       + "".join(f'<tr><td>{esc(n)}</td><td class="num">{percent(t.get("accepted"))}</td><td>{esc(t.get("version") or "")}</td></tr>'
                                 for n, t in sorted(row["targets"].items())) + "</tbody></table></div>")
        if row.get("decodes") and row.get("expectation_met") is not None:
            ok = row["expectation_met"]
            out.append(f'<p class="{"good" if ok else "bad"}">The spec says its files decode {esc(row["decodes"])}: '
                       f'{"as expected" if ok else "not as expected"}.</p>')
        for name, cov in sorted(row["coverage"].items()):
            out.append(f"<h3>{esc(name)}</h3>")
            facts = [("lines reached", percent(cov.get("lines"), 1) + (f" of {cov['lines_total']}" if cov.get("lines_total") else "")),
                     ("real files reach", percent(cov.get("seeds_lines"), 1) or "-"),
                     ("only the generated files", f"{cov['only_generated']} lines" if cov.get("only_generated") is not None else "-"),
                     ("only the real files", f"{cov['only_seeds']} lines" if cov.get("only_seeds") is not None else "-")]
            out.append('<dl class="facts">' + "".join(f"<dt>{k}</dt><dd>{esc(v)}</dd>" for k, v in facts) + "</dl>")
            out.append(self.curve_svg(cov.get("curve") or [], cov.get("seeds_lines")))
        return "\n".join(out)

    def citation_section(self, e: Entry) -> str:
        meta = e.meta
        authors = _author_names(meta) or ["Fanbase contributors"]
        doi = meta.get("doi") if isinstance(meta.get("doi"), str) else None
        url = f"https://doi.org/{doi}" if doi else (self.repo or "")
        key = re.sub(r"[^A-Za-z0-9]+", "_", f"fanbase_{e}_{meta.get('version') or ''}").strip("_")
        fields = [("author", " and ".join(bib_author(a) for a in authors)),
                  ("title", f"{_bib(meta.get('title') or e.kind)} ({_bib(e)}), a Fandango input specification"),
                  ("version", _bib(meta["version"]) if meta.get("version") is not None else None),
                  ("doi", doi), ("url", url or None), ("note", f"Fanbase registry, {_bib(e.path)}, sha256 {entry_sha(self.reg, e)[:12]}")]
        bib = f"@software{{{key},\n" + "".join(f"  {n:<8}= {{{v}}},\n" for n, v in fields if v) + "}"
        return f"<h2>Cite it</h2>\n<pre>{esc(bib)}</pre>"

    def spec_page(self, e: Entry) -> str:
        meta = e.meta
        facts: list[str] = []

        def row(label: str, value: str) -> None:
            if value:
                facts.append(f"<dt>{label}</dt><dd>{value}</dd>")

        row("Format", f'<a href="index.html">{esc(self.reg.format_info(e.format).get("title") or e.format)}</a> <span class="muted">({esc(e.format)})</span>')
        authors = []
        for author in meta.get("authors") or []:
            name = author.get("name") if isinstance(author, dict) else author
            if isinstance(name, str) and name.strip():
                orcid = author.get("orcid") if isinstance(author, dict) else None
                authors.append(esc(name) + (f' <a href="https://orcid.org/{esc(orcid)}" rel="noopener noreferrer nofollow">ORCID</a>'
                                           if isinstance(orcid, str) and _ORCID.fullmatch(orcid) else ""))
        row("Authors", ", ".join(authors))
        row("License", esc(meta.get("license") or ""))
        row("Fandango", esc(meta.get("fandango") or ""))
        row("Extensions", esc(", ".join(map(str, meta.get("extensions") or []))))
        row("Media type", esc(meta.get("mime") or ""))
        row("Needs", esc(", ".join(map(str, (meta.get("pip") or meta.get("requires") or [])))))
        if meta.get("reference"):
            row("Specification", web_link(meta["reference"]))
        if meta.get("source"):
            row("Made from", web_link(meta["source"]))
        if meta.get("derived_from"):
            row("Forked from", self.derived(str(meta["derived_from"])))
        extends = self.extends.get(str(e)) or []
        row("Extends", ", ".join(f'<a href="{self.spec_href(t, "../")}">{esc(text)}</a>' if t else esc(text) for text, t in extends))
        row("Extended by", self.links(self.extended_by.get(str(e)) or [], "../") if self.extended_by.get(str(e)) else "")
        if isinstance(meta.get("doi"), str):
            row("DOI", web_link(f"https://doi.org/{meta['doi']}", meta["doi"]))
        if meta.get("decodes"):
            row("Its files should decode", esc(meta["decodes"]))
        link = self.source_url(e)
        if link:
            row("In the registry", web_link(link, e.path))

        name = f"{self.owner}:{e.kind}" if self.owner else e.kind
        use = (f"fanbase registry add {self.repo}\n" if self.owner and self.repo else "") + \
              f"fanbase install {name}\nfandango fuzz -F {name} -n 10"
        text = self.reg.read(e).decode("utf-8", errors="replace")
        shown = text[:SOURCE_LIMIT]
        source = (f'<details><summary>Source ({len(text.splitlines())} lines)</summary><pre>{esc(shown)}</pre>'
                  + (f'<p class="muted">Cut after {SOURCE_LIMIT} characters.</p>' if len(text) > SOURCE_LIMIT else "") + "</details>")
        body = (f"<h1>{esc(e)}</h1>\n<p>{self.badges(e)}</p>\n<p>{esc(meta.get('description') or '')}</p>\n"
                f'<dl class="facts">{"".join(facts)}</dl>\n'
                f"<h2>Use it</h2>\n<pre>{esc(use)}</pre>\n{self.quality_section(e)}\n{self.citation_section(e)}\n<h2>Source</h2>\n{source}")
        return self.page(f"{e} · {self.title}", body, "../", str(meta.get("description") or ""))

    def derived(self, text: str) -> str:
        ref, _, version = text.partition("@")
        prefix, rest = split_prefix(ref)
        if prefix in (None, self.owner or DEFAULT_REGISTRY_NAME, DEFAULT_REGISTRY_NAME if not self.owner else None):
            try:
                target = self.reg.resolve(rest)
            except RegistryError:
                target = None
            if target is not None:
                return f'<a href="../{self.spec_href(target)}">{esc(text)}</a>'
        return esc(text)

    def index_json(self) -> str:
        rows = []
        for e in self.entries:
            meta = e.meta
            found = self.quality_of(e)
            rows.append({
                "spec": str(e), "format": e.format, "kind": e.kind, "page": self.spec_href(e),
                "version": None if meta.get("version") is None else str(meta["version"]),
                "status": meta.get("status") if isinstance(meta.get("status"), str) else None,
                "description": clean(str(meta.get("description") or "")),
                "extensions": [clean(str(x)) for x in meta.get("extensions") or []],
                "authors": [clean(a) for a in _author_names(meta)], "license": meta.get("license") if isinstance(meta.get("license"), str) else None,
                "doi": meta.get("doi") if isinstance(meta.get("doi"), str) else None,
                "extends": [text for text, _ in self.extends.get(str(e), [])],
                "quality": None if not found else {**found[0], "current": found[1]},
            })
        return json.dumps({"schema": 1, "title": self.title, "specs": rows}, indent=2, ensure_ascii=False, sort_keys=True) + "\n"


def build_site(reg: Registry, out: Path, results: dict | None, repo: str | None, title: str) -> list[Path]:
    """Write the site into `out`, a folder that is new, empty, or made by this before."""
    if out.exists():
        if not out.is_dir():
            raise RegistryError(f"{out} is not a folder")
        if any(out.iterdir()) and not (out / MARKER).is_file():
            raise RegistryError(f"{out} has something in it that is not a site made by fanbase; use a folder that is new or empty")
        for old in out.iterdir():
            shutil.rmtree(old) if old.is_dir() else old.unlink()
    out.mkdir(parents=True, exist_ok=True)
    site = Site(reg, results, repo, title)
    files: dict[str, str] = {MARKER: "made by `fanbase site`: this folder is replaced whenever it is run again\n", "style.css": STYLE.lstrip(),
                             "site.js": SCRIPT.lstrip(), "index.html": site.index_page(), "index.json": site.index_json()}
    for fmt in reg.formats():
        files[f"{fmt}/index.html"] = site.format_page(fmt)
    for e in site.entries:
        files[f"{e.format}/{e.kind}.html"] = site.spec_page(e)
    if results is not None:
        files["quality.json"] = json.dumps(results, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    written = []
    for name, text in sorted(files.items()):
        path = out / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        written.append(path)
    return written


def cmd_site(args, ctx: Context) -> int:
    reg = checkout(ctx)
    results = quality_results.for_registry(reg, getattr(args, "quality", None))
    repo = args.repo or (None if reg.info.get("name") else DEFAULT_REPO)
    if repo and not re.fullmatch(r"https://[^\s\"'<>]+", repo):
        raise RegistryError("--repo is the address of the registry's repository, https://...")
    title = clean(args.title or reg.info.get("title") or (reg.info.get("name") and f"{reg.info['name']} registry") or "Fanbase")
    written = build_site(reg, Path(args.output), results, repo.rstrip("/") if repo else None, title)
    print(f"{len([p for p in written if p.suffix == '.html'])} pages in {args.output}")
    return 0
