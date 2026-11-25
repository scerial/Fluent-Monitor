# FluentMon: UI agent instructions (iPad views)

Nov 25, 2026 · @Youngmin Kim

Seven view tasks for the UI agent: everything the user sees on the iPad, with no networking, parsing or store logic. The main plan (`plans/fluentmon-plan.md`) stays the source of truth; this is a separate document at the user's request.

## Scope and rules

The UI agent builds the views and nothing else. The logic agent (tasks F1 to F5, F12 in section 13 of the main plan) supplies the data types the views read. The UI agent never edits those types.

| Owns | Does not touch |
| --- | --- |
| `Views/ArcMeter.swift`, `Views/ProgressBars.swift`, `Views/ResidualChart.swift`, `Views/HeaderBand.swift`, `Views/ConsoleDrawer.swift`, `Views/ConnectSheet.swift`, `Views/SettingsSheet.swift` | `Layout/*`, `Model/*`, `Net/*`, `DashboardView`, `SceneProbe`, wire messages, config keys, file layout |

- Source of truth is the main plan, sections 3, 7, 8 and 9. If this document and the plan disagree, stop and report; do not choose.
- Write no comments in any source file.
- Do one task at a time and finish it only when its acceptance check passes. Report any deviation from the task text.
- Make no design decision the task does not state: colours, sizes and strings come from the task or from the plan. A missing value: use the named temporary value, then list it in the report.
- Every view takes plain value inputs and holds no network or store logic. It must work in an Xcode preview with hard-coded data.
- Dark appearance only; iPadOS 17; SwiftUI with the `@Observable` macro; text scales with Dynamic Type through the one `@ScaledMetric` the plan defines.
- Each task ends with a preview file `Previews/<View>Previews.swift` showing the cases listed, on an 11-inch and a 13-inch iPad canvas.

### Inputs the logic agent provides

These types exist before the UI work starts; use them as they are.

| Type | Used by | Shape |
| --- | --- | --- |
| `Meter`, `Segment` | U1 | as printed in section 7 |
| `Bars` | U2 | decoded `bars` message: `iter`, `conv`, `step` |
| `MonitorStore` | U3 | `visibleSeries(range:)`, `xDomain(range:)`, `yDomain`, `target`, `scrubIter` |
| `Session`, `Connection` | U4 | case, mode, status; `live`, `stale`, `offline` |
| `ConsoleBuffer`, `ConsoleLine` | U5 | lines with `id`, `text`, `kind` |
| `Palette` | all | `cool`, `hot`, `fluent`, `residuals`; values are temporary |

## Tasks U1 to U4: dashboard views

### U1: arc meter

- **File:** `Views/ArcMeter.swift`. Copy `ArcMeter` as printed in section 7 of the plan.
- **Look:** a 270° arc opening at the bottom (`Circle().trim` rotated 135°), grey track at 10 % white, no needle, no scale. Outer stroke 8.5 % of D, at least 8 pt; inner ring 4.5 % of D, at least 5 pt, inset by the outer stroke plus 3 % of D. Text in three tiers, centred: label 7.5 % of D in a light weight and secondary colour, major value 17 % of D, minor line 8.5 % of D, each with a 10 pt floor. Use `monospacedDigit()` on the major and minor values.
- **Motion:** fill changes animate with `.linear(duration: 0.2)`, and not at all when Reduce Motion is on.
- **Edge cases:** a segment with `from == to` draws nothing; a fill above 1 clamps; an empty `outer` list draws the track only; `minor == nil` removes the third line and recentres the other two.
- **Check:** preview at D = 100, 120 and 156 pt showing a CPU meter, the power meter with its inner ring, the RAM meter with two segments, and a meter with no data (all text `—`, track only). No clipped text at D = 100. Text sizes are exactly the percentages above.
- **Out of scope:** where meters are placed, how values are formatted, colour decisions beyond `Palette`.

### U2: progress bars

