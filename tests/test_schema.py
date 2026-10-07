import pytest
import yaml

from fanbase.cli import main
from fanbase.manifest import (
    INDEX_FILENAME,
    SCHEMA_VERSION,
    check_metadata,
    dump_index,
    parse_index,
    reindex,
)
from fanbase.registry import Registry, RegistryError


def run(capsys, *argv):
    code = main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


def edit_metadata(registry, kind, **meta):
    path = registry / "specs" / kind.split("-")[0] / kind / "metadata.yml"
    data = yaml.safe_load(path.read_text())
    data.update(meta)
    path.write_text(yaml.safe_dump(data))


def metadata(registry, kind):
    path = registry / "specs" / kind.split("-")[0] / kind / "metadata.yml"
    return yaml.safe_load(path.read_text())


# --- the index carries its schema

def test_the_index_says_which_schema_it_has(registry):
    index = yaml.safe_load((registry / INDEX_FILENAME).read_text())
    assert index["schema"] == SCHEMA_VERSION == 2
    assert [row["kind"] for row in index["specs"]] == ["gif", "png", "png-apng"]


def test_an_index_without_a_schema_is_schema_1_and_still_read(registry):
    text = (registry / INDEX_FILENAME).read_text()
    old = yaml.safe_load(text)
    del old["schema"]  # what a 0.4 registry looks like
    assert [str(e) for e in parse_index(yaml.safe_dump(old))] == ["gif/gif", "png/png", "png/png-apng"]


def test_an_index_from_the_future_is_refused_with_a_way_out():
    future = yaml.safe_dump({"schema": SCHEMA_VERSION + 1, "specs": []})
    with pytest.raises(RegistryError, match=r"schema 3.*pip install --upgrade fanbase"):
        parse_index(future)


@pytest.mark.parametrize("schema", ["two", 2.5, True, [2]])
def test_a_schema_that_is_not_a_whole_number_is_a_malformed_index(schema):
    with pytest.raises(RegistryError, match="malformed"):
        parse_index(yaml.safe_dump({"schema": schema, "specs": []}))


def test_a_client_refuses_a_registry_from_the_future(capsys, registry, served):
    (registry / INDEX_FILENAME).write_text(yaml.safe_dump({"schema": 99, "specs": []}))
    code, _, err = run(capsys, "--registry", served, "list")
    assert code == 2 and "schema 99" in err and "upgrade" in err


def test_check_notices_an_index_without_a_schema(capsys, registry):
    old = yaml.safe_load((registry / INDEX_FILENAME).read_text())
    del old["schema"]
    (registry / INDEX_FILENAME).write_text(yaml.safe_dump(old))
    code, out, _ = run(capsys, "--registry", str(registry), "reindex", "--check")
    assert code == 1 and "index.yml out of date" in out
    run(capsys, "--registry", str(registry), "reindex")
    assert run(capsys, "--registry", str(registry), "reindex", "--check")[0] == 0


# --- the optional keys

GOOD = dict(
    authors=["Ada Lovelace", {"name": "Grace Hopper", "orcid": "0000-0002-1825-0097"}, {"name": "No Orcid"}],
    license="Apache-2.0",
    source="https://example.org/a-template",
    extends=["png", "png/png-apng"],
    derived_from="png/png",
    status="stable",
    doi="10.5281/zenodo.1234567",
)


def test_all_optional_keys_are_accepted_and_kept_in_order(registry):
    edit_metadata(registry, "png-apng", version="1.0", **GOOD)
    reindex(Registry(registry))
    meta = metadata(registry, "png-apng")
    assert {k: meta[k] for k in GOOD} == GOOD
    # core keys, then the optional ones in a fixed order, then whatever else a person wrote
    assert list(meta) == [
        "format", "kind", "description", "fanbase", "fandango", "requires",
        "authors", "license", "source", "extends", "derived_from", "status", "doi",
        "extensions", "version",
    ]


def test_reindex_is_stable_with_the_optional_keys(registry):
    edit_metadata(registry, "png-apng", **GOOD)
    reindex(Registry(registry))
    _, changed, _ = reindex(Registry(registry))
    assert changed == []


