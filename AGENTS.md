# Repository Guidelines

## Project

- This repository contains an Azure Functions application.
- All HTTP-triggered functions must use function-level authorization (`AuthLevel.FUNCTION`) by default. Anonymous access requires an explicit, documented exception.
- Keep deployable application and configuration files in version control, including `host.json` and `.funcignore` when present.
- Never commit secrets, credentials, `local.settings.json`, local authentication helpers, or generated Azure and Azurite state.
- Follow the language, runtime, package manager, and project structure established by the application scaffold. Document material changes to those choices.

## Development workflow

- Use `dev` as the integration branch and `main` as the release branch.
- Create feature branches from the latest `dev` using `<ticket-number>-<kebab-case-description>`.
- Target feature pull requests to `dev`; do not target `main` directly from a feature branch. Use `dev` as the only source for release pull requests to `main` and synchronization pull requests to active feature branches.
- During the R&D phase, run pull-request quality CI for changes targeting `dev`. Add a separate `main` quality gate when `main` gains its own deployment workflow or release-validation requirements.
- Include `Refs #<ticket-number>` in feature commit messages and pull request descriptions so GitHub links the work to its issue.
- Assign pull requests to the authenticated GitHub user.

## Validation

- Run the checks relevant to the affected code before committing or opening a pull request.
- Report any checks that could not be run, and never claim that an unexecuted check passed.
- Preserve unrelated user changes and do not rewrite Git history unless explicitly requested.

## AI tooling

- Keep shared AI instructions and workflows vendor-neutral.
- Store reusable skills under `.agents/skills/<skill-name>/SKILL.md`.
- Keep optional vendor-specific metadata isolated from the core `SKILL.md` workflow. Do not add repository-level vendor configuration directories such as `.codex/` unless explicitly required.
- `.agents/references/codex-agents-md.md` is a local reference copy of the Codex `AGENTS.md` documentation. Consult it only when creating or updating Codex instruction files; it is not an active instruction file. Prefer the official documentation when it is accessible.

## Repository skills

- Use the `commit` skill only when explicitly asked to commit and push changes.
- Use the `handoff-script-gen` skill to prepare a new private local handoff for the current ticket branch; never stage or commit its output.
- Use the `handoff-script-pickup` skill to resume work from the newest private local handoff without modifying repository state.
- Use the `pr-to-dev` skill to open a pull request from the current ticket branch to `dev`.
- Use the `pr-to-main` skill to open a promotion pull request from `dev` to `main`.
- Use the `sync-local-settings-example` skill to copy local Azure Functions setting names into the tracked example without exposing real values.
- Use the `sync-from-dev` skill to open a synchronization pull request from `dev` to the current ticket branch.
