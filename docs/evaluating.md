# Evaluating a spec

Part of the [fanbase-cli README](../README.md).

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

## How much of a parser the files reach: `--coverage`

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

## Not evaluating again what has not changed: `--reuse`

With a fixed seed and the same parsers, evaluating a spec again can only give the same result (but for how fast it went, which is
the machine's). `--reuse` leaves that out:

```bash
fanbase evaluate --all --seed 1 --coverage --json-file evaluation.json --reuse https://github.com/acme/specs/releases/latest/download/quality.json
fanbase quality build evaluation.json -o quality.json      # the specs that were not evaluated again keep what was found
```

Each spec in `quality.json` carries a **fingerprint** of everything its evaluation depended on:

- the spec **and everything it extends** (when `png` changes, so does `png-apng`), by the hash of each file;
- each **target** that judges it: every file of the target's folder, and the **version** the parser reports (a parser that is
  upgraded, or that is not installed any more, is a change);
- **Fandango's** version and **fanbase's**;
- the settings that change what comes out: the number of inputs, the **seed**, the time Fandango gets, whether coverage is
  measured and after how many inputs.

A spec whose fingerprint is the same as in the earlier results is not asked of Fandango or of the parsers: its earlier result is
kept, and the report says so. `decodes` is read from the spec as it is now (it is metadata, not part of the grammar), so a changed
expectation is checked against the kept result without evaluating anything (`--strict` sees it).

Things to know:

- **Use one seed.** The seed is part of the fingerprint. A seed that changes every week, as in the registry's workflows until now,
  makes every spec a change every week.
- **A full evaluation is still the way to be sure.** The fingerprint does not know the Python packages a spec imports, nor
  anything about the machine that no version string says. Evaluate without `--reuse` at each release.
- **Speed** is the machine's: a reused result keeps the speed it was measured at.
- **The private record** (`--incidents`) holds what was found in the specs that were evaluated; a bug found in a spec that was
  not is in the record of the run that found it.
- Results that cannot be read are not an error: everything is evaluated, and it says so.

## Choosing between grammars: `quality.json` and `--compare-with`

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
