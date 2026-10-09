# Releasing fanbase

A release is made by pushing a tag. Nothing else publishes: making a release by hand on GitHub does not upload to PyPI.

1. **Bump the version** in `pyproject.toml` and `src/fanbase/__init__.py` in a pull request, and merge it.
2. **Tag the commit on `main`** with an annotated tag whose message is the release notes (a short paragraph and a list of what changed):

   ```bash
   git fetch origin && git tag -a v0.6.0 -m "Notes of the release ..." origin/main
   git push origin v0.6.0
   ```

3. **Watch Actions.** `publish.yml` runs, in this order, and each step only if the one before passed:
   tests (Linux, macOS, Windows) → build (the tag must match the version, and the commit must be on `main`) → PyPI → the GitHub release,
   with the package attached and the message of the tag as its notes.

The GitHub release comes last because the Zenodo integration archives a release under a DOI that is **permanent** the moment it is
published, so a version that failed its tests or is not on PyPI never gets one. A tag that fails can be deleted and made again
(`git push origin :refs/tags/v0.6.0`) as long as nothing reached PyPI; a version on PyPI can never be uploaded again.

To try a change to `publish.yml` without releasing: run it by hand from Actions (it does the tests and the build and stops).
