# Using specs

Part of the [fanbase-cli README](../README.md).

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

### `fanbase search [WORD...] [--extension EXT] [--all]`

Find specs by what their name, title, description, file name extension or media type say. Every
word has to match. `--all` also searches the registries you added.

```bash
fanbase search animated png
fanbase search --extension jpg
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

`--into DIR` installs somewhere else. The default is the first place Fandango looks, following its own
search order: the first entry of `$FANDANGO_PATH`; else on a Mac `~/Library/Fandango`; else
`$XDG_DATA_HOME/fandango`, by default `~/.local/share/fandango`. A copy left in a place Fandango looks at
later cannot shadow what is installed.

### `fanbase outdated`, `fanbase diff`, `fanbase cite`

```bash
fanbase outdated               # installed specs that a registry has a different version of
fanbase outdated --check       # ... and exit 1 if there are any (for scripts)
fanbase diff png               # how the registry's png differs from the one you installed
fanbase diff png acme:png-strict   # how two specs differ (exit 1 when they do)
fanbase cite png               # how to cite it: with its DOI if it has one, else where and the hash of the file
fanbase cite png --bibtex
```

`list`, `show`, `search`, `outdated`, `cite` and `list --installed` take `--json`, for scripts.

### `fanbase uninstall REF...`

Remove installed specs, with their metadata copy. It works offline, removes all the named
specs or none, and only touches specs `fanbase` installed: a `.fan` file you put in the
install directory yourself is never removed.

### `fanbase update [REF...]`

Bring installed specs up to date. Without arguments, every installed spec is checked.

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
