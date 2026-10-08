# Writing specs

Part of the [fanbase-cli README](../README.md).

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
