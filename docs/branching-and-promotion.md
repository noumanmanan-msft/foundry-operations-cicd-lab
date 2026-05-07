# Branching and Promotion Flow

This repository uses a PR-first promotion model.

## Flow
- Work happens on feature branches.
- Changes merge into `main` via pull request.
- Push to `main` triggers `deploy-dev-foundry`.
- Successful Dev deployment triggers `promote-foundry-qa`.

## Manual QA override
- `promote-foundry-qa` can also be run manually with inputs:
  - `agent-name`
  - `agent-version`
  - `git-ref`
