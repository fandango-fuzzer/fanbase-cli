# fanbase-cli

The `fanbase` command: browse, install and update grammars from the Fanbase registry.

The registry itself lives in the [`fanbase`](https://github.com/fandango-fuzzer/fanbase)
repository; this package is the client. You never need the whole registry: a spec is
fetched when you ask for it, and fetched again only when the registry has a newer one.

## Install

```bash
pip install fanbase
```

Fandango depends on `fanbase`, so `pip install fandango-fuzzer` installs it too.

## Using specs with Fandango

`fandango -F NAME` is `fandango -f FILE`, for a spec from the registry. The spec is fetched
into Fandango's standard library location if it is missing or out of date, and then used:

```bash
fandango fuzz -F png -n 10                  # the default PNG spec; writes png-inputs/fandango-0000.png ...
fandango fuzz -F png-apng -n 10 -d apngs    # a named variant, into a directory of your choice
fandango fuzz -F png -f mypng.fan -n 10     # your own rules on top of the Fanbase spec
```

Since the format is known, the file name extension is set for you, and with neither `-o`
nor `-d` the files go to a fresh directory (`png-inputs`, then `png-inputs-2`, ...). Every
other `fandango fuzz` option works as usual. See the Fandango documentation for details.

## Commands

### `fanbase list [FORMAT]`

Without an argument, list the formats in the registry. With a format, list its specs,
with a one-line description and any Python packages they need. Installed specs are
marked with `*`.

`fanbase list --installed [FORMAT]` lists what is installed, from the install directory
alone. It works offline.

```bash
fanbase list
#   bmp   3 specs
#   png   6 specs

fanbase list png
#  *png             Valid PNGs of every colour type with all the standard chunks
#   png-apng        Animated PNG (APNG) with the usual ancillary chunks
#   ...
```

### `fanbase show REF`

Print a spec's metadata: description, version, file extensions, required packages.

### `fanbase install REF... [--into DIR]`, `fanbase install --all`

Copy specs into the directory Fandango's `include()` searches, so they can be included
straight away. `REF` is `<format>/<kind>`, a plain kind such as `png-apng`, or a plain
`<format>` for the format's default spec. Installing a spec that is already up to date
does nothing.

```bash
fanbase install png png-apng
#   installed png/png -> ~/Library/Fandango/png/png.fan
#     include("png/png.fan")
#   installed png/png-apng -> ~/Library/Fandango/png/png-apng.fan
#     include("png/png-apng.fan")
```

`fanbase install --all` installs every spec in the registry, which is handy for a test machine that should work offline.

A spec that imports third-party Python packages lists them under `requires`. `install` and
`update` install the ones that are missing, with pip (or `uv pip`, in an environment without
pip), into the environment `fanbase` runs in, which is the one Fandango runs in. Packages that
are already installed are left alone. `--no-requirements` skips this and only prints what the
specs need, for you to install. Only plain package requirements are ever passed to pip:
a package name with optional extras and version specifiers. Anything else in a spec's
metadata (an option such as `--index-url`, a URL, a path) is refused, and nothing is
installed. `fandango -F` installs missing packages the same way. Most
specs need nothing beyond the standard library.

A spec names the Fandango versions it is written for (`fandango: '>=1.3'`). If the
Fandango you have installed is outside that range, `install`, `update` and `fandango -F`
warn; the spec is installed and used anyway.

`--into DIR` installs somewhere else. The default follows Fandango's own search order:
`$FANDANGO_PATH` (first entry), then `$XDG_DATA_HOME/fandango`, then `~/Library/Fandango`
(macOS) or `~/.local/share/fandango` (elsewhere).

### `fanbase uninstall REF...`

Remove installed specs, with their metadata copy. It works offline, removes all the named
specs or none, and only touches specs `fanbase` installed: a `.fan` file you put in the
install directory yourself is never removed.

### `fanbase update [REF...]`

Bring installed specs up to date. Without arguments, every installed spec is checked.

### `fanbase reindex [--check]`

For registry maintainers. Brings every `metadata.yml` and the root `index.yml` in line with
the `.fan` files, and tells you which specs still have no description. Run it before
committing changes to the registry. It needs a local checkout.

`--check` changes nothing and exits with status 1 if anything is out of date, which makes
it fit for CI.

## Repeating a run: `fanbase.lock`

Registries change every week. To fuzz with exactly the specs you used last time, lock them:

```bash
fanbase lock png png-apng       # writes fanbase.lock: these two, what they extend, and a hash of each
git add fanbase.lock            # commit it with the project

fanbase install --locked        # installs exactly those, or nothing if a registry has them differently
fanbase install --locked --into .fanbase     # into the project instead of the shared install directory
fanbase lock --check            # for CI: exit 1 if the registries have moved on from the lock
fanbase lock                    # on purpose: bring the lock up to what the registries have now
```

With a `fanbase.lock` in the current directory (or the file `$FANBASE_LOCK` names), `fandango -F png`
holds the run to it as well: if `png` is not exactly what the lock says, the run stops and says so,
instead of quietly using a newer one. The specs it extends are held to the lock too. Without
the registry, the installed copy is used, but only if it is the locked one. A spec that is not in the lock
is used as usual, with a warning.

A lock holds hashes, not copies. Once a spec has changed in the registry, the locked version can
only be fetched from an older release of the registry, so the lock records where the registry was
read from. **Lock against a release, not against `main`**, which `fanbase lock` warns about:

```bash
fanbase --registry https://github.com/fandango-fuzzer/fanbase/tree/<tag> lock png
```

The lock reads the public registry from where it was locked, unless you pass `--registry` or set
`$FANBASE_REGISTRY`. A registry you added has to be added on your machine; the lock says where it came from and
`fanbase install --locked` tells you what to run if it is missing.

## Building on other specs

A spec can build on another instead of copying it. It says so in its `metadata.yml`, includes the
other spec, and redefines what it wants to change:

```yaml
# specs/png/png-apng/metadata.yml
extends:
  - png                # a spec of the same registry
  - png>=1.0,<2        # ... in a version the range allows
```

```python
# png-apng.fan
include("png/png.fan")
<image> ::= <apng_image>      # redefine what differs
```

Installing a spec installs what it extends first, so the `include()` finds its file: `fanbase install
png-apng` puts `png` there too, and so does `fandango -F png-apng`. Cycles are refused.

```bash
fanbase deps png-ultra           # what it stands on, as a tree
fanbase deps png --reverse       # what stands on png
fanbase uninstall png            # refused while png-apng is installed (--force overrides)
```

A range can also be put on a ref, as a check: `fanbase install 'png>=1.2'` fails if the registry's
`png` is older, and `png@1.2` means `png==1.2`. The registry has one version of each spec at a
time; an older one is found by pointing `--registry` at an older release of it
(`https://github.com/fandango-fuzzer/fanbase/tree/<tag>`).

Which specs may extend which: a registry's specs extend specs of the same registry. The default
registry's extend nothing else, so a release of it stands on its own. A registry you add can also
extend the default one's, written `fanbase:png`; it cannot name a third registry, because the name a
user gives a registry is the user's own. Inside a registry called `acme`, `acme:png-base` means its
own `png-base`. `fanbase reindex` checks all of this (names, ranges, cycles) and writes nothing if
something is wrong.

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
as above). `fanbase registry add` says so and asks; `--trust` answers yes in a script. Installing a
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
| `extends` | Optional. Specs this one builds on, as refs, each with an optional version range: `png`, `png>=1.0,<2`, `fanbase:png`. See [Building on other specs](#building-on-other-specs). |
| `derived_from` | Optional. The spec this one was forked from, as a ref. Recorded and checked for shape. |
| `status` | Optional. `draft`, `stable` or `deprecated`. |
| `doi` | Optional. DOI of an archived copy, e.g. `10.5281/zenodo.1234567`. |

All the optional keys are written by hand and kept in this order after the generated keys;
`reindex` refuses a value of the wrong shape, naming the spec, and writes nothing in that case.

Other keys (`title`, `mime`, `reference`, ...) are kept as they are.

Run `fanbase reindex` and it creates `metadata.yml` where it's missing and refreshes the
generated keys. `description`, `fandango`, `pip` and any extra keys you add are left alone.