@pytest.mark.parametrize("key, value, complaint", [
    ("status", "stabel", "status: 'stabel' is not one of draft, stable, deprecated"),
    ("status", None, "status"),
    ("license", "", "license: expected text"),
    ("license", ["MIT"], "license: expected text"),
    ("source", 5, "source: expected text"),
    ("derived_from", ["png"], "derived_from: expected text"),
    ("doi", "zenodo.123", "doi: 'zenodo.123' is not a DOI"),
    ("doi", 10.5281, "doi"),
    ("extends", "png", "extends: expected a list"),
    ("extends", ["png", ""], "extends: expected a list"),
    ("authors", "Ada", "authors: expected a list"),
    ("authors", [], "authors: expected a list"),
    ("authors", [{"orcid": "0000-0002-1825-0097"}], "has no name"),
    ("authors", [{"name": "Ada", "orcid": "1234"}], "orcid '1234' is not like"),
    ("authors", [42], "has no name"),
])
def test_a_wrongly_shaped_optional_key_is_an_error_naming_the_spec(capsys, registry, key, value, complaint):
    edit_metadata(registry, "png-apng", **{key: value})
    code, _, err = run(capsys, "--registry", str(registry), "reindex")
    assert code == 2
    assert "invalid metadata" in err and "png/png-apng" in err and complaint in err


def test_check_metadata_reports_every_problem():
    problems = check_metadata({"status": "x", "doi": "y", "license": ""})
    assert len(problems) == 3


def test_an_invalid_spec_leaves_the_registry_untouched(capsys, registry):
    # gif would be refreshed (it now imports something), but png-apng is invalid: write nothing.
    (registry / "specs/gif/gif/gif.fan").write_text("import brotli\n<start> ::= 'gif'\n")
    before = (registry / "specs/gif/gif/metadata.yml").read_text()
    edit_metadata(registry, "png-apng", status="nonsense")
    code, _, err = run(capsys, "--registry", str(registry), "reindex")
    assert code == 2 and "png/png-apng" in err
    assert (registry / "specs/gif/gif/metadata.yml").read_text() == before
    assert "brotli" not in before


def test_reindex_check_also_fails_on_invalid_metadata(capsys, registry):
    edit_metadata(registry, "png", status="nonsense")
    code, _, err = run(capsys, "--registry", str(registry), "reindex", "--check")
    assert code == 2 and "status" in err


def test_the_optional_keys_reach_the_index_and_the_client(capsys, registry, served):
    edit_metadata(registry, "png-apng", **GOOD)
    run(capsys, "--registry", str(registry), "reindex")
    code, out, _ = run(capsys, "--registry", served, "show", "png-apng")
    shown = yaml.safe_load(out)
    assert code == 0 and shown["license"] == "Apache-2.0" and shown["authors"][1]["orcid"] == "0000-0002-1825-0097"


# --- an index is not trusted to name only safe things

def index_with(**changes):
    row = {"format": "png", "kind": "png", "path": "specs/png/png/png.fan", "sha256": "0" * 64}
    row.update(changes)
    return yaml.safe_dump({"schema": 2, "specs": [row]})


@pytest.mark.parametrize("changes", [
    {"format": "../../etc"},
    {"format": "a/b"},
    {"format": ".."},
    {"format": ".hidden"},
    {"format": ""},
    {"format": 5},
    {"kind": "../../../.bashrc"},
    {"kind": "x/../y"},
    {"kind": "with space"},
    {"kind": "a\x1b[31mb"},
    {"path": "../../elsewhere/x.fan"},
    {"path": "/etc/passwd"},
    {"path": "specs/../../x.fan"},
    {"path": "specs//x.fan"},
    {"path": "specs\\x.fan"},
    {"path": "https://evil.example/x.fan"},
    {"path": "C:/x.fan"},
    {"path": ""},
    {"sha256": 5},
])
def test_an_index_naming_something_unsafe_is_refused(changes):
    with pytest.raises(RegistryError, match="not safe|malformed"):
        parse_index(index_with(**changes))


def test_ordinary_names_are_fine():
    entries = parse_index(index_with(format="png", kind="png-apng.v2_x", path="specs/png/png-apng/a.fan"))
    assert str(entries[0]) == "png/png-apng.v2_x"


# --- a registry says who it is

def test_a_registry_yml_goes_into_the_index(capsys, registry):
    (registry / "registry.yml").write_text(yaml.safe_dump({"name": "acme", "description": "Acme"}))
    assert run(capsys, "--registry", str(registry), "reindex", "--check")[0] == 1  # the index lacks it
    run(capsys, "--registry", str(registry), "reindex")
    index = yaml.safe_load((registry / INDEX_FILENAME).read_text())
    assert index["registry"] == {"name": "acme", "description": "Acme"}
    assert list(index) == ["schema", "registry", "specs"]
    assert run(capsys, "--registry", str(registry), "reindex", "--check")[0] == 0
    assert Registry(registry).info["name"] == "acme"


@pytest.mark.parametrize("name", ["Acme", "1acme", "a b", "a/b", "x" * 40, "fanbase", 5])
def test_a_registry_name_has_to_be_usable_and_not_the_default(capsys, registry, name):
    (registry / "registry.yml").write_text(yaml.safe_dump({"name": name}))
    code, _, err = run(capsys, "--registry", str(registry), "list")
    assert code == 2 and "registry" in err
