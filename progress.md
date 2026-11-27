# FluentMon progress log

Last updated: 2026-11-25. Written so other agents can pick up the work without the chat history.

## Where things stand

- **Branch:** the server, installer and app are committed on `main`.
- **Server:** the server has not been run end to end with the iPad.
- **Patch-up:** section 17 of `plans/fluentmon-plan.md` lists the patch-up tasks P1 to P8 and which were built and tested.
- **Project file:** `FluentMon.xcodeproj` is generated from `project.yml` with xcodegen. Run `xcodegen generate` after adding files.

## Plan and rules

- `plans/fluentmon-plan.md`: the spec. Section 6 is the wire protocol, section 3 the progress-bar math, section 12 the changelog.
- `plans/ui-agent-instructions.md`: the UI tasks U1–U7 and F1–F13.
- `RULES.md`: branch before committing on the default branch, no comments in source files, changelog entry for interface or file-layout changes.
- **Plan deviations to resolve:**
  - Section 3 says transient runs use a per-step reference for the convergence bar. The app currently uses a whole-run reference (see "Convergence bar" below). This needs a decision.

## Architecture (app)

- `FluentMon/Layout/`: `DashboardGeometry`, `BarMetrics`, `SceneProbe`. Pixel layout, scene state.
- `FluentMon/Net/`:
  - `Messages.swift`: Codable wire types and `decodeIncoming`.
  - `MonitorClient.swift`: WebSocket client, reconnect backoff, token rejection (close code 4401).
  - `MockClient.swift`: generated mock run, or replay of a solver log.
  - `Discovery.swift`: Bonjour browse (`_fluentmon._tcp`) and the `fluentmon://` pairing URL.
  - `FluentLog.swift`: parser for Fluent console logs (new, see below).
- `FluentMon/Model/`: `MonitorStore` (state and derived bars), `SeriesBuffer` (decimation), `ConsoleBuffer`, `Keychain`, `Palette`.
- `FluentMon/Views/`: `DashboardView`, `ArcMeter`, `ProgressBars`, `ResidualChart`, `HeaderBand`, `ConsoleDrawer`, `ConnectSheet`, `SettingsSheet`.
- `FluentMon/FluentMonApp.swift`: `AppModel` and `RootView`. Mock mode is chosen when the `mock` user default is set.
- `FluentMon/Previews/`: canvas previews and `SampleData`.

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

- Note: the shared scheme's Run action still passes `-mock YES` (see `FluentMon.xcscheme`).

## Convergence bar

- The bar measures progress of the **least converged residual** toward 1e-6.
  - Reference: the largest residual in the first row of the log (9.66e-4). Span = log10(reference) + 6 decades (about 3.0 here).
  - Current value: the lowest value the least converged residual has reached so far (`Progress.best`, `Progress.bestColumn`). It never moves back up.
- Earlier versions reset the reference at every time step. Step starts spike, so the bar jumped back and the decade count restarted. That is fixed.
- Open: whether this whole-run rule replaces the per-step rule in plan section 3 (see deviations above).

## Log data used for testing

- `log sample.txt` in the repo root (untracked, 1.2 MB, 29,999 lines). It is the console output of a transient run.
  - Iterations 4300 to 7377 (3078 rows). Time steps 723 to 3007 (flow time 0.0030 s).
  - 2285 "solution is converged" events, usually one per time step.
  - Continuity: starts at 1.59e-3, ends at 6.68e-6 (never reaches 1e-6).
  - Limiting residual at the end: k at 4.8e-5.
  - Reversed flow on pressure-outlet 16 grows from 2727 faces (71.2%) to 3187 faces (85.8%).
  - Ends with `Error: WorkBench Error: Could not handle event: CALCULATION-COMPLETE`, then `Calculation complete.`
  - Has no energy column. The first 4300 iterations are not in the file.
- `SolutionMonitor.gz` from Downloads (copied by the user from the RDC share): gzip data, contains six **report** monitors for zones 1, 14, 15, 16, 5 and 201, sampled at 877 points each. It does **not** contain the residuals and is not used by the app.

## Tests

- `FluentMonTests/`: `DashboardGeometryTests`, `HeaderBandTests`, `ConsoleBufferTests`, `MessagesTests`, `MonitorStoreTests`, `SeriesBufferTests`, `PairingURLTests`, `KeychainTests`, `MonitorClientTests`, and the new `FluentLogTests` (4 tests).
- Last full run that finished: 37 tests, 1 failure, which was the new `FluentLogTests.testStepStartRowIsFirstRowOfLatestStep`. The test expectation was wrong, and it was fixed. The `FluentLogTests` run after the fix passed 4 of 4.
- **Not rerun since:** the streaming replay refactor, the whole-run convergence change, and the `Progress` change. Run the full suite again before trusting it.
- An earlier run showed 7 failures (Keychain round trip, `MonitorStore` meter indexes). They did not appear in the 37-test run. Not investigated further.

## Tooling gotchas

- `xcodebuild` can ignore `timeout`'s SIGTERM and keep running. Kill it by PID, then rerun with output to a log file. Check with `ps`.
- SourceKit shows "Cannot find type" and "No such module XCTest" for single files. These are noise. The real signal is `xcodebuild`.
- The iOS Simulator panel was attached with the `attach` action of the simulator tool. Launch with args uses `xcrun simctl`, because the tool's `launch` does not pass arguments.

## Open items

1. Decide the convergence reference: whole run (current) or per-step (plan section 3). Update the plan either way.
2. Rerun the full unit test suite after the last three changes.
3. Update plan section 12 changelog for the new and moved files.
4. Decide whether `-mock YES` should stay in the scheme's Run action.
5. Verify Bonjour discovery, `fluentmon://` pairing via the system, the denied-permission state, the settings sheet, and the "Live ↓ N new" pill. None are verified on a device or a real server.
6. The Windows server does not exist yet. Real-data testing is limited to the log replay above.
7. Commit the work to `ui-ipad-prototype` once the user says to. Nothing is pushed.
