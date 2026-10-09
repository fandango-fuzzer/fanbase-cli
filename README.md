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

## A first look

```bash
fanbase list                  # the formats in the registry
fanbase list png              # the specs of one format
fanbase show png-apng         # one spec's metadata
fanbase install png-apng      # copy it where Fandango's include() finds it
```

## What else it does

| You want to | Commands | Read |
|---|---|---|
| find, install and update specs | `list`, `search`, `show`, `install`, `update`, `uninstall`, `outdated`, `diff`, `cite` | [Using specs](https://github.com/fandango-fuzzer/fanbase-cli/blob/main/docs/using-specs.md) |
| run with exactly the specs you used before | `lock`, `install --locked` | [Repeating a run](https://github.com/fandango-fuzzer/fanbase-cli/blob/main/docs/using-specs.md#repeating-a-run-fanbaselock) |
| write a spec and contribute it | `new`, `fork`, `rebase`, `deps`, `check`, `changes`, `publish` | [Writing specs](https://github.com/fandango-fuzzer/fanbase-cli/blob/main/docs/writing-specs.md) |
| see how well a spec's files do against real parsers | `evaluate`, `targets`, `quality` | [Evaluating a spec](https://github.com/fandango-fuzzer/fanbase-cli/blob/main/docs/evaluating.md) |
| keep what a parser does on a bad file private | `incidents open`, `send`, `list`, `show`, `track`, `tracked`, `verify` | [The private record](https://github.com/fandango-fuzzer/fanbase-cli/blob/main/docs/private-record.md) |
| use another registry, or run your own | `registry`, `pin`, `unpin`, `reindex` | [Registries](https://github.com/fandango-fuzzer/fanbase-cli/blob/main/docs/registries.md) |
| sign a registry, put it on a site, give a spec a DOI | `sign`, `verify`, `site`, `doi` | [Signing, browsing and citing](https://github.com/fandango-fuzzer/fanbase-cli/blob/main/docs/publishing.md) |

`fanbase --help` lists every command, and `fanbase COMMAND --help` its options.

## Citing

fanbase-cli is archived on Zenodo. The DOI [10.5281/zenodo.23259520](https://doi.org/10.5281/zenodo.23259520) stands for the software
as a whole and always leads to the latest version; each release has a DOI of its own, on that page.

```bibtex
@software{fanbase_cli,
  author  = {Zamudio Amaya, Jos{\'e} Antonio},
  title   = {fanbase-cli: the command-line client for the Fanbase registry of Fandango input specifications},
  doi     = {10.5281/zenodo.23259520},
  url     = {https://doi.org/10.5281/zenodo.23259520},
  license = {Apache-2.0}
}
```

The registry with the specs, [`fanbase`](https://github.com/fandango-fuzzer/fanbase), is archived separately.

## License

Apache-2.0, see [LICENSE](https://github.com/fandango-fuzzer/fanbase-cli/blob/main/LICENSE).
