# Branching and Promotion Flow

This repository uses a PR-first promotion model.

## Required branch policy
- Make all day-to-day changes on `develop`.
- Open a pull request from `develop` into `main` for promotion.
- Do not push directly to `main`.
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
