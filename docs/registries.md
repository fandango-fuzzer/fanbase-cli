# Registries

Part of the [fanbase-cli README](../README.md).

## Registries besides the public one

The public registry is the default, and the only one a plain name such as `png` can ever
mean. Other registries (a team's own, a research group's, a private one) are specs in a git
repo with the same layout. You add them by name, and then name them in front of a spec:

```bash
fanbase registry add https://github.com/acme/fuzz-specs      # named by the registry itself: acme
fanbase registry add ~/code/our-specs --name ours             # a local checkout, under a name you pick
fanbase list acme:                    # the formats of acme
fanbase list acme:png                 # its PNG specs
fanbase install acme:png-strict       # -> <install dir>/acme/png/png-strict.fan
fanbase registry list
fanbase registry remove acme [--uninstall]
```

A spec of an added registry is installed under the registry's name, so `include("acme/png/png-strict.fan")`
finds it and it never clashes with a spec of the same name elsewhere. `fandango -F acme:png-strict` works too.

**Adding a registry is a decision to trust it.** Its specs are Python code that runs inside Fandango
with your permissions, and they may ask for Python packages to be installed (plain package names only,
as [for `install`](using-specs.md)). `fanbase registry add` says so and asks; `--trust` answers yes in a script. Installing a
plain name never reaches a registry you did not name: `fanbase install png-strict` fails if only `acme`
has it, and tells you `acme:png/png-strict` exists.

To use a spec of another registry wherever you say `png`, **pin** it. The pin is yours, in your config:

```bash
fanbase pin png acme:png/png-strict   # `png` now means that spec for you (fandango -F png too)
fanbase pin                           # list the pins
fanbase unpin png
```

A private registry takes a token from an environment variable you name; the file holds the name,
never the token, and the token is sent to the registry's host and nowhere else:

```bash
fanbase registry add https://github.com/acme/private-specs --token-env ACME_TOKEN
```

Your registries and pins are in `$FANBASE_CONFIG`, else `$XDG_CONFIG_HOME/fanbase/config.yml`, else
`~/.config/fanbase/config.yml`.

### Publishing a registry of your own

Make a git repo with the layout below, add a `registry.yml` (`name: acme`, `description: ...`) to its
root, and run `fanbase reindex`. The name is what others call your registry and what your specs are
installed under, so a spec of yours can include another with `include("acme/png/png-base.fan")`.
Names are lower case letters, digits and dashes; `fanbase` is the default registry's.

## Finding the registry

`fanbase` looks, in order, at:

1. `--registry PATH-or-URL` (before the subcommand)
2. the `FANBASE_REGISTRY` environment variable
3. the current directory and its parents, for a registry checkout (`specs/` and `index.yml`)
4. the public registry, `https://github.com/fandango-fuzzer/fanbase`

```bash
fanbase --registry ~/code/fanbase list          # a local checkout
export FANBASE_REGISTRY=https://example.org/my-registry
```

A `github.com` URL (optionally `.../tree/<ref>`) is read through `raw.githubusercontent.com`.
Any other URL must serve raw file bytes. Remote reads use `index.yml` only: `list` fetches
that one file, `install` fetches the one spec it installs and checks it against the hash
recorded in the index.

If the registry cannot be reached, `fandango -F` uses the copy that is already installed,
with a warning, so a test run does not depend on the network.

## Registry layout

```
specs/<format>/<kind>/<kind>.fan
specs/<format>/<kind>/metadata.yml
index.yml
```

The default spec of a format is the kind named after the format: `specs/png/png/png.fan`.
Any other kind is named `<format>-<what makes it different>`, e.g. `png-apng`.

`index.yml` is generated: it repeats every `metadata.yml`, plus each spec's path and
SHA-256, so a client can browse the registry from a single file. It starts with the
`schema` it follows (currently 2; a file without one is schema 1). A client that meets a
newer schema than it knows says so and asks you to upgrade `fanbase`.

An installed spec is `<install dir>/<format>/<kind>.fan`, with a copy of its metadata next
to it as `<kind>.yml`.

### `format.yml`

What is true of a format, whichever of its specs is meant, can be said once, in `specs/<format>/format.yml`:

```yaml
title: Portable Network Graphics
mime: image/png
extensions: [png]
reference: https://www.w3.org/TR/png-3/
targets: [pillow, imagemagick]     # what files of this format are evaluated against; see evaluating.md
```

A spec has all of it, and what its own `metadata.yml` says wins. `reindex` does not copy the format's words into
every `metadata.yml`, but the index and what is installed say them for each spec, so a client needs nothing else.
A target named here or in a spec has to be defined in the registry's `targets/`.

### `metadata.yml`

```yaml
format: png
kind: png-apng
description: Animated PNG (APNG) with the usual ancillary chunks
fanbase: 0.4.0
fandango: '>=1.3'
requires: []
version: '1.0'
title: Portable Network Graphics
extensions:
- png
mime: image/png
reference: https://www.w3.org/TR/png-3/
```

| key | |
|---|---|
| `format`, `kind` | From the folder names. |
| `description` | One line. Written by hand. |
| `fanbase` | Version of this package that last ran `reindex`. |
| `fandango` | Fandango versions the spec is written for. Defaults to `>=1.3`; edit by hand. |
| `requires` | Third-party Python packages the spec imports, found by scanning its `import` lines (standard library and Fandango excluded). Listed by import name. |
| `pip` | Optional, written by hand. What to install for the spec, as pip requirements, when a package has a different name than its module: `requires: [yaml]` with `pip: [pyyaml>=6]`. Without it, the names in `requires` are taken to be package names. |
| `version` | Version of the spec, a version number such as `'1.0'` (in quotes). Written by hand; it is what a version range in `extends` or on a ref is compared with. |
| `extensions` | File name extensions of the format; the first one is used for generated files. |
| `authors` | Optional. Who wrote the spec: names, or `{name, orcid}`. |
| `license` | Optional. SPDX identifier of the spec's license, e.g. `Apache-2.0`. |
| `source` | Optional. What the spec was made from: a URL, or a few words. |
| `extends` | Optional. Specs this one builds on, as refs, each with an optional version range: `png`, `png>=1.0,<2`, `fanbase:png`. See [Building on other specs](writing-specs.md#building-on-other-specs). |
| `derived_from` | Optional. The spec this one was forked from, as a ref. Recorded and checked for shape. |
| `status` | Optional. `draft`, `stable` or `deprecated`. |
| `decodes` | Optional. How often its files are meant to be accepted by a parser: `always`, `mostly`, `rarely` or `never`. Many specs exist to produce files that do not decode, and `evaluate` reports against this. |
| `targets` | Optional. The targets to evaluate it against, instead of its format's. |
| `doi` | Optional. DOI of an archived copy, e.g. `10.5281/zenodo.1234567`. |

All the optional keys are written by hand and kept in this order after the generated keys;
`reindex` refuses a value of the wrong shape, naming the spec, and writes nothing in that case.

Other keys (`title`, `mime`, `reference`, ...) are kept as they are.

Run `fanbase reindex` and it creates `metadata.yml` where it's missing and refreshes the
generated keys. `description`, `fandango`, `pip` and any extra keys you add are left alone.
## `fanbase reindex [--check]`

For registry maintainers. Brings every `metadata.yml` and the root `index.yml` in line with
the `.fan` files, and tells you which specs still have no description. Run it before
committing changes to the registry. It needs a local checkout.

`--check` changes nothing and exits with status 1 if anything is out of date, which makes
it fit for CI.
