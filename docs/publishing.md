# Signing, browsing and citing a registry

Part of the [fanbase-cli README](../README.md).

## Signed registries: `fanbase sign`, `--signer`, `fanbase verify`

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
one of them to be added), and kept in the user's config; the public registry's will be in the CLI itself, and are checked for a
release or a commit of it (`--registry .../tree/<tag>`), not for `main`, the default, which moves with every merge. A registry that
has keys pinned is refused, with nothing in it believed, if its index is not signed, is signed by another key, or was changed
after it was signed. Only modern keys (ed25519, ecdsa, security keys) are taken, and only the key itself, not a comment.

What a signature does not say is that the index is the newest: an old index, signed, stays signed. To read a registry as it was
at a release, point at the release (`.../tree/<tag>`), which does not move; a registry that signs only its releases is read
verified that way. fanbase never reads a private key: `ssh-keygen` does the signing, and asks for the passphrase or the touch.

## A site to browse the registry: `fanbase site`

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

## A DOI for a spec: `fanbase doi`

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
