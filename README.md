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

## Contributing a spec

The public registry is a git repository, and a spec gets into it by a pull request. Clone it (or
your fork of it), and from inside the clone:

```bash
fanbase new png-fancy --extends png --description "PNGs with ..."   # a spec to edit, with its metadata
fanbase fork png-apng --as png-apng-mine                            # or: a copy of an existing spec to change
$EDITOR specs/png/png-fancy/png-fancy.fan
fanbase check                    # metadata and index in order? does each spec make Fandango produce inputs?
fanbase publish                  # branch, commit, push, and a pull request (needs git, and gh for the pull request)
```

- `new` makes `specs/<format>/<kind>/` with a `.fan` to edit and a `metadata.yml` (authors from your git
  settings, `status: draft`, `version: '0.1'`). With `--extends` the `.fan` includes what it builds on.
  A format's default spec is named after the format; any other is `<format>-<what makes it different>`.
- `fork` copies a spec of any registry (`acme:png-strict` too) into the checkout under a new name. It
  records `derived_from: fanbase:png/png-apng@1.0` and keeps the original authors, adding you. Use
  `--into DIR` to put it in another checkout, such as your own registry.
  A spec that builds on others (`extends`) is copied alone by default, and keeps building on the originals: in
  your own registry they are `fanbase:png`, and a spec of a third registry cannot be moved at all, since your
  registry cannot name it. With `--with-deps` the whole family is copied: what it extends, and what that extends,
  each under its own name, forked from the original like the spec itself (`derived_from`, version `0.1`, `draft`).
  The copies build on each other: their `extends` name the copies, with no version range (the copy is at `0.1`),
  and their `include("...")` lines point at the copies' files, so changing the base changes what builds on it. A
  dependency you forked before is kept as it is, with your changes; a name that is taken by something else stops
  the whole thing, and nothing is written.
- `rebase SPEC` brings a fork up to date with the spec it was forked from. It merges what changed in the
  original since you forked it into your version, with `git merge-file`, so a conflict looks like git's: fix it,
  and `check` refuses the spec until you do. It needs the original as it was when you forked, the common ancestor,
  which it finds, checking its hash against the `derived_sha256` that `fork` recorded, in the copy `fork` kept
  (in `$FANBASE_CACHE`, else `~/.cache/fanbase`), in the git history of your clone, or in an older release of the
  registry that you name with `--base-registry`. `--dry-run` says whether it merges cleanly and writes nothing.
  Your `metadata.yml` is merged the same way for the four keys a fork usually keeps from its original,
  `extends`, `fandango`, `pip` and `extensions`: `fork` records what the original said then (`derived_meta`), so
  `rebase` knows what the original changed since. A list gets the original's additions and loses what the original
  dropped, whatever you added of your own; a value is taken if only the original changed it, and is said to be in
  conflict, and left as yours, if you both did. It is offered, not done behind your back: at a terminal it asks;
  otherwise it says what it would change and how to take it (`--metadata adopt`, or `--metadata keep` to leave it
  out for good). For a fork made before `derived_meta` existed, `--adopt fandango,extends` takes the original's
  current value of those keys, and from then on the original's words are recorded. `license` and `mime` are only
  told when they differ. Give the fork a new `version` afterwards.
- `check [SPEC...]` verifies that `metadata.yml` and `index.yml` are up to date, that what specs extend
  exists, and that each spec makes Fandango produce inputs (it needs `fandango` installed; `--count N`,
  `--timeout S`, `--no-generate`). The Python packages a spec imports are installed first, as `fandango -F`
  does, so a spec that needs one is checked rather than failing on an import; `--no-requirements` only says which are missing. It notes specs with no description, authors or license, and `--strict`
  makes that a failure. `--base REGISTRY` also demands that a spec that changed since then has a new version.
  A registry of hundreds of specs cannot be run through Fandango one after the other: `-j N` asks about N specs
  at a time (packages are still installed one spec at a time), and `--changed --base REGISTRY` asks only about the
  specs that are new or different from that registry, and about everything that builds on them. The index and what
  specs extend are always checked for the whole registry; a pull request's CI uses `--changed`, `main` checks all.
