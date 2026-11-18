# Fuzzing your program

Part of the [fanbase-cli README](../README.md).

`fanbase fuzz` fuzzes a program of your own with inputs Fandango makes from Fanbase specs: what makes it crash,
hang or be killed, and what it gets wrong, goes into a private record, and what is printed is counts. It is what
the [Fandango GitHub Action](https://github.com/fandango-fuzzer/fandango-action) runs in a repository's CI, and you
can run the same on your machine:

```bash
fanbase fuzz json json-boundary --target-dir .fandango/targets --record ~/fuzz-private          # on your machine
fanbase fuzz json --target-dir .fandango/targets --only-own-targets --record DIR --recipients keys.txt   # in CI
```

- **What is fuzzed.** A *target* says how to run your program on one file: a folder `NAME/` with a `target.yml`
  and, usually, a small harness that exits 0 when the program accepts the file (see
  [Evaluating a spec](evaluating.md) for `target.yml`). Every target in `--target-dir` that reads the spec's format
  is fuzzed, unless `--targets` names some. With `--only-own-targets`, which the action always passes, targets come
  only from `--target-dir`, each has to run a harness of its own folder (`{dir}/...`), and a folder that links out of
  `--target-dir` is refused: it fuzzes your program, not somebody else's.
- **What is found.** A target that crashes, hangs or is killed is an *incident*, written down as with
  `evaluate --incidents` (see [The private record](private-record.md)). An input that every target rejects although
  the spec says its files always decode, or, with two or more targets, one that they disagree on, is a *finding*.
  Findings are grouped by what each target said, and the smallest input of each group is reduced (lines, then
  characters, for `--reduce-budget` seconds) to one that gets the same answers.
- **Where it goes.** Into one record with the incidents: `findings/<id>/` holds a report (what each target said, and
  how to make the inputs again), the reduced input and the original one. On your machine it is a folder only you can
  read, written only if something was found; with `--recipients` it is one file, `fuzz-private.age`, encrypted to
  the keys in that file and written on every run. `fanbase incidents open` reads it.
- **What is printed** is counts: for each target, the inputs it accepted, rejected, or failed on (an incident is
  counted as a failure, as a failing harness is), and how many kinds of finding there were. `--json-file` writes the
  same counts as JSON, `--markdown` prints them for a CI job summary, and `--fail` exits 1 when anything was found.
