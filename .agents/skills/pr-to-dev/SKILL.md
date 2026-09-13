---
name: pr-to-dev
description: Create a GitHub pull request from the current ticket-numbered branch to dev when the user explicitly requests a PR to dev.
---

# Pull Request to Dev

Create the pull request only when the user explicitly invokes this workflow. This skill creates a PR; it does not merge it.

## Required workflow

1. Inspect the current branch, working tree, remote configuration, commits, and diff against `origin/dev`.
2. Extract the ticket number from the current branch's leading numeric prefix. For example, `1-set-up-project-boilerplate-and-development-configuration` maps to issue `#1`. Stop and ask the user for a corrected branch or ticket number if the branch does not begin with a number followed by `-`.
3. Confirm that `dev` exists on the remote and that the extracted issue exists in the current GitHub repository. Stop if either cannot be verified; never guess a different base branch or issue.
4. Ensure the current branch is available on the remote before creating the PR. Do not commit local changes as part of this workflow; report that uncommitted changes will not be included. If a push is needed, use a normal push and never force-push.
5. Before a required push, use the platform-specific authentication helper when present:
   - Linux or WSL: `./.agents/skills/commit/scripts/local-auth.sh`
   - Windows PowerShell: `& .\.agents\skills\commit\scripts\local-auth.ps1`

   If the applicable helper is missing, print a clear warning identifying its path and continue. If it exists but fails, stop without pushing or creating the PR.
6. Check for an existing open PR from the current branch to `dev`. If one exists, do not create a duplicate; report its URL instead.
7. Resolve the currently authenticated GitHub user. Create a non-draft PR assigned to that user, with the current branch as the head and `dev` as the base. Derive a concise title from the actual changes. The body must summarize the changes, state the validation performed, and include `Refs #<ticket-number>` on its own line. Use `Refs`, not an auto-closing keyword.
8. Verify and report the PR number, URL, title, assignee, head branch, base branch, and referenced issue.

## Pull request body shape

```markdown
## Summary

- Describe the important changes
- Explain relevant implementation decisions

## Validation

- List tests or checks that were run

Refs #1
```