- `changes --base REGISTRY` lists what was added, changed and removed since another registry or release
  (`--markdown` for release notes, `--check` to fail when a changed spec kept its version).
- `publish` refreshes `metadata.yml` and `index.yml`, runs `check`, says what it is going to do, and asks
  (`--yes` answers for scripts, `--dry-run` only shows the plan). Then it makes a branch (unless you are on
  one already), commits `specs/`, `index.yml` and `registry.yml` as you, pushes, and opens the pull request
  with `gh` (`--no-pr` to stop after the push). Nothing else in your clone is touched.

Give a spec that changed a new `version`: it is what lets everyone, and the registry's checks, tell the
new from the old.

## Evaluating a spec

`fanbase check` says that a spec produces files. `fanbase evaluate` says how those files do against the
parsers of their format, from a registry checkout:

```bash
fanbase evaluate png png-apng             # 100 inputs from each, asked about by the targets of the format
fanbase evaluate --changed --base https://github.com/fandango-fuzzer/fanbase    # what you changed, and what builds on it
fanbase evaluate png -n 1000 --json       # as JSON; --markdown for a CI job summary; --json-file FILE as well
fanbase targets                           # the targets of this checkout, and whether they can run here
```

```
png/png-apng  version 1.0: 100 inputs from seed 1 in 3.7s (27.0/s)
  target   accepted  invalid  unsupported  resource-limit  crash  timeout  error  files/s  version
  pillow    100/100        0            0               0      0        0      0     9676  12.3.0
  expected always: best target accepts 100%, which is always; as expected
```

- **Validity.** For each target, how many files it accepts, and the rest by reason: invalid, unsupported (a
  feature the parser lacks), resource limit (a file that asks for more than it may have), crash, timeout, or a
  failure of the target itself. The commonest reasons are listed, made the same for files that differ only in
  name or number.
- **Throughput.** How fast Fandango produces inputs, and how fast each target answers. Both depend on the
  machine; read them as a trend.
- **What to expect.** Many specs exist to make files that do not decode. A spec says how often its files should
  be accepted, with `decodes: always|mostly|rarely|never`, and the report says whether the best target agrees
  (`--strict` makes a mismatch a failure; `--suggest-decodes` says, for a spec that does not say, what the best target saw, for
  its authors to decide). One parser's word is not the format's: a parser may lack a feature, so
  the best target decides, and a format should have several.
- **Same each time.** `--seed` (default 1) is given to Fandango, and to Python's hash seed, so the inputs are the same.
- **Time.** Some specs take seconds for a hundred inputs, some minutes. Fandango gets `--budget` seconds per spec
  (default 60): it is asked for a few inputs first, to see how fast they come, then for as many as fit in the time
  left, and what it made by then is used (the report says how many). A target gets `--judge-budget` seconds per spec
  (default 60) and `--timeout` per file (default 10); a target that waits out its timeout on file after file does
  not hold up the rest, and the report says how many files it was asked about.

**Targets** are the parsers and tools files are judged by. A format names them in its `format.yml`
(`targets: [pillow, imagemagick]`), or a spec does in its own `metadata.yml`; each is a folder `targets/<name>/`
of the registry with a `target.yml`:

```yaml
title: Pillow
formats: [png, gif, bmp, jpeg, tiff, webp]                # what it can judge
run: ["{python}", "{dir}/harness.py", "{file}"]           # exit 0 accepts the file; anything else rejects it
needs: {commands: [], python: [PIL], pip: [Pillow]}       # what has to be there; evaluate says what is missing
version: ["{python}", "-c", "import PIL; print(PIL.__version__)"]
classify:                                                 # what a rejection means, by what it says
  - match: "(?i)decompression ?bomb|exceeds limit"
    as: resource-limit
```

`{file}` is the file asked about, `{dir}` the target's folder, `{python}` the interpreter fanbase runs in.
A tool with the right exit status needs no harness: `run: [djpeg, -fast, -outfile, /dev/null, "{file}"]`.