- **File:** `Views/ProgressBars.swift`. `struct ProgressBars: View { let bars: Bars?; let metrics: BarMetrics }`.
- **Look:** three rows, each a title, a 7 pt rounded track at 10 % white, a rounded fill, and a caption when `metrics.showCaptions` is true. Row heights and padding come from `BarMetrics`, never from measuring. Titles: `Iterations`, `Convergence to 1e-6`, `Time steps`. Captions are the `label` strings of the message, one line each, truncated in the middle.
- **Fill colours:** iterations blue accent, green when converged; convergence teal for `ok`, amber for `stalled`, red for `rising` and `nan`; time steps purple.
- **Steady run:** when `step` is nil the time-step row stays in place, dimmed, with its title and the text `Steady run`, and a grey empty track.
- **Log-scale hint:** on the convergence track, one faint tick at each whole decade: i / of of the track width for i = 1 to ⌈of⌉ − 1, 1 pt wide, white at 30 % opacity, drawn over the fill and taller than the track by 3 pt, no numbers. No ticks when `of ≤ 1`. In compact mode drop them if two would be closer than 8 pt.
- **Motion:** fill animates with `.easeOut(duration: 0.3)`. The whole view is capped at `.dynamicTypeSize(...DynamicTypeSize.xLarge)`.
- **Edge cases:** `bars == nil` shows three empty tracks; `p` outside 0...1 clamps; compact mode drops captions and uses the 42 pt rows.
- **Check:** preview with six cases (steady, transient, stalled, rising, nan, compact) and a convergence bar at `of = 6.0` (five ticks) and `of = 3.4` (three ticks). No overlap or clipping at xLarge.
- **Out of scope:** any bar maths; all values arrive computed.

### U3: residual chart

- **File:** `Views/ResidualChart.swift`. `struct ResidualChart: View { let store: MonitorStore }`.
- **Chart:** implement the `Chart` block of section 7 exactly: one `LineMark` per series at 1.5 pt, log y scale over `store.yDomain`, the 1e-6 `RuleMark` at 2.5 pt, 75 % white, dashed 8 / 5, legend `.overlay` at `.topTrailing` with spacing 8, `chartXSelection` bound to `scrubIter`. Series colours from `Palette.residuals`.
- **Controls:** a segmented control (All, Last 500, Last 100) at the top left of the chart frame. While `scrubIter` is set, a readout lists every series value at that iteration, in the legend's order, 11 pt monospaced digits.
- **Legend:** 11 pt, at most 8 entries.
- **Edge cases:** no data shows `Waiting for a Fluent run` centred and no axes; one series; target outside the data range; more than 8 series (first 8 only).
- **Check:** preview fed with three falling series and with no data. The target line is visibly heavier than the series lines and does not touch the legend. Dragging shows the readout; changing the segmented control changes the x range.
- **Out of scope:** the `Canvas` fallback, decimation, series toggles and tick-label formatting beyond the defaults.

### U4: header band

- **File:** `Views/HeaderBand.swift`. `struct HeaderBand: View { let session: Session?; let connection: Connection; let lastIteration: Date? }` fills the 32 pt band it is given.
- **Layout, left to right:** case title (600 weight), `Steady` or `Transient` in secondary colour, a status dot with its word (Running, Converged, Idle, Problem, Disconnected), then on the right the time since the last iteration. 11.5 pt text, band outline 1.25 pt at the axis colour, 6 pt corners.
- **Rules:** `static func caseTitle(_ name: String?) -> String` returns `Unnamed` for `FFF` in any letter case and for nil or empty, the name unchanged otherwise. No session shows `No session`. `.offline` always shows Disconnected. The age redraws once a second with `TimelineView(.periodic(from: .now, by: 1))`, `monospacedDigit()`, in the form `0.4 s ago`, `12 s ago`, `3 min ago`, `1 h 05 min ago`; a future date shows `0 s`.
- **Edge cases:** long case names truncate at the tail; the right-hand age never moves when the title length changes.
- **Check:** unit tests for `caseTitle` and the age formatter; preview of all five statuses and a 40-character case name.
- **Out of scope:** the 32 pt height (fixed by the layout) and any wire change.

## Tasks U5 to U7: console and sheets

### U5: console drawer

