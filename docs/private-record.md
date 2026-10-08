# The private record of crashes and hangs

Part of the [fanbase-cli README](../README.md).

Hiding a crash from a public report is not the same as losing it: whoever runs the evaluation is the one who has to
report the bug. So `evaluate` can write the details down, for you alone:

```bash
fanbase evaluate --all --incidents ~/fanbase-private                  # on your machine
fanbase evaluate --all --hide-crashes --incidents DIR --incident-recipients recipients.txt     # in CI
```

- **What is in it.** For each cause (a bug found forty times is one entry): the input (three examples), the command
  with the file as `INPUT` (written without this machine's paths, with the small files of the target that call the parser
  in `harness/`, once for each target, so that the vendor can see how it was called), how it ended (the signal), what the target said, whether it happened again when tried
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

Once a record is open, these keep what is to be done about it straight:

```bash
fanbase incidents list opened/                          # what broke, and where each incident stands
fanbase incidents show opened/ imagemagick-crash        # the note to the vendor (the start of an id will do)
fanbase incidents track opened/ imagemagick-crash --reported today --vendor ImageMagick --reference "issue 4711"
fanbase incidents verify opened/ imagemagick-crash      # run the kept input against the parser as it is now: still broken?
fanbase incidents track opened/ imagemagick-crash --fixed-in 7.1.2 --fixed-on 2026-11-20
```

Every `track` is noted twice: in the record's own `tracking.yml`, and in a file of yours, `incidents.yml` in your data folder
(`$FANBASE_INCIDENTS`, else `$XDG_DATA_HOME/fanbase`, else `~/.local/share/fanbase`; mode 0600, never in a registry). The same bug
found again next week is the same incident (its id says the spec, the target and the cause), so the new record already
shows it as reported ("tracked in an earlier record"), and what you add there is added to what was known. `fanbase incidents
tracked` lists every incident you have noted anything about, whichever record it was in, the soonest to be public first
(`--within 30` for what may be public within a month); `track --forget` takes one out of both; `--no-global` keeps a note in the
record only.

`track` keeps the usual clock (OSS-Fuzz, Project Zero): a bug may be made public 90 days after it was reported, or 30 days after
it was fixed if that comes first; `list` shows the date for each. It is a reminder, not a rule: fanbase never makes anything
public. What is tracked is in `tracking.yml` in the record's folder (mode 0600). `verify` runs the input an incident kept
with the target as the registry checkout defines it, and the limits of `evaluate`, and says whether it still crashes or hangs, and
with which version; it does not note a fix for you. Run it where you would run that parser on a file that is a bug.

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