A target is a command that a registry tells fanbase to run, so it is as much code as a spec is: `evaluate` works on a
checkout you have read, and in CI on a machine that is thrown away. Each file is judged by a process of its own,
with a timeout (`--timeout`), a memory and CPU limit (`--memory`, enforced on Linux), and core dumps switched off.

**Crashes and hangs are not for a public log.** A parser that crashes, or hangs, on a generated file may have a bug
that is not fixed yet, and the registry's [ethics considerations](https://github.com/fandango-fuzzer/fanbase/blob/main/ETHICS.md)
are that concrete inputs and unfixed vulnerabilities are not published. A crash or a hang is counted and named in
what you run locally. With `--hide-crashes`, which CI uses for anything public, it is counted as a plain error and
nothing about it is kept, and no input is ever saved unless you ask with `--keep DIR` (which does not go with it).

### How much of a parser the files reach: `--coverage`

That a parser accepts a file says little about how much of the parser the file ran. A target that is built to be
measured says so in its `target.yml`, and `fanbase evaluate --coverage` asks how much of the library the generated
files reach, against a few real files:

```bash
fanbase evaluate png --coverage -n 1000 --curve 1,10,100,1000      # + every target that can be measured; --coverage-only: just those
```

```
  coverage of libpng-cov libpng 1.6.59: 8119 lines, 5547 branches
    6 real file(s) reach 1306 (16.1%) lines and 652 (11.8%) branches
    1 generated: 1213 (14.9%) lines, 587 (10.6%) branches
    100 generated: 1824 (22.5%) lines, 939 (16.9%) branches
    the generated files reach 728 lines the real ones do not; the real ones reach 210 the generated ones do not
```

- The real files (`seeds`) are run first: that is the baseline. Then the generated files, one after another, with the
  coverage read after the 1st, 10th, 100th (`--curve`, default 1, 10, 100, 1000, and the last): a curve that is
  flat from the first input says the spec makes files that are all alike.
- The comparison says what the generated files reach that real ones do not (what a grammar finds that a camera
  does not) and the reverse (what the spec is missing).
- fanbase does not know how coverage is counted. The target says how to clear it and how to read it:

```yaml
coverage:
  reset: ["/opt/cov/bin/cov-reset", "libpng"]            # clears what has been covered
  snapshot: ["/opt/cov/bin/cov-snapshot", "libpng"]      # prints what has been covered since, as JSON
  seeds: ["/opt/cov/seeds/png/*"]                        # real files; a relative pattern is in the target's folder
```

  The snapshot is `{"schema": 1, "lines": {"total": N, "covered": ["file:line", ...]}, "branches": {...}}` (branches
  are optional), so a C library built with gcov, a Python module measured with coverage.py, or anything else, fits.
  The registry's `coverage/` has an image with libpng, libjpeg-turbo, giflib, libtiff, libwebp and stb_image built
  with gcov, so that CI compiles nothing. The files are run one at a time, in order (the counters are shared), and
  what the verdict pass ran does not count: the counters are cleared first.

### Choosing between grammars: `quality.json` and `--compare-with`

Several specs can describe the same format. What is kept of an evaluation for choosing between them is
`quality.json`: for each spec, tied to the hash of the spec it was measured on, how much of its files each parser
accepts, how much of a library they reach, and how fast Fandango makes them.

```bash
fanbase evaluate --all --coverage --json-file evaluation.json     # the run
fanbase quality build evaluation.json -o quality.json              # what is kept (several reports can be one document)
fanbase list png --quality                                         # in the listing: [accepts 100%, covers 22.5%, 16/s]
fanbase show png/png-apng --quality
```

A registry says where it keeps the results (`quality:` in its `registry.yml`), or has a `quality.json` in its root if
it is a checkout; for a GitHub registry it is the asset of its latest release. `--quality FILE|URL` reads another.
Results measured on an earlier version of a spec are shown as such ("for an earlier version"). The results are read
strictly: only numbers and cleaned names are taken from them, as they come from a registry that may not be yours.

