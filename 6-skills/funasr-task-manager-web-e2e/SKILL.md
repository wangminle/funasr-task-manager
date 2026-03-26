---
name: funasr-task-manager-web-e2e
description: Build, run, or maintain browser-based end-to-end tests for the funasr-task-manager project. Use when Codex needs to simulate a real user dragging audio or video files into the web upload page, creating a batch of transcription tasks, waiting for scheduler execution, and validating that the system returns reasonable recognition results with saved test artifacts.
---

# FunASR Task Manager Web E2E

Use this skill for the project's real browser workflow, not for API-only validation.
Existing `pytest` E2E cases under `4-tests/scripts/e2e/` cover backend flows; this skill covers the missing path: browser upload, batch task creation, task-list observation, result download, and artifact archiving.

Current profile guidance:

- `smoke`: smallest fast regression for daily edits
- `remote-standard`: remote FunASR recommended batch, fixed to the five smallest files by size
- `standard`: heavier local/pre-merge coverage, may include 100MB+ assets
- `full`: all available assets

## Quick Start

1. Read [references/project-context.md](references/project-context.md) before changing or running browser E2E.
2. Prefer the frontend `npm run test:e2e:prepare:*` commands to build fixture batches because they already handle Python command differences across platforms.
3. Reuse existing frontend/backend commands when possible; do not invent a second app layout.
4. Prefer Playwright for browser automation if a browser test harness must be added or repaired.
5. Save run artifacts under `7-data/outputs/e2e/<timestamp>/`.
6. Keep the official Playwright project under `3-dev/src/frontend/`; do not add a second root-level `playwright.config.*`, `tests/example.spec.*`, or `e2e/example.spec.*` scaffold.

## Workflow

### 0. Detect platform and adapt commands

Before running any command, detect the current platform and choose the matching command style.

1. Detect OS with `node -p "process.platform"` or Python `platform.system()`.
2. On Windows PowerShell:
	- prefer `python` over `python3`
	- set variables with `$env:NAME='value'`
3. On macOS/Linux shells:
	- prefer `python3`, fall back to `python` only if needed
	- set variables with `NAME=value command` or `export NAME=value`
4. When running from `3-dev/src/frontend/`, prefer the existing `npm run test:e2e:*` entry points because they already normalize Python lookup and `ASR_E2E_PROFILE` handling.
5. Record the detected platform in run artifacts.

### 1. Choose the right goal

- If the user asks to design or improve the repeatable browser E2E strategy, update the Playwright flow, fixture selection, and artifact rules first.
- If the user asks to run a quick regression, use the `smoke` batch unless they explicitly want broader coverage.
- If the user is using a remote FunASR node, bandwidth is limited, or they want a stable five-file regression, use `remote-standard`.
- If the user asks for pre-merge or release confidence on a local or higher-bandwidth environment, use `standard` or `full` and widen assertions.

### 2. Build the fixture batch

Run the helper script instead of hand-picking files:

```bash
cd 3-dev/src/frontend
npm run test:e2e:prepare:smoke
npm run test:e2e:prepare:remote-standard
npm run test:e2e:prepare:standard
npm run test:e2e:prepare:full
```

If you must run the Python helper directly from the repository root, split examples by shell:

```bash
python3 6-skills/funasr-task-manager-web-e2e/scripts/build_fixture_batch.py --profile smoke
python3 6-skills/funasr-task-manager-web-e2e/scripts/build_fixture_batch.py --profile remote-standard --write
python3 6-skills/funasr-task-manager-web-e2e/scripts/build_fixture_batch.py --profile standard --write
python3 6-skills/funasr-task-manager-web-e2e/scripts/build_fixture_batch.py --profile full --output 7-data/outputs/e2e/full-batch.json
```

```powershell
python 6-skills/funasr-task-manager-web-e2e/scripts/build_fixture_batch.py --profile smoke
python 6-skills/funasr-task-manager-web-e2e/scripts/build_fixture_batch.py --profile remote-standard --write
python 6-skills/funasr-task-manager-web-e2e/scripts/build_fixture_batch.py --profile standard --write
python 6-skills/funasr-task-manager-web-e2e/scripts/build_fixture_batch.py --profile full --output 7-data/outputs/e2e/full-batch.json
```

Use the output as the source of truth for:

- which files enter the run
- why those files were chosen
- where artifacts should be stored

### 3. Preflight environment check

Before running any browser test, verify all dependencies:

