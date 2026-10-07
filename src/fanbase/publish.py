"""`fanbase publish`: take what you changed in a registry checkout to a pull request.

A registry is a git repository, and the way a spec gets into the public one is a pull
request, which the maintainers review. This command does the mechanical part: it checks the
checkout, makes a branch, commits the specs (and the index that goes with them), pushes the
branch and opens the pull request with `gh`. It says what it is about to do first, and
asks, because pushing and opening a pull request cannot be taken back quietly.

It only ever stages `specs/`, `index.yml` and `registry.yml`. The commit is made as you,
with your git settings.
"""

from __future__ import annotations

import re
import shutil
import sys
from dataclasses import dataclass, field

from fanbase.context import Context
from fanbase import contrib
from fanbase.contrib import checkout, refresh, run_checks
from fanbase.output import clean
from fanbase.registry import RegistryError

TRACKED = ("specs", "index.yml", "registry.yml")


@dataclass
class Plan:
    added: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    branch: str = ""
    new_branch: bool = False
    base: str = "main"
    message: str = ""
    body: str = ""

    @property
    def specs(self) -> list[str]:
        return [*self.added, *self.updated, *self.removed]


def changed_specs(status: bytes | str) -> Plan:
    """The specs that a `git status --porcelain=v1 -z -uall` listing says were added, updated
    or removed. A spec is added or removed if its .fan file is; any other change is an update."""
    text = status.decode("utf-8", "replace") if isinstance(status, bytes) else status
    seen: dict[str, str] = {}
    for entry in text.split("\0"):
        if len(entry) < 4 or entry[2] != " ":
            continue  # the old name of a rename follows the entry, without a status
        code, parts = entry[:2], entry[3:].split("/")
        if len(parts) < 4 or parts[0] != "specs":
            continue
        spec = f"{parts[1]}/{parts[2]}"
        if parts[3] == f"{parts[2]}.fan":
            seen[spec] = "removed" if "D" in code else "added" if code == "??" or code.startswith("A") else "updated"
        else:
            seen.setdefault(spec, "updated")
    plan = Plan()
    for spec, what in sorted(seen.items()):
        getattr(plan, what).append(spec)
    return plan


def _slug(plan: Plan) -> str:
    first = (plan.added or plan.updated or plan.removed)[0].replace("/", "-")
    verb = "add" if plan.added else "update" if plan.updated else "remove"
    return re.sub(r"[^a-z0-9-]+", "-", f"{verb}-{first}".lower()).strip("-") + ("" if len(plan.specs) == 1 else "-and-more")


def _message(plan: Plan) -> str:
    if len(plan.specs) == 1:
        verb = "Add" if plan.added else "Update" if plan.updated else "Remove"
        return f"{verb} {plan.specs[0]}"
    parts = [f"{verb} {len(group)}" for verb, group in (("add", plan.added), ("update", plan.updated), ("remove", plan.removed)) if group]
    return "Specs: " + ", ".join(parts)


def _body(plan: Plan, checked: str) -> str:
    lines = ["## What", ""]
    for verb, group in (("Add", plan.added), ("Update", plan.updated), ("Remove", plan.removed)):
        lines += [f"- {verb} `{spec}`" for spec in group]
    lines += ["", "## Checks", "", f"- `fanbase check`: {checked}", "", "Opened with `fanbase publish`."]
    return "\n".join(lines)


def cmd_publish(args, ctx: Context) -> int:
    reg = checkout(ctx)
    root = str(reg.root)

    def git(*cmd: str):
        return contrib._run(["git", "-C", root, *cmd], capture_output=True, text=True)

    inside = git("rev-parse", "--is-inside-work-tree")
    if inside.returncode != 0 or inside.stdout.strip() != "true":
        raise RegistryError(f"{root} is not a git checkout")
    origin = git("remote", "get-url", "origin")
    if origin.returncode != 0:
        raise RegistryError("the checkout has no remote called origin to push to")

    refresh(reg)  # the metadata and the index that go with the specs are part of the change
    paths = [p for p in TRACKED if (reg.root / p).exists()]
    status = contrib._run(["git", "-C", root, "status", "--porcelain=v1", "-z", "-uall", "--", *paths], capture_output=True)
    plan = changed_specs(status.stdout)
    if not plan.specs:
        raise RegistryError("nothing to publish: no spec has been added, changed or removed")

    report = run_checks(reg, [], count=args.count, generate_inputs=not args.no_generate, say=lambda line: print(line, file=sys.stderr))
    for line in report.warnings:
        print(clean(f"warning: {line}"), file=sys.stderr)
    if report.failures:
        for line in report.failures:
            print(clean(f"FAILED:  {line}"), file=sys.stderr)
        raise RegistryError("not publishing: `fanbase check` found problems (above)")
    checked = f"{report.checked} specs looked at, {report.generated} produced inputs, nothing wrong"

    current = git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    head = git("symbolic-ref", "--short", "refs/remotes/origin/HEAD")
    plan.base = args.base_branch or (head.stdout.strip().removeprefix("origin/") if head.returncode == 0 and head.stdout.strip() else "main")
    plan.new_branch = bool(args.branch) or current in (plan.base, "HEAD", "")
    plan.branch = args.branch or (f"fanbase/{_slug(plan)}" if plan.new_branch else current)
    plan.message = args.message or _message(plan)
    plan.body = _body(plan, checked)

    print("publish plan:")
    for verb, group in (("add", plan.added), ("update", plan.updated), ("remove", plan.removed)):
        for spec in group:
            print(clean(f"  {verb:<7} {spec}"))
    print(f"  branch  {plan.branch} ({'new, from the current commit' if plan.new_branch else 'the one you are on'})")
    print(clean(f"  commit  {plan.message}"))
    print(f"  then    git push -u origin {plan.branch}" + ("" if args.no_pr else f", and open a pull request against {plan.base} with gh"))
    if args.dry_run:
        print("(dry run: nothing was done)")
        return 0
    if not args.yes:
        if not sys.stdin.isatty():
            raise RegistryError("not going ahead without your confirmation; pass --yes to give it")
        if input("Go ahead? [y/N] ").strip().lower() not in ("y", "yes"):
            raise RegistryError("not published")

    def must(done, what: str):
        if done.returncode != 0:
            raise RegistryError(f"{what} failed: {(done.stderr or done.stdout).strip()}")
        return done

    if plan.new_branch:
        must(git("switch", "-c", plan.branch), f"git switch -c {plan.branch}")
    must(git("add", "--all", "--", *paths), "git add")
    if git("diff", "--cached", "--quiet").returncode == 0:
        raise RegistryError("nothing is staged after git add; nothing to commit")
    must(git("commit", "-m", plan.message), "git commit")
    must(git("push", "-u", "origin", plan.branch), "git push")
    print(f"pushed {plan.branch}")

    if args.no_pr:
        return 0
    if shutil.which("gh") is None:
        print(f"gh is not installed, so no pull request was opened; open one from {plan.branch} on {clean(origin.stdout.strip())}")
        return 0
    pr = must(
        contrib._run(["gh", "pr", "create", "--base", plan.base, "--head", plan.branch, "--title", plan.message, "--body", plan.body],
             cwd=root, capture_output=True, text=True),
        "gh pr create",
    )
    print(pr.stdout.strip())
    return 0