`evaluate --compare-with FILE|URL` says what changed since earlier results (a `quality.json` or a report of
`--json-file`), and why: a drop is **worse** only when it is the spec's, that is, the parser is the same one as
before (its version is recorded); if the parser was updated, the number moved for a reason the spec did not give,
and it is only a note. A parser that accepts five points fewer of a spec's files, or two points less of a library's
lines, is worse; Fandango's speed is only told when it moves by half or double, since it is the machine's as much as
the spec's. `--fail-on-worse` makes it an exit status. Nothing to compare with (the first run) is only said.

### Signed registries: `fanbase sign`, `--signer`, `fanbase verify`

A spec is code that runs inside Fandango. `index.yml` holds the SHA-256 of every spec, so if a registry's maintainers sign
the index, every spec that matches it is what they published, whoever else got to the repository, the host or the network.
The signature is `index.yml.sig`, next to the index; it is an SSH signature, made and checked by `ssh-keygen` (OpenSSH
8.0 or later), so a key can be an ordinary one or one on a hardware token, and it is made in the namespace `fanbase`, so a
signature the same key made for anything else (a git commit) is not one for an index.

```bash
fanbase reindex && fanbase sign --key ~/.ssh/fanbase_signing     # a maintainer, in the checkout: writes index.yml.sig
fanbase registry add https://github.com/acme/specs --signer ~/keys/acme.pub     # a user: from now on, only if it is signed by it
fanbase verify acme                         # is it still? (or: fanbase --registry URL verify --signer KEY)
```

The keys to trust are the user's choice, never the registry's: a registry that said which keys to trust would say so in the
very files in question. They are pinned with `registry add --signer` (a key, or a `.pub` file; the registry has to be signed by
one of them to be added), and kept in the user's config; the public registry's will be in the CLI itself. A registry that
has keys pinned is refused, with nothing in it believed, if its index is not signed, is signed by another key, or was changed
after it was signed. Only modern keys (ed25519, ecdsa, security keys) are taken, and only the key itself, not a comment.

What a signature does not say is that the index is the newest: an old index, signed, stays signed. To read a registry as it was
at a release, point at the release (`.../tree/<tag>`), which does not move; a registry that signs only its releases is read
verified that way. fanbase never reads a private key: `ssh-keygen` does the signing, and asks for the passphrase or the touch.

### A site to browse the registry: `fanbase site`

```bash
fanbase site site/ --quality                 # in a registry checkout: pages for the registry, each format and each spec
fanbase site site/ --quality results.json --title "Acme specs" --repo https://github.com/acme/specs
```

A page for the registry (every spec, with a filter by words, format and status), one for each format, and one for each spec:
what it is and who made it, what it extends and what extends it, how to install and use it, how well it does (with
`--quality`: how much of its files each parser accepts, how much of a library they reach, with a picture of how that grows
with the number of files, and how fast they are made), how to cite it, and its source. `index.json` has the same for
machines. It is plain files, to put anywhere (GitHub Pages, for one); the registry's `site.yml` does that.