1. Backend health: `GET http://localhost:8000/health` → `status: "ok"`.
2. Frontend reachable: `GET http://localhost:5173` → HTTP 200.
3. At least one FunASR server registered: `GET /api/v1/servers` → `len(servers) > 0`.
4. Test data exists: `7-data/assets/1-测试audioFiles/` contains audio/video files.

If any check fails, stop and report which prerequisite is missing rather than proceeding to a confusing browser failure.

### 4. Prepare the environment

- Prefer the project's existing local dev workflow: backend on `http://localhost:8000`, frontend on `http://localhost:5173`.
- Reuse current commands from the repository instead of inventing wrappers.
- If browser automation dependencies are missing, prefer adding them in `3-dev/src/frontend/` and keep browser test code close to the frontend toolchain.
- Keep browser test outputs, screenshots, traces, result snapshots, and run summaries under `7-data/outputs/e2e/`.
- Treat `7-data/outputs/e2e/`, `3-dev/src/frontend/test-results/`, and `3-dev/src/frontend/playwright-report/` as local artifacts, not source files to commit.
- If no FunASR server exists yet, provide `ASR_E2E_SERVER_HOST` and `ASR_E2E_SERVER_PORT`; otherwise the test will reuse the first already-registered server.

### 5. Simulate the real user path

Drive the same visible workflow a user follows:

1. Open `/upload`.
2. Add multiple files through the upload control.
3. Set shared task options such as language and ASR flags.
4. Click `提交转写`.
5. Verify the created task count matches the selected file count.
6. Open `/tasks`.
7. Poll until tasks reach terminal states (SUCCEEDED / FAILED / CANCELED). Use per-profile timeouts from [references/project-context.md](references/project-context.md). The backend also exposes SSE at `GET /api/v1/tasks/{id}/progress`; prefer API polling at 5–10 second intervals.
8. Download or fetch text results for validation.

Prefer `input[type=file].setInputFiles(...)` when using Playwright.
Element Plus drag-and-drop ultimately feeds the same upload component state, and direct file assignment is more stable than synthetic drag events.
Only simulate literal drag events when the task explicitly requires verifying drag-only behavior.

### 6. Validate in layers

Apply assertions in this order:

1. Hard gate: page loads, upload succeeds, tasks are created, tasks leave the queue, terminal states are reachable, results are downloadable.
2. Structural gate: each successful task returns non-empty text or JSON text content, and the number of finished results matches expectations.
3. Semantic gate: if a baseline exists for a file, verify expected keywords or phrases; if no baseline exists, record the transcript and flag it for review instead of inventing strict text equality.

Do not require exact transcript equality unless the project already has a maintained golden baseline for that file and model combination.

### 7. Report and archive

Always leave behind machine-readable and human-readable artifacts:

- selected fixture manifest
- final task status summary
- transcript snapshots or downloaded `.txt` results
- screenshots or Playwright traces when a browser flow fails
- a concise run summary that states which profile ran, how many files succeeded, and what needs follow-up

## Browser Test Implementation Rules

- Prefer Playwright over Cypress for new browser E2E in this repository.
- Prefer locating elements by visible text or stable attributes you add deliberately; avoid brittle CSS chains.
- If a page lacks stable selectors, add minimal `data-testid` hooks in the app rather than writing fragile locators.
- Keep test logic deterministic: use explicit waits on API results or UI state, not arbitrary sleeps.
- Keep generated browser tests focused on the upload-to-result workflow; do not bundle unrelated settings or monitoring coverage into the same scenario.

## Result Quality Rules

- Treat empty transcripts, unreadable garbage, or missing downloads as failures.
- Treat wording drift as a warning when no curated baseline exists.
- When a file is known to be Chinese speech, prefer checking that the transcript contains Chinese text and expected anchor words if available.
- When a file is video, verify that the system still produces a transcription result instead of failing in preprocessing.

## Platform Decision Tree

1. Run `node -p "process.platform"`.
2. If the result is `win32`:
	- use PowerShell syntax like `$env:ASR_E2E_SERVER_HOST='127.0.0.1'`
	- use `python` or the frontend `npm run test:e2e:*` scripts
3. If the result is `darwin` or `linux`:
	- use `ASR_E2E_SERVER_HOST=127.0.0.1 command`
	- use `python3` or the frontend `npm run test:e2e:*` scripts
4. Prefer `npm run test:e2e:smoke` and related commands over ad-hoc shell one-liners whenever possible.

## References

- Read [references/project-context.md](references/project-context.md) for routes, page behavior, fixture policy, artifact locations, and acceptance strategy.
