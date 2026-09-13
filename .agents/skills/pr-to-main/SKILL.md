---
name: pr-to-main
description: Create a GitHub pull request from dev to main when the user explicitly requests a promotion or release PR, automatically restoring the original branch afterward.
---

# Pull Request from Dev to Main

Create the pull request only when the user explicitly invokes this workflow. This skill creates a PR; it does not merge it.

## Required workflow

1. Record the current branch as the original branch. Require a named branch and a clean working tree before switching; if local changes are present, stop rather than stashing, committing, or carrying them across branches.
2. Fetch `main` and `dev`, then confirm both branches exist on the remote. If the original branch is not `dev`, switch to the local `dev` branch. If it does not exist locally, create it as a tracking branch for `origin/dev`.
3. Once a switch occurs, treat restoration as required cleanup: switch back to the original branch before reporting success, an existing PR, no changes, or any failure. If restoration fails, report that prominently and do not claim the workflow completed cleanly.
4. Inspect the remote configuration, commits, and complete diff from `origin/main` to `origin/dev`. Confirm that `dev` contains changes not already in `main`. Stop rather than guessing alternate branches or creating an empty PR.
5. Synchronize local `dev` with `origin/dev`. If local `dev` is behind, run `git pull --ff-only origin dev`. If local `dev` is ahead, push it normally. If it has diverged, or if the fast-forward pull fails, stop and report the synchronization problem. Never create a merge commit during synchronization and never force-push.
6. Before a required push, use the platform-specific authentication helper when present:
   - Linux or WSL: `./.agents/skills/commit/scripts/local-auth.sh`
   - Windows PowerShell: `& .\.agents\skills\commit\scripts\local-auth.ps1`

   If the applicable helper is missing, print a clear warning identifying its path and continue. If it exists but fails, stop without pushing or creating the PR.
7. Build the PR description from all commits and changes in `origin/main...origin/dev`, not only the latest commit. Collect and deduplicate every GitHub issue reference represented by those changes, including `Refs #<number>` lines from commit messages. Verify referenced issues when possible; never invent an issue number. If none are found, state that clearly and continue.
8. Check for an existing open PR from `dev` to `main`. If one exists, do not create a duplicate; restore the original branch and report the PR URL.
9. Resolve the currently authenticated GitHub user. Create a non-draft PR assigned to that user, with `dev` as the head and `main` as the base. Derive a concise title from the complete change set. The body must summarize all included work, state the validation performed, and list each associated issue as `Refs #<number>` on its own line. Use `Refs`, not an auto-closing keyword.
10. Verify the PR number, URL, title, assignee, head branch, base branch, and all referenced issues. Restore the original branch, verify that restoration succeeded, and report the result.

## Pull request body shape

```markdown
## Summary

- Summarize all work being promoted from dev
- Highlight relevant implementation decisions

## Validation

- List tests or checks that were run

## Related issues

Refs #1
Refs #2
```