The metadata of a spec comes from pull requests, so nothing in it is trusted: every text is cleaned and escaped, a link is
only made to an http(s) address, and each page says in a Content-Security-Policy that it takes nothing from anywhere else
(the style and the filter are two files of the site's own). Nothing is run: a spec is read, never executed. The same registry
and results give the same files, byte for byte, because nothing is dated. The folder is replaced each time it is made, and
only if it is new, empty, or made by this before.

### A DOI for a spec: `fanbase doi`

A DOI makes a spec citable as it was: [Zenodo](https://zenodo.org) keeps the spec's `.fan` and `metadata.yml`, and the DOI
always means those. Zenodo's records are permanent, and cannot be taken back or changed, so this is opt-in and careful:

```bash
fanbase doi png-apng                           # says what would be sent, and sends nothing
fanbase doi png-apng --sandbox                 # tries it on sandbox.zenodo.org, whose DOIs mean nothing; writes nothing
fanbase doi png-apng --production              # publishes on zenodo.org after asking, and writes doi: into metadata.yml
fanbase doi png-apng --production --new-version    # the spec changed after it got a DOI: a new version of that record
```

The access token is in `ZENODO_TOKEN` (`ZENODO_SANDBOX_TOKEN` for the sandbox) and is sent to Zenodo's own host over
https, and never shown or put in a file. A spec in draft, one without authors or version, one whose `metadata.yml` is out of
date, and one that has a DOI already are refused. The record is of type software, named after the spec and its version, with the
spec's authors (as "Family, Given"), license and a link to the registry. A record that cannot be finished is deleted rather
than left half made. The DOI goes into the spec's metadata.yml for you to commit; `fanbase cite` uses it from then on, and it
does not make the spec a changed one (a DOI is not a new version of the grammar).

### The private record of crashes and hangs

Hiding a crash from a public report is not the same as losing it: whoever runs the evaluation is the one who has to
report the bug. So `evaluate` can write the details down, for you alone:

```bash
fanbase evaluate --all --incidents ~/fanbase-private                  # on your machine
fanbase evaluate --all --hide-crashes --incidents DIR --incident-recipients recipients.txt     # in CI
```

- **What is in it.** For each cause (a bug found forty times is one entry): the input (three examples), the command
  with the file as `INPUT`, how it ended (the signal), what the target said, whether it happened again when tried
  once more (a hang can be a busy machine), the target's version, and what is needed to make the file again: the
  spec's version and hash, the Fandango and Fanbase versions, the seed. And a note to the vendor to start from, with a
  checklist: report it privately, write down the date, a usual 90 days to a fix.
- **On your machine** (`--incidents DIR`) it is a folder only you can read (modes 0700 and 0600), written only if
  something broke.
- **In CI** (`--incident-recipients FILE`) it is one file, `evaluation-private.age`, encrypted with
  [age](https://age-encryption.org) to the public key(s) in `FILE`, which makes it safe in a public artifact and
  needs no secret to make. It is padded to whole megabytes and written on every run, whether or not anything broke,
  so that its existence and its size say nothing (and it is never more than about 15 MB, so that it can always be mailed:
  past 300 causes, or 8 MB of inputs, things are counted and not written down). Nothing about it is printed, and what is public still counts a
  crash or a hang as a plain error. If `age` or the key is missing, the run is refused before it starts.
- **Make a key once**, on your machine: `age-keygen -o fanbase.key` writes the private key (keep it to yourself) and
  prints the public key, `age1...`, which goes in `recipients.txt`. Fanbase never sees the private key.

```bash
fanbase incidents open evaluation-private.age --identity fanbase.key --into opened    # decrypt and unpack
fanbase incidents send evaluation-private.age --to you@example.org                   # mail it (for CI)
```

`open` unpacks only plain files with plain names into a folder that is new or empty (never into one with something
in it, and never anywhere else), and makes it readable by you alone. Start with its `REPORT.md`.

`send` mails the encrypted file and nothing else, and refuses anything that does not start with the `age` header, or
is over 20 MB (a mail server may refuse less: keep the artifact as the copy that is always there). The body says the
same thing whatever the file holds. The server comes from the environment, so the secrets stay out of any file:

| Variable | |
|---|---|
| `FANBASE_SMTP_HOST` | the mail server |
| `FANBASE_SMTP_PORT` | `465` (the default): TLS from the first byte; another port, such as `587`, must upgrade with STARTTLS or nothing is sent |
| `FANBASE_SMTP_USER`, `FANBASE_SMTP_PASSWORD` | the login, if the server wants one (both or neither); only sent over TLS |
| `FANBASE_SMTP_FROM` | the sender (default: the user, if that is an address) |

Certificates are checked. Errors say what kind of failure it was and nothing else: no host, address or password
is ever printed, since the output of a CI job is public.

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

### `format.yml`

What is true of a format, whichever of its specs is meant, can be said once, in `specs/<format>/format.yml`:

```yaml
title: Portable Network Graphics
mime: image/png
extensions: [png]
reference: https://www.w3.org/TR/png-3/
targets: [pillow, imagemagick]     # what files of this format are evaluated against; see below
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
| `extends` | Optional. Specs this one builds on, as refs, each with an optional version range: `png`, `png>=1.0,<2`, `fanbase:png`. See [Building on other specs](#building-on-other-specs). |
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