- **File:** `Views/ConsoleDrawer.swift`. `ConsoleDrawer(console: ConsoleBuffer, onSubscribe: (Bool) -> Void, onLoadOlder: (Int) -> Void)`.
- **Heights:** collapsed is a 28 pt handle showing the newest non-residual line as a ticker; half is 45 % of the left column with the chart visible above; full covers chart and bars, with the meters still visible on the right. Dragging the handle or tapping it cycles the three heights.
- **Lines:** `ScrollViewReader` with `LazyVStack(alignment: .leading)`, one `Text` per line, 12 pt monospaced, one line each with a wrap toggle, row id = absolute line number. Colours by kind: error red, warning amber, time-step lines purple, headers dim and bold, residual rows dim grey, everything else primary.
- **Follow mode:** while scrolled to the bottom, new batches scroll to the last line (without animation when a batch has more than 50 lines). Scrolling up turns follow off and shows a `Live ↓ N new` pill that jumps back. Use `.defaultScrollAnchor(.bottom)` and `.scrollPosition(id:)`.
- **Controls:** a `Hide residual rows` toggle, on by default; a filter field that shows only matching lines; long-press copies a line. Opening the drawer calls `onSubscribe(true)`, closing it `onSubscribe(false)`; reaching the top calls `onLoadOlder(firstLoadedLine)` once per distinct first line.
- **Edge cases:** an empty buffer (centred `No console output yet`); a filter with no match (`No matching lines`); 5,000 lines without dropped frames in the preview.
- **Check:** preview driven by a timer at 30 lines per second shows follow mode, the pill after scrolling up, and every colour kind. Drawer API stays unchanged if the `UITextView` fallback is ever needed.
- **Out of scope:** the buffer logic, sending commands to Fluent.

### U6: connect sheet

- **File:** `Views/ConnectSheet.swift`. `ConnectSheet(servers: [DiscoveredServer], onConnect: (Endpoint) -> Void, initial: Endpoint?)`.
- **Sections:** a list of discovered servers (name and host, tap to fill); manual entry with host, port (default 8765) and a `Use wss` switch; a secure token field with a show / hide button. A `Connect` button is disabled until host and token are non-empty and the port is 1 to 65535. A line under the form states the campus case: `Campus Wi-Fi often hides servers; enter the address by hand.`
- **States:** searching (spinner row), none found (`No servers found nearby`), rejected token (red message above the form, token field focused), local-network permission denied (message explaining where to enable it).
- **Edge cases:** pasted host with `ws://` or a trailing `/ws` (strip it in the view's validation only, and show the cleaned host); keyboard avoidance on both iPad sizes; Dynamic Type up to xLarge.
- **Check:** preview of all four states, with a long server name and a Tailscale name such as `youngmin-pc.tailnet.ts.net`.
- **Out of scope:** discovery, Keychain, the URL scheme handler and the connection itself.

### U7: settings sheet

- **File:** `Views/SettingsSheet.swift`. `SettingsSheet(settings: Binding<ClientSettings>)`, with `ClientSettings { target: Double; exclude: [String]; meterCpus: [Int] }` supplied by the logic agent.
- **Fields:** `target` as a decade stepper from 1e-3 to 1e-8, shown as `1e-6`; `exclude` as a multi-select of residual names currently in the series; `meterCpus` as four pickers, each CPU 0 to 15, no duplicates.
- **Note:** a one-line caption states that these changes affect this iPad only. No wire message sends them to the server yet.
- **Edge cases:** an empty exclude list; a duplicate CPU choice is refused with an inline message; `target` out of range clamps.
- **Check:** preview with each field edited, and one with a duplicate CPU.
- **Out of scope:** persistence and any server write.

### Open questions for the refinement stage

1. `Palette` values are undefined; the views use the temporary values until they are chosen.
2. `Endpoint`, `DiscoveredServer` and `ClientSettings` are minimal shapes invented for these tasks; confirm or replace.
3. The settings sheet has no wire message to reach the server.
4. Previews live in `Previews/` here; the plan's file layout has no such folder.
5. Decimation for a very long chart is undefined, which may affect the chart's frame rate.
