# FluentMon progress log

Last updated: 2026-11-27. Written so other agents can pick up the work without the chat history.

## Where things stand

- **Branch:** `main` holds all merged work. PRs #1 to #7 are merged (see "Repo history"). Nothing from the work below is uncommitted in the repo.
- **App:** the iPad client (SwiftUI, iOS 17+, bundle `dev.local.fluentmon`) builds for the iPad Air 11-inch (M2) simulator and runs in mock and replay mode. It has not been run against a real server.
- **Project file:** `FluentMon.xcodeproj` is generated from `project.yml` with xcodegen. Run `xcodegen generate` after adding files.
- **Server:** the Windows server is in `src/fluentmon/` (merged in PR #3, layout fixed in PR #7). The Linux test suite passes: 426 passed, 4 skipped (the skips need a real Windows PC).
- **Installer:** `installer/install.ps1` and `fmon setup`/`doctor`/`uninstall` are built. The first real install on a Windows PC failed on the package layout. The fix is merged (PR #7). A successful real install is not recorded in the repo.
- **Fixtures:** real LHM data (`lhm_amd.json`, AMD Ryzen 7 7800X3D) and four real Fluent transcripts are in `tests/fixtures/` (PRs #5 and #6).

## Repo history

| PR | Branch | Merged | What |
| --- | --- | --- | --- |
| #1 | `ui-ipad-prototype` | 2026-11-25 | iPad frontend prototype |
| #2 | `ui-ipad-prototype` | 2026-11-25 | Plan section 14 (installer), sections 15 and 16 (server and installer tasks) |
| #3 | `backend-installer` | 2026-11-26 | Server, installer and iPad glass refinements |
| #5 | `claude/epic-einstein-i4f9qx` | 2026-11-26 | LHM web server on 127.0.0.1, real AMD LHM fixture |
| #6 | `claude/epic-einstein-i4f9qx` | 2026-11-26 | Real steady, transient, diverging and report-definition Fluent transcript fixtures |
| #7 | `claude/epic-einstein-i4f9qx` | 2026-11-27 | Python package moved to `src/fluentmon/` (Windows install failed with `No module named 'fluentmon'`) |

## Plan and rules

- `plans/fluentmon-plan.md`: the spec. Section 6 is the wire protocol, section 3 the progress-bar math, section 12 the changelog, sections 13 (iPad), 15 (server) and 16 (installer) the implementation tasks.
- `plans/ui-agent-instructions.md`: the UI tasks U1–U7 and F1–F13.
- `RULES.md`: branch before committing on the default branch, no comments in source files, changelog entry for interface or file-layout changes.
- **Plan deviation to resolve:** section 3 (line 160) says transient runs use a per-step reference for the convergence bar. The app uses a whole-run reference (see "Convergence bar" below).

## Architecture (app)

- `FluentMon/Layout/`: `DashboardGeometry`, `BarMetrics`, `SceneProbe`. Pixel layout, scene state.
- `FluentMon/Net/`:
  - `Messages.swift`: Codable wire types and `decodeIncoming`.
  - `MonitorClient.swift`: WebSocket client, reconnect backoff, token rejection (close code 4401).
  - `MockClient.swift`: generated mock run, or replay of a solver log.
  - `Discovery.swift`: Bonjour browse (`_fluentmon._tcp`) and the `fluentmon://` pairing URL.
  - `FluentLog.swift`: parser for Fluent console logs.
- `FluentMon/Model/`: `MonitorStore` (state and derived bars), `SeriesBuffer` (decimation), `ConsoleBuffer`, `Keychain`, `Palette`.
- `FluentMon/Views/`: `DashboardView`, `ArcMeter`, `ProgressBars`, `ResidualChart`, `HeaderBand`, `ConsoleDrawer`, `ConnectSheet`, `SettingsSheet`.
- `FluentMon/FluentMonApp.swift`: `AppModel` and `RootView`. Mock mode is chosen when the `mock` user default is set.
- `FluentMon/Previews/`: canvas previews and `SampleData`.

## Server (Python)

- `src/fluentmon/`: config, transcript parser and session manager, progress math, WebSocket hub and coalescer, system sampler, launcher, notifications, discovery, pairing, doctor, and the installer under `setup/`.
- `tools/replay.py`: replays a Fluent transcript into a watched folder.
- `installer/install.ps1`: bootstrap script. It installs `uv`, runs `uv tool install` from the repository, then runs `fmon setup`.
- Run the tests with `pip install -e '.[dev]'` and `pytest -q`. Python 3.12 or later.

## Mock and replay mode

- `-mock YES` starts `MockClient`. Without `-mockLog`, it generates a 1000-iteration run.
- `-mockLog <path>` replays a Fluent console log instead.
  - `FluentLog` parses the file once. Each data row keeps the solver state at that row (step, steps left, flow time, step-start row, line count, best residual so far).
  - `MockClient.replayStep` feeds 24 rows every 0.1 s, with the matching bars and console lines. When the log ends it sends the trailing console lines and a `session_end` event.
  - The app takes about 5–10 s to show anything on launch while the log parses.
- Launch example (simulator):

  ```bash
  xcrun simctl launch booted dev.local.fluentmon -mock YES -mockLog "/Users/.../fluent-monitor/log sample.txt"
  ```

- The shared scheme's Run action still passes `-mock YES` (see `FluentMon.xcscheme`, the `CommandLineArgument` block around line 91).

## Convergence bar

- The bar measures progress of the **least converged residual** toward 1e-6.
  - Reference: the largest residual in the first row of the log (9.66e-4). Span = log10(reference) + 6 decades (about 3.0 here). The code is `FluentLog.swift` line 146.
  - Current value: the lowest value the least converged residual has reached so far (`Progress.best`, `Progress.bestColumn`). It never moves back up.
- Earlier versions reset the reference at every time step. Step starts spike, so the bar jumped back and the decade count restarted. That is fixed.
- Open: the code is whole-run, and plan section 3 says per-step. Decide which one is right, then update the plan.

## Log data used for testing

- `log sample.txt` in the repo root (untracked, not in `.gitignore`, 1.2 MB, 29,999 lines). It is the console output of a transient run.
  - Iterations 4300 to 7377 (3078 rows). Time steps 723 to 3007 (flow time 0.0030 s).
  - 2285 "solution is converged" events, usually one per time step.
  - Continuity: starts at 1.59e-3, ends at 6.68e-6 (never reaches 1e-6).
  - Limiting residual at the end: k at 4.8e-5.
  - Reversed flow on pressure-outlet 16 grows from 2727 faces (71.2%) to 3187 faces (85.8%).
  - Ends with `Error: WorkBench Error: Could not handle event: CALCULATION-COMPLETE`, then `Calculation complete.`
  - Has no energy column. The first 4300 iterations are not in the file.
- `SolutionMonitor.gz` from Downloads (copied by the user from the RDC share): gzip data, contains six **report** monitors for zones 1, 14, 15, 16, 5 and 201, sampled at 877 points each. It does **not** contain the residuals and is not used by the app.
- Server-side real fixtures are in `tests/fixtures/` (see "Where things stand").

## Tests

- **Server:** `pytest -q` gives 426 passed, 4 skipped on Linux, Python 3.13 (PR #7 state). The skipped tests are `tests/test_winbackend.py` and need a real Windows PC.
- **iPad:** `FluentMonTests/` has 37 test functions across 10 files: `DashboardGeometryTests`, `HeaderBandTests`, `ConsoleBufferTests`, `MessagesTests`, `MonitorStoreTests`, `SeriesBufferTests`, `PairingURLTests`, `KeychainTests`, `MonitorClientTests`, and `FluentLogTests` (4 tests).
- The last recorded full run with a result was 37 tests, 1 failure, which was `FluentLogTests.testStepStartRowIsFirstRowOfLatestStep`. The expectation was wrong and has been fixed. The 4 `FluentLogTests` passed after the fix.
- **Not rerun since:** the streaming replay refactor, the whole-run convergence change and the `Progress` change. This update did not run the Swift tests (no Xcode in the cloud session). Run the suite with `xcodebuild` before trusting it.
- An earlier run showed 7 failures (Keychain round trip, `MonitorStore` meter indexes). They did not appear in the 37-test run. Not investigated further.

## Tooling gotchas

- `xcodebuild` can ignore `timeout`'s SIGTERM and keep running. Kill it by PID, then rerun with output to a log file. Check with `ps`.
- SourceKit shows "Cannot find type" and "No such module XCTest" for single files. These are noise. The real signal is `xcodebuild`.
- The iOS Simulator panel was attached with the `attach` action of the simulator tool. Launch with args uses `xcrun simctl`, because the tool's `launch` does not pass arguments.
- `gh pr list` fails in Claude Code sessions (GraphQL is blocked). Use `gh api repos/scerial/fluent-monitor/pulls`.
- `git push` from a Mac terminal was blocked by the cloud sandbox during the 2026-11-26 session. Pushes from the cloud clone worked.

## Repo state

- Untracked on the Mac only: `.DS_Store`, `log sample.txt`. Neither is in `.gitignore`.
- The `prototype/` directory and `FluentMon/Stubs/StandInTypes.swift` from the earlier uncommitted state are gone from `main`.

## Open items

1. Decide the convergence reference: whole run (code) or per-step (plan section 3). Update the plan either way.
2. Rerun the iPad unit tests with `xcodebuild` after the last three changes (see "Tests").
3. Plan section 12 changelog is missing `FluentMon/Net/FluentLog.swift`, the `FluentLogTests` file and the new Python-side entries for the transcript fixtures. Refinement stage to add them.
4. Decide whether `-mock YES` should stay in the scheme's Run action.
5. Verify Bonjour discovery, `fluentmon://` pairing via the system, the denied-permission state, the settings sheet, and the "Live ↓ N new" pill. None are verified on a device or a real server.
6. Run `fmon setup` on a Windows PC after PR #7 and record the result in the repo or the plan. Until then, the installer's real-PC path is unverified.
7. Add `.DS_Store` and `log sample.txt` to `.gitignore`, or commit the log if it is wanted in the repo.
