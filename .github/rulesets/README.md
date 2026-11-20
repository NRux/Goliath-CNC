# Repository rulesets

These files are the source of truth for this repository's GitHub rulesets.
GitHub does not read them automatically. A repository admin imports each one
once, then re-imports it whenever the file changes.

## `protect-main.json`: rules for the default branch (`main`)

| Rule | Effect |
|---|---|
| Restrict deletions | `main` cannot be deleted. |
| Block force pushes | `main`'s history cannot be rewritten. |
| Require a pull request before merging | Every change reaches `main` through a PR. No direct pushes, including by admins, because the bypass list is empty. |
| 0 required approvals | This is deliberate. GitHub never lets a PR's author approve it, and this repo has one maintainer. Requiring an approval would block every merge. Raise it to 1 once a second maintainer exists. |
| Dismiss stale approvals on push | An approval stops counting if new commits are pushed after it. This has no effect at 0 approvals, but it's ready for when that changes. |
| Require conversation resolution | Open review threads, from people or review bots, must be resolved before merging. |

Deliberately **not** included yet:
- **Required status checks.** [CI](../workflows/ci.yml) now runs on pull
  requests, but this ruleset does not yet enforce named status checks. The
  maintainer should wait for all applicable checks before merging. Adding
  enforced check names is a separate ruleset change after verifying the
  workflow's reported contexts; documentation changes do not alter protection.
- **Required signed commits.** Commits made from the cloud development environment
  are unsigned, so their PRs could not be merge-committed into `main`.
- **Linear history.** It would forbid merge commits. That is a style choice, not a
  safety one.

## How to import

1. On GitHub, go to **Settings → Rules → Rulesets**.
2. Click **New ruleset → Import a ruleset** and choose `protect-main.json`.
3. Review the rules, then click **Create**.

To update the ruleset, edit the existing "Protect main" ruleset and keep the
file in sync. Do not delete active protection as an intermediate step. Confirm
the live settings after applying a change; editing this JSON alone does not
change GitHub.
