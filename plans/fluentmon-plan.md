# FluentMon — Fluent solver monitor (Windows server + iPad client)

Nov 25, 2026 · @Youngmin Kim

## Overview

FluentMon is a small Python service on the Windows workstation that receives Fluent's console lines as Fluent prints them, polls the PC status on every broadcast, and pushes both over WebSocket to all connected iPads, which never poll. It needs no UDF, no Fluent add-on and no extra license seat, and the same line stream that feeds the residual plot also feeds the console livestream.

| Decision | Choice | Why |
| --- | --- | --- |
| Fluent data source | Event-driven line sources: stdout pipe (batch), PyFluent transcript stream (GUI), transcript file with Windows change notifications (fallback) | Each source pushes lines as they are printed; all three feed one parser, which gives residuals, progress and console |
| Server | Python 3.12, FastAPI + uvicorn, psutil, httpx, zeroconf | Fast to write, every metric has a library |
| Package power | LibreHardwareMonitor's built-in web server (`/data.json`) | Windows has no public API for Zen 4 package power; LHM already ships the driver |
| Transport | One WebSocket, JSON messages, full snapshot on connect | Server pushes each change to every client, encoded once; `URLSessionWebSocketTask` is built into iOS |
| iPad app | SwiftUI + Swift Charts + Observation, iPadOS 17+ | Log-scale y-axis built in; `@Observable` keeps redraws cheap |
| Reach | Bonjour on the LAN, Tailscale off campus | No port forwarding on the university network |

Hardware assumptions: Ryzen 7 7800X3D (8 cores, 16 threads, single CCD, 162 W PPT) on Windows, Fluent limited to 4 cores by the student license. Windows numbers SMT siblings next to each other, so logical CPUs 0, 4, 8 and 12 are the first threads of physical cores 0, 2, 4 and 6. Section 5 shows how to pin the 4 Fluent ranks onto exactly those CPUs so the four meters each show one rank.

## 1. Getting events out of Fluent

Fluent has no event API for its console, so FluentMon uses the most push-like source each kind of run allows. All three adapters put the same `line` events on one `asyncio.Queue`; nothing downstream knows which adapter is active, and none of them polls on a timer.

| Run type | Adapter | Mechanism | Wakes up when |
| --- | --- | --- | --- |
| Batch (`-g`) started by `fmon launch` | **stdout pipe** | `asyncio.create_subprocess_exec(..., stdout=PIPE, stderr=STDOUT)`, then `await proc.stdout.readline()` in a loop; each line is also teed to `console.trn` | Fluent writes a line to stdout |
| GUI started by `fmon launch` | **PyFluent transcript stream** | Fluent launched with `-sifile=...`; `connect_to_fluent(...)`, then `session.transcript.register_callback(on_text)`. The gRPC callback runs on a worker thread and hands text to the loop with `loop.call_soon_threadsafe(q.put_nowait, text)` | Fluent streams console text over gRPC |
| Anything else, including a GUI run started by hand | **transcript file + change notifications** | `watchdog` observer (Windows `ReadDirectoryChangesW`) on the transcript folder; each `FileModifiedEvent` triggers one read from the saved offset to EOF | Windows reports a write to the file |

Session lifecycle is event-driven too:

- **Start.** The launcher writes `session.json` into `runs_dir`; the directory watcher's creation event attaches the right adapter. A transcript started by hand in `watch_dir` raises the same kind of creation event.
- **End.** The launcher awaits `proc.wait()`; for runs it did not start, a worker thread blocks in `psutil.Process(pid).wait()` on the Fluent host process. Both return when Windows signals the process handle, and the server broadcasts `session_end`.

What the line stream contains (format to confirm in phase 0 against your own version). A steady run writes:

```text
  iter  continuity  x-velocity  y-velocity  z-velocity      energy           k       omega     time/iter
   118  2.3156e-03  1.2034e-04  9.8812e-05  1.1102e-04  3.4410e-07  2.0981e-04  5.5521e-04  0:01:12  382
```

The header repeats every few dozen iterations. On data lines, the second-to-last column is Fluent's estimate of wall time left for the current Calculate, and the last column is iterations left. A transient run adds, after each time step:

```text
Flow time = 1.2000e-02s, time step = 120
880 more time steps
```

### Starting the transcript (three supported ways)

1. **FluentMon launcher (recommended).** `fmon launch case.cas.h5` creates `C:\FluentMon\runs\20261125-1041_case\`, writes `start.jou` there, and starts `fluent 3ddp -t4 -i start.jou` (GUI stays interactive once the journal ends). The journal is:

   ```text
   /file/start-transcript "C:/FluentMon/runs/20261125-1041_case/console.trn"
   /file/read-case-data "D:/cfd/case.cas.h5"
   ```

   The two paths in the journal are examples. The launcher replaces the run folder and the case path with the real ones.

   The launcher also writes `session.json` (case path, launch time, Fluent PID, core count) so the server knows exactly which transcript is live. Use forward slashes and no spaces in paths; Fluent's TUI parser is fussy about both.
2. **Manual, from the GUI.** File > Write > Start Transcript, saved into the watched folder `C:\FluentMon\transcripts\`. The server treats the newest `.trn` modified in the last 60 s as live.
3. **Batch runs.** `fluent 3ddp -t4 -g -i run.jou > C:\FluentMon\transcripts\run.log 2>&1`. The console log has the same format, so the same tailer reads it.

### Things phase 0 must confirm

- **Pipe buffering.** If Fluent block-buffers stdout when it is a pipe rather than a console, lines arrive in 4 KB bursts. Then use the file adapter for batch runs too.
- **Change notifications on an open file.** NTFS can delay or merge change notifications while another process keeps the file open, and directory listings show a stale size until the handle closes. FluentMon reads bytes through its own handle, so the data are right whenever an event fires; the test is whether every append fires one. Write a 30-line script that counts `FileModifiedEvent`s against appended residual lines during a real run. If events stall, set `safety_read_s = 2` in the config: one extra read only after 2 s of silence during an active run. It is off by default.
- **PyFluent on the student edition.** Confirm `connect_to_fluent` and `transcript.register_callback` work with your Fluent version and license, and pin the PyFluent version in `pyproject.toml`; API names have changed between releases. If it is unavailable, GUI runs use the file adapter.

The PyFluent connection also leaves room for remote controls later (interrupt, save), kept behind a confirmation dialog.

## 2. Transcript tailer and parser

Whichever adapter is active hands text chunks to one line assembler, which feeds complete lines into a line-by-line state machine; every regex below is anchored and tested against recorded transcripts before the UI exists.

### File adapter and line assembler (`fluentmon/transcript/tailer.py`)

- **Line assembler (shared by all adapters).** Appends each incoming chunk to a buffer, splits on `\n`, keeps the trailing partial line for the next chunk, strips `\r`, and decodes as UTF-8 with `errors="replace"`, retrying as `cp949` when a line contains U+FFFD (Korean Windows paths in Fluent messages).
- **Open once.** `open(path, "rb")`. Python on Windows opens with read and write sharing, so Fluent keeping the file open for writing is not a problem.
- **Read on notification.** The watchdog callback runs on watchdog's own thread and only calls `loop.call_soon_threadsafe(drain_requested.set)`. A single `drain()` task waits on that `asyncio.Event`, clears it, then reads from the saved offset to EOF in 1 MB chunks. A burst of 50 notifications therefore costs a few reads, not 50.
- **Restart detection.** If the file is smaller than the saved offset, the transcript was restarted: reset the session.
- **Switching sessions.** A creation event for a newer `session.json` or transcript closes the current session as `ended` and attaches the new one.
- **Catch-up.** On attach to an existing file, read it from the start without broadcasting per line, then switch to live mode. This rebuilds full history after a server restart.

### Line patterns (`fluentmon/transcript/parser.py`)

```python
HEADER    = re.compile(r"^\s*iter\s+(?P<names>.+?)\s+time/iter\s*$")
DATA      = re.compile(r"^\s*(?P<it>\d+)\s+(?P<vals>(?:\S+\s+)+?)"
                       r"(?P<eta>\d+:\d{2}:\d{2})\s+(?P<left>\d+)\s*$")
FLOWTIME  = re.compile(r"^\s*Flow time\s*=\s*(?P<t>[-+0-9.eE]+)\s*s?\s*,\s*time step\s*=\s*(?P<n>\d+)")
MORESTEPS = re.compile(r"^\s*(?P<n>\d+)\s+more time steps?")
CONVERGED = re.compile(r"^\s*!\s*(?P<it>\d+)\s+solution is converged")
PROBLEM   = re.compile(r"(Divergence detected|Floating point exception|Error:|"
                       r"reversed flow|turbulent viscosity limited|temperature limited)", re.I)

def num(tok):
    try:
        return float(tok)
    except ValueError:      # nan, -nan(ind), 1.#QNAN, inf
        return math.nan
```

Rules:

- `HEADER` sets the active column list. The data line is accepted only when its value count equals the header's name count; otherwise it is logged and dropped.
- Columns are split into **residuals** and **reports**. Residuals = names in a known set (`continuity`, `x/y/z-velocity`, `energy`, `k`, `epsilon`, `omega`, `nut`, `intermit`, `retheta`, `v2`, species names, `uds-N`), overridable in `config.toml`. Anything else is a report definition printed to the console; it is kept and sent, but not used for the convergence bar.
- An iteration number lower than the last one means the case was re-initialised: archive the current series as a segment and start a new one.
- Any `FLOWTIME` line switches the session mode to `transient`.

### Events the parser emits

| Event | Trigger | Used for |
| --- | --- | --- |
| `header` | `HEADER` matched with a different name list | chart series set |
| `iter` | `DATA` matched | residual points, iterations left, ETA |
| `run_start` | first `iter`, or iterations-left larger than on the previous line | resets the progress baselines |
| `step` | `FLOWTIME` + `MORESTEPS` | time-step bar, flow time |
| `converged` | `CONVERGED` | green state, notification |
| `problem` | `PROBLEM` | red banner, console highlight, push notification |
| `idle` | no `iter` for max(30 s, 5 × mean time per iteration) | grey state, bars freeze |
| `line` | every line | console stream |

Keep the parser a pure function of lines in, events out. That makes it testable with fixture files and reusable by the replay tool (section 10).

## 3. Progress math (the three bars)

All three bars are computed on the server, so the iPad only draws numbers it is given and every client shows the same values. The convergence bar is measured in decades on a log scale, because a linear fraction of the distance to 1e-6 would read 99.9 % almost immediately.

### Bar 1: iterations

Let i0 be the first iteration of the current run (set at `run_start`), i the current iteration and L the iterations-left column.

```latex
p_{\mathrm{iter}} = \frac{i - i_0 + 1}{(i - i_0 + 1) + L}
```

- Label: `Iteration 482 / 1000 · 518 left · ETA 0:12:40` (ETA straight from Fluent's column).
- On `converged`, fill to 100 % and relabel `Converged at 482`.
- Transient runs: L counts iterations left inside the current time step, so this bar becomes `Iterations this step 7 / 20` and resets each step.

### Bar 2: convergence toward 1e-6

For each iteration take the highest residual over the residual columns only (reports excluded), ignoring NaN:

```latex
r_{\max}(i) = \max_j r_j(i), \qquad r_{\mathrm{ref}} = \max_{i_0 \le i < i_0 + 5} r_{\max}(i)
```

```latex
p_{\mathrm{conv}} = \operatorname{clamp}\!\left(\frac{\log_{10} r_{\mathrm{ref}} - \log_{10} \tilde r_{\max}}{\log_{10} r_{\mathrm{ref}} - \log_{10} r_{\mathrm{target}}},\ 0,\ 1\right), \qquad r_{\mathrm{target}} = 10^{-6}
```

- r̃max is an exponential moving average in log space, log r̃ ← 0.8 log r̃ + 0.2 log rmax, so oscillating residuals do not make the bar flicker.
- Example: rref = 1, rmax = 1e-3 gives 3 of 6 decades, 50 %.
- Scale: the fill is linear in decades, so the bar is logarithmic in residual value. The scale hint is one faint tick at each whole decade between rref and 1e-6, at i / of of the track width for i = 1 to ⌈of⌉ − 1 (5 ticks for 6 decades, none when of ≤ 1). The client draws them from the `of` value already in `conv`, so the wire format does not change. Ticks carry no numbers; the title already names the 1e-6 end.
- Label: `Limiting: continuity 2.3e-03 · 3.4 / 6.0 decades`. Naming the limiting equation is the most useful part of this bar.
- Rate estimate: least-squares slope s of log10 rmax against iteration over the last 200 iterations. If s < −1e-4 decades per iteration, show `≈ 1,900 iterations to 1e-6`, computed as (log10 rmax − −6) / (−s); otherwise show `stalled`.
- If rmax rises above rref, clamp to 0 and colour the bar red; any NaN turns it red with the label `NaN in <equation>`.
- Transient runs: residuals restart every step, so rref is taken from the first iteration of each step and the bar shows per-step convergence.
- `target` and an `exclude` list (for example, continuity on cases that plateau) live in `config.toml` and can be changed from the iPad settings sheet.

### Bar 3: time steps (transient only)

Let n be the time step from the latest `Flow time` line, n0 the first step of the run and M the `more time steps` count.

```latex
p_{\mathrm{step}} = \frac{n - n_0 + 1}{(n - n_0 + 1) + M}, \qquad t_{\mathrm{end}} \approx t + M\,\Delta t
```

- Δt comes from the difference between consecutive flow times; wall time per step is a moving average of the gap between `step` events, and ETA = that average × M.
- Label: `Step 120 · 880 left · t = 0.0120 / 0.1000 s · ETA 2:41:05`.
- Steady runs: the bar stays in place, greyed, labelled `Steady run`, so the layout never jumps.

## 4. System metrics (right-hand meters)

CPU load and RAM come from `psutil`; package power, clock speeds and temperature come from LibreHardwareMonitor's local web server, because Windows has no public API for AMD package power. Six arcs carry the values below, with a clock speed under each CPU meter and a temperature ring inside the power meter.

| Meter | Source | Interval | Shown as |
| --- | --- | --- | --- |
| CPU 0, 4, 8, 12: load | `psutil.cpu_percent(percpu=True, interval=None)`, indices from `config.toml` | 0.5 s | arc 0–100 %, major text |
| CPU 0, 4, 8, 12: clock | LHM Clocks, `Core #1`, `#3`, `#5`, `#7` (Effective) | 1 s | minor text, GHz |
| Package power | LHM `http://127.0.0.1:8085/data.json`, sensor `CPU Package` power | 1 s | outer arc 0–162 W (the 7800X3D's 162 W PPT), major text |
| CPU temperature | LHM `Core (Tctl/Tdie)` | 1 s | inner arc from 20 °C to the thermal limit, minor text |
| RAM used | `psutil.virtual_memory()`: used = total − available | 1 s | arc 0–total, major text as % |
| RAM held by Fluent | sum of `memory_full_info().uss` over the Fluent process tree | 2 s | tinted part of the RAM arc, minor text in GB |

### PC status polled on every broadcast

Hardware cannot notify on change: CPU load is an average over an interval, and neither LHM nor Windows performance counters push. So the server asks, and it asks at the moment it has something to broadcast. Every event-driven broadcast (a residual and bars flush, a console flush, a solver event) is followed by one poll of the PC status, and the result goes out as a `sys` message. The meters refresh exactly as often as the residual chart does, and no separate sampling timer runs while Fluent is solving.

- **Trigger.** `Hub.broadcast()` sends the event message first, then calls `poller.request()`. The event never waits for the poll; the `sys` message follows it, typically 10 to 30 ms later. Residual and bars flushes, console flushes and solver events all count.
- **One poll at a time.** The poll runs in `asyncio.to_thread`. A request that arrives while one is running sets a `dirty` flag, and exactly one more poll starts when the current one ends, so requests never pile up.
- **Minimum spacing.** Polls start at least 200 ms apart, because `psutil.cpu_percent` measures load since its previous call and a shorter window is mostly noise. A broadcast inside that window is covered by the next poll, which caps `sys` at 5 messages a second however fast Fluent iterates.
- **What one poll reads.** Per-CPU load and RAM totals from `psutil`, and package power, clocks and temperature from one LHM `/data.json` request. The Fluent share of RAM (`memory_full_info()` over the process tree) is the costly part, so it is refreshed at most once a second and reused in between.
- **Idle fallback.** A 5 s timer, re-armed by every broadcast, requests a poll when nothing else has: Fluent not running, or a case slower than one iteration per 5 s. It is the only timer in the server.
- **No deadband.** Every poll's values are sent (about 250 bytes), and the app already smooths the arcs.
- The Fluent process tree is resolved once at `session_start` and re-resolved only when a stored PID raises `NoSuchProcess`, not rescanned on every poll.
- Optional: to drop the HTTP hop to LHM, load `LibreHardwareMonitorLib.dll` in-process with `pythonnet` and call its update visitor on each poll. This is still polling, just without a second process, and it needs FluentMon to run elevated. The web server stays the default because it keeps FluentMon unprivileged.

Clients never ask for hardware values; they receive `sys` messages right after each event broadcast, and every 5 s when none is flowing.

### CPU meters and rank pinning

On Windows the two SMT threads of a core are adjacent logical CPUs, so 0, 4, 8 and 12 sit on four different physical cores. With the student license's 4 ranks, pinning one rank to each of those CPUs makes each meter a rank meter and keeps two ranks off the same core.

- First choice: the launcher sets `I_MPI_PIN_PROCESSOR_LIST=0,4,8,12` before starting Fluent (Intel MPI is Fluent's default on Windows). Confirm in Task Manager > Details > right-click `fl_mpi*.exe` > Set affinity.
- Fallback (`pin_ranks = true`): when the server finds exactly 4 `fl_mpi*.exe` processes, it sorts them by PID and calls `cpu_affinity([0])`, `([4])`, `([8])`, `([12])`. Same user, no admin needed.
- `psutil` reports time-based load, which can differ from Task Manager's "% Processor Utility" (that one scales with boost clock). Mention this in the app's info sheet so the numbers do not look wrong.

### Package power via LibreHardwareMonitor

1. Install the newest LibreHardwareMonitor release. Older builds use the WinRing0 driver, which Microsoft Defender now flags; recent builds use the PawnIO driver instead, which may ask to be installed on first run.
2. Options > Remote Web Server > Run (port 8085); Options > Run On Windows Startup and Start Minimized. It needs admin rights for its driver, so let it create its startup task with highest privileges.
3. The server polls `data.json` with `httpx` and walks the sensor tree to the CPU node > `Powers` > `Package`. Store the path once (fmon setup detects and writes it, section 14) in `config.toml` (`power_sensor = "AMD Ryzen 7 7800X3D/Powers/Package"`), and parse `"87.3 W"` to a float.
4. If LHM is unreachable, send `pkg_w: null`; the arc shows a hatched empty track with `no sensor` instead of a misleading 0.
5. While the tree is open, also read `Core (Tctl/Tdie)` temperature; it feeds the inner temperature ring of the power meter.

Fill = power ÷ 162 W, the stock PPT, so the arc reads as fraction of the power limit. The arc turns orange above the 120 W TDP, so the orange part is the headroom between TDP and PPT. If you run Eco Mode or PBO, change `pkg_max_w` to match. A 4-rank Fluent load will typically fill well under the full arc; that is correct, not a bug.

### Clock speed and the temperature ring

- **Clock under each CPU meter.** Logical CPU n sits on physical core n ÷ 2 (SMT siblings are adjacent), and LHM numbers cores from 1, so CPU 0, 4, 8, 12 read LHM `Core #1`, `#3`, `#5`, `#7`. Prefer the `(Effective)` sensor when LHM exposes it, because it averages over sleep states and an idle core does not read 5 GHz. Show GHz with one decimal, and `—` when no clock sensor exists. Confirm the names in LHM's GUI in phase 0.
- **Temperature ring.** The inner arc starts at 20 °C (empty) and ends at the thermal limit, 89 °C for the 7800X3D (AMD's listed maximum operating temperature; `tjmax_c` in the config). It fills linearly between them.
- **Colour levels.** Cool accent up to 70 °C, yellow above 70 °C (`temp_warn_c`, the point where all-core loads stop being moderate), red above 90 % of the arc, which is 82.1 °C with these numbers. The minor temperature text takes the same colour.

Chip figures behind these numbers: 8 cores and 16 threads, 4.2 GHz base, up to 5.0 GHz boost, 120 W TDP, 162 W PPT and an 89 °C maximum. A CPU meter's clock should read between about 4.2 and 5.0 GHz under load; a value far outside that range points to a wrong sensor mapping.

### RAM with the Fluent share tinted

- Fluent tree = the launcher PID from `session.json` plus all its descendants. Without a launcher, match names with `^(fluent|cx\d{3,4}|fl\d{3,4}|fl_mpi\d{3,4})\.exe$` plus any `hydra_pmi_proxy.exe` / `mpiexec.exe` descendants of those.
- Use `uss` (unique set size, about the same as Task Manager's private working set). Working set (`rss`) would count shared pages once per MPI rank and overstate Fluent's share. `memory_full_info()` is slow on Windows, hence the 2 s interval; fall back to `memory_info().private` if it raises `AccessDenied`.
- Clamp so 0 ≤ Fluent ≤ used ≤ total, then send all three. The arc draws Fluent in a bright accent, other used memory in neutral grey, and leaves free memory as the empty track.

## 5. Windows server design

One asyncio process: one ingest adapter, one PC status poller, an event bus, a session reducer and a broadcast hub, served by uvicorn on port 8765. The transcript itself is the database: after a restart the server re-parses it, so there is no SQLite or history file to keep in sync.

```text
fluentmon/
  pyproject.toml            # fastapi, uvicorn[standard], psutil, httpx, zeroconf, tomli-w
  config.toml
  installer/
    install.ps1
  fluentmon/
    __main__.py             # load config, start tasks, run uvicorn
    config.py               # dataclass + toml load/save
    launcher.py             # `fmon launch`: run folder, start.jou, session.json, I_MPI env
    transcript/
      tailer.py             # file adapter: watchdog events, offset, restart
      pipe.py               # stdout adapter for batch runs
      pyfluent_src.py       # gRPC transcript callback adapter
      assembler.py          # chunks -> lines, shared by all adapters
      parser.py             # regexes -> events (pure, no I/O)
    model/
      session.py            # SessionState: series, baselines, bars, status
      progress.py           # the three bar formulas + EMA + slope fit
      decimate.py           # min/max bucket downsampling for snapshots
    system/
      cpu.py                # per-CPU load, optional affinity pinning
      power.py              # LHM data.json client
      memory.py             # totals + Fluent tree USS
    api/
      ws.py                 # /ws: hello, snapshot, live messages
      http.py               # /api/health, /api/sessions, CSV export, config
      hub.py                # encode once, fan out to per-client queues
      bus.py                # in-process publish/subscribe
    discovery.py            # Bonjour advert _fluentmon._tcp
    cli.py
    setup/
      wizard.py
      detect.py
      lhm.py
      tasks.py
      firewall.py
      manifest.py
      doctor.py
      backend.py
  tools/
    replay.py               # writes a recorded transcript out at N x speed
  tests/
    fixtures/steady_kw.trn, transient_les.trn, diverged.trn, reports.trn, lhm_amd.json, lhm_intel.json
    test_parser.py, test_progress.py, test_detect.py, test_manifest.py
```

### Runtime behaviour

&#91;embedded content: push pipeline · 3 adapters, 1 bus, 1 PC poll per broadcast, N clients\]

The PC status poll runs after each broadcast, and its 5 s idle fallback is the only timer in the server; every other box runs when the box above it hands it something.

- **Event bus.** Adapters and the PC poller `publish(event)`; the reducer, the hub and `notify.py` each `subscribe()` and await their own queue. No consumer loops on a timer.
- **Reducer.** Applies parser events to `SessionState` and publishes derived events (`bars` changed, status changed). Series are `array('d')` per column, so 50,000 iterations × 10 columns is about 4 MB.
- **Broadcast hub.** Each outgoing message is serialised once, and the same bytes go into every client's queue. Each client has one writer task awaiting its queue, so a slow iPad never delays the others.
- **Slow or asleep clients.** Queues hold at most 1,000 messages. A full queue drops that client; it reconnects and gets a fresh snapshot instead of a backlog.
- **Leading-edge coalescing.** The first iteration after a quiet spell is sent at once. Anything more within the next 100 ms is merged into one `resid` + `bars` message at the end of that window. A slow 3D case gets every iteration instantly; a fast 2D case at 30 iterations/s costs at most 10 messages/s. Console lines use the same rule (100 ms, at most 400 lines).
- **Snapshot.** On connect, each series is decimated to at most 1,500 points (min and max per bucket, so spikes survive) for the old part, plus the last 500 iterations at full resolution. After that the client only receives pushes.
- **Blocking calls.** `memory_full_info()` and the LHM request run through `asyncio.to_thread`, so a slow sample never delays a residual push.

```python
class Hub:
    def __init__(self, poller: PcPoller) -> None:
        self.clients: set[Client] = set()
        self.poller = poller
        self.idle: asyncio.TimerHandle | None = None

    def send(self, msg: dict) -> None:
        data = orjson.dumps(msg)
        for c in tuple(self.clients):
            try:
                c.queue.put_nowait(data)
            except asyncio.QueueFull:
                c.close(code=4408)

    def broadcast(self, msg: dict) -> None:
        self.send(msg)
        self.poller.request()
        self.rearm_idle()

    def rearm_idle(self) -> None:
        if self.idle:
            self.idle.cancel()
        self.idle = asyncio.get_running_loop().call_later(5.0, self.idle_poll)

    def idle_poll(self) -> None:
        self.poller.request()
        self.rearm_idle()

class PcPoller:
    def __init__(self, send, sample) -> None:
        self.send = send
        self.sample = sample
        self.running = False
        self.dirty = False
        self.last_at = 0.0

    def request(self) -> None:
        if self.running:
            self.dirty = True
            return
        self.running = True
        asyncio.create_task(self.run())

    async def run(self) -> None:
        try:
            while True:
                self.dirty = False
                wait = self.last_at + 0.2 - time.monotonic()
                if wait > 0:
                    await asyncio.sleep(wait)
                msg = await asyncio.to_thread(self.sample)
                self.last_at = time.monotonic()
                self.send(msg)
                if not self.dirty:
                    return
        finally:
            self.running = False

async def client_writer(c: Client) -> None:
    while True:
        data = await c.queue.get()
        await c.ws.send_bytes(data)
```

### HTTP endpoints

| Method and path | Returns |
| --- | --- |
| `GET /api/health` | `{"ok": true, "version": "0.1.0", "lhm": true}` |
| `GET /api/sessions` | live and recent sessions with case name, mode, status |
| `GET /api/sessions/{id}/residuals.csv` | full-resolution history for export |
| `PUT /api/config` | change `target`, `exclude`, meter CPUs, `pkg_max_w` |
| `WS /ws` | the stream (section 6) |

### config.toml

```toml
[server]
port = 8765
token = "generate-with-secrets.token_urlsafe(24)"
watch_dir = "C:/FluentMon/transcripts"
runs_dir = "C:/FluentMon/runs"

[convergence]
target = 1e-6
exclude = []
extra_residual_names = []

[meters]
cpus = [0, 4, 8, 12]
pin_ranks = false
lhm_url = "http://127.0.0.1:8085"
power_sensor = "AMD Ryzen 7 7800X3D/Powers/Package"
pkg_max_w = 162
clock_sensors = ["Core #1 (Effective)", "Core #3 (Effective)", "Core #5 (Effective)", "Core #7 (Effective)"]
temp_sensor = "AMD Ryzen 7 7800X3D/Temperatures/Core (Tctl/Tdie)"
temp_base_c = 20
temp_warn_c = 70
tjmax_c = 89
tdp_w = 120
poll_min_ms = 200
idle_poll_s = 5
fluent_ram_s = 1
```

### Running it

- Installed: run `installer/install.ps1` in PowerShell. It installs `uv`, the package, LibreHardwareMonitor, both scheduled tasks and the firewall rule, then finishes with `fmon doctor` (section 14). The Always-on and Firewall bullets below are the same setup done by hand.
- Development: `py -3.12 -m venv .venv`, `pip install -e .`, `python -m fluentmon`.
- Always-on: Task Scheduler task "At log on of the current user", action `pythonw.exe -m fluentmon` (no console window), restart on failure every 1 minute. LHM gets its own elevated task; FluentMon itself does not need admin. fmon setup registers both tasks (section 14).
- Firewall, once, in an elevated PowerShell:

  ```powershell
  New-NetFirewallRule -DisplayName "FluentMon" -Direction Inbound -Protocol TCP `
    -LocalPort 8765 -Action Allow -Profile Private `
    -RemoteAddress LocalSubnet,100.64.0.0/10
  ```

  The second address range is Tailscale's; fmon setup adds it only when you accept the Tailscale step (section 14). Keep LHM's port 8085 closed to the network; only FluentMon reads it, locally.

## 6. Wire protocol

One WebSocket at `ws://<host>:8765/ws`, binary frames carrying UTF-8 JSON, each with a type field `t`. The client never asks for deltas: on every (re)connect it gets one `snapshot`, then live messages, which keeps reconnect logic trivial.

- Auth: header `Authorization: Bearer <token>` on the upgrade request (`URLRequest` with `URLSessionWebSocketTask` supports this). Wrong or missing token → close code 4401.
- JSON has no NaN. The hub uses orjson, which writes NaN as null; with the standard library you would serialise with `allow_nan=False` after turning NaN into `null`; Swift decodes those as `Double?`. Python's default `NaN` output would make `JSONDecoder` throw.
- Keepalive: uvicorn ping every 15 s. Client reconnect backoff 0.5, 1, 2, 4, 8, then 10 s, reset on success.
- Traffic is tiny: about 0.4 KB/s for `sys`, 1–5 KB/s for residuals and console during a run.

### Server → client

```json
{"t":"hello","proto":1,"server":"YOUNGMIN-PC","version":"0.1.0","session":"20261125-1041_duct",
 "thermal":{"base_c":20,"warn_c":70,"max_c":89}}

{"t":"snapshot",
 "session":{"id":"20261125-1041_duct","case":"duct_kw","mode":"steady","status":"running",
            "started":"2026-11-25T10:41:07+09:00"},
 "columns":["continuity","x-velocity","y-velocity","z-velocity","energy","k","omega","cd-1"],
 "kinds":["res","res","res","res","res","res","res","rep"],
 "iters":[1,2,3],
 "values":[[1.0,0.62,0.41],[0.12,0.09,0.07],[0.1,0.08,0.06],[0.11,0.08,0.06],
            [0.003,0.002,0.001],[0.4,0.2,0.1],[0.9,0.5,0.3],[0.81,0.77,0.74]],
 "bars":{ /* same shape as the bars message */ },
 "con_tail":{"first":9735,"lines":["..."],"kinds":"nnhr"}}

{"t":"resid","rows":[[483,[2.31e-3,1.2e-4,9.9e-5,1.1e-4,3.4e-7,2.1e-4,5.6e-4,0.712]],
                     [484,[2.29e-3,1.2e-4,9.8e-5,1.1e-4,3.4e-7,2.1e-4,5.5e-4,0.711]]]}

{"t":"bars",
 "iter":{"p":0.484,"done":484,"total":1000,"left":516,"eta_s":758,"label":"Iteration 484 / 1000"},
 "conv":{"p":0.57,"rmax":2.29e-3,"limiting":"continuity","decades":3.4,"of":6.0,
         "iters_to_target":1900,"state":"ok"},
 "step":null}

{"t":"sys","cpu_ids":[0,4,8,12],"cpu":[96.8,97.9,98.2,96.5],"pkg_w":71.4,"pkg_max_w":162,"tdp_w":120,
 "clk_mhz":[4950,4925,4975,4900],"tctl_c":68.0,"ram":{"total":34359738368,"used":21474836480,"fluent":12884901888}}

{"t":"con","first":10235,"lines":["   484  2.2900e-03  ...  0:12:38  516"],"kinds":"r"}

{"t":"event","kind":"converged","iter":1412,"text":"! 1412 solution is converged",
 "ts":"2026-11-25T11:53:40+09:00"}
```

- `bars.step` is `null` for steady runs, otherwise `{"p","n","left","flow_time","t_end","eta_s","label"}`.
- `conv.state` is `ok`, `stalled`, `rising` or `nan`; the app maps it to bar colour.
- `con.kinds` has one character per line, classified on the server: `h` header, `r` residual row, `w` warning, `e` error, `s` time-step line, `n` anything else. The client colours lines without running any regex.
- `con.first` is the absolute line number of the first line, so the client can detect and ignore duplicates after reconnecting.
- `event.kind`: `run_start`, `converged`, `problem`, `idle`, `resumed`, `session_end`.
- `sys` follows every `resid`, `bars`, `con` and `event` message within about 30 ms, at most 5 times a second, and arrives alone every 5 s when none of those is flowing.

`sys.clk_mhz` has one entry per id in `cpu_ids` and is `null` for a missing sensor. `tctl_c` is the CPU temperature; its scale and colour levels come from `hello.thermal`, which is sent once.

### Client → server

```json
{"t":"sub","console":true}
{"t":"con_back","before":9735,"n":500}
{"t":"ping"}
```

`sub` lets the app stop console traffic while the console drawer is closed; `con_back` fetches older console lines when the user scrolls up past the 500-line tail sent in the snapshot.

No client message asks for live data. Everything after the snapshot is pushed; the only client messages are a subscription change and a scroll-back request the user triggers by scrolling.

## 7. iPad app

A SwiftUI app for iPadOS 17+ that picks its layout from the sensed orientation (full screen) or the window's aspect ratio (windowed). A header band spans the top in both orientations. Below it, in landscape, a column of six needle-less arc meters runs down the right edge, and the residual chart over the three bars fills everything to its left. In portrait, the six meters run in one row above the chart. Every frame comes from one pure `DashboardGeometry` function.

&#91;embedded content: iPad layouts · landscape: height sets D, chart takes the rest; portrait: meters on top\]

### Landscape geometry: one driving dimension

In landscape the content height H, less the header band h and one gap g, is the only input that sizes anything; no width ratio is defined. Each gauge shows k · D of visible arc (k = (1 + sin 45°) / 2 ≈ 0.8536, a 270° arc opening downward), and s = 0.20 D is the vertical gap between gauges.

1. **Gauge diameter from height.** The gap between gauges is a share of the diameter, s = 0.20 D, so six gauges and five gaps span H − h − g exactly:

```latex
D = \frac{H - h - g}{6k + 5 \cdot 0.20} = \frac{H - h - g}{6k + 1}
```

2. **Meter column** is exactly D wide, flush with the right content edge, and starts g below the header band.
3. **Left column** is the remainder: W − g − D.
4. **Bars** end at the content bottom H; their height comes from `BarMetrics`.
5. **Chart** starts g below the header band and ends g above the bars: height H − h − g − g − bars height.

The last gauge's visible bottom and the bars' bottom are both defined as H, so they are collinear by construction rather than by a second constraint that could conflict. D itself is not rounded; frame edges are snapped to the pixel grid (see Pixel snapping), and the shared bottom edge is one snapped number, so the equality still holds exactly.

| Defined | Derived | Deliberately not defined |
| --- | --- | --- |
| Content size W × H; constants p = 16, g = 12, h = 32; gap ratios 0.20 (landscape, vertical) and 0.15 (portrait, horizontal); k; bars height | D, gauge gap s, meter column width, left column width, chart height | Any width ratio between the columns; any target gauge diameter; any gauge gap in points |

### Header band

A band of h = 32 pt spans the full content width above everything else, in both orientations. In landscape it pushes the meter column and the chart down by h + g; in portrait it sits above the meter row. It holds, left to right: the case title, a Steady or Transient label, a status dot with its word (Running, Converged, Idle, Problem, Disconnected), and the time since the last iteration. The age text is redrawn once a second by the view itself, which is a display clock and not a data poll.

`HeaderBand.caseTitle(_:)` shows "Unnamed" when the case name is "FFF" (case-insensitive), the name Fluent gives a case that has not been saved. Any other name is shown as sent. The server keeps sending the raw name, so the wire protocol does not change.

### Orientation rule

The mode is an input to `DashboardGeometry`, not something it derives. `LayoutMode.resolve` picks it. In a full-screen window it follows the sensed interface orientation: portrait and portrait upside-down give portrait, the two landscapes give landscape. In a windowed app (Split View, Stage Manager, Slide Over) it follows the window's aspect ratio, with hysteresis: it switches to portrait when width / height falls below 0.95 and back to landscape when it rises above 1.05, so dragging a window edge across square does not flip the layout back and forth. If the orientation is unknown, the aspect rule applies. `SceneProbe` reads `isFullScreen` and the interface orientation from the window scene and hands them to `DashboardView`.

### Pixel snapping

Every frame edge is rounded to the display's pixel grid: `snap` rounds `minX`, `minY`, `maxX` and `maxY` separately to multiples of 1 / displayScale and rebuilds the rectangle from them. The shared bottom edge is one number, so the bars and the last gauge still end on exactly one line, and strokes stay crisp. Gaps and widths can differ from the ideal by up to 1 / displayScale (0.5 pt on a 2x screen), which the tests allow. D itself stays unrounded and keeps driving text and stroke sizes.

### Reserved bar height

The bars block keeps its full height on steady runs. The time-step row stays in place, dimmed, with its title and `Steady run`, so nothing moves when a run switches between steady and transient. The cost is one unused 66 pt row during steady runs.

| Value | Setting | Status | How it is confirmed |
| --- | --- | --- | --- |
| Header band height | 32 pt | assumed | Look at it on the 11-inch and 13-inch in phase 0 |
| Unsaved case shown as Unnamed | `FFF` | mapping decided; that `FFF` is the exact name Fluent reports is assumed | Check the transcript of a fresh, unsaved case |
| Mode in full screen | interface orientation | decided | Rotate the iPad on its stand |
| Mode when windowed | aspect ratio, portrait below 0.95, landscape above 1.05 | rule decided; the two thresholds are assumed | Drag a Stage Manager window across square |
| Frame edges | snapped to 1 / displayScale | decided | Tests, plus a screenshot on a 2x screen |
| Steady-run time-step row | kept, dimmed | decided | none needed |
| Legend | top right of the plot, 11 pt | place decided; size assumed | Look with 8 series |
| Target line | 2.5 pt, 75 % white, dashed 8 / 5 | bolder decided; the numbers are assumed | Look on the dark background |

### Gauge padding

Gaps are fixed shares of the diameter, not fixed points. They grow with the gauges, and the rule that sets D stays a single formula.

- **Landscape, vertical gap s = 0.20 D** (about 23 pt on an 11-inch iPad with the header band, was 8 pt). Each gauge's caption sits in the opening below its arc, so the gap must be wide enough that the caption does not read as belonging to the next gauge's top arc.
- **Portrait, horizontal gap s = 0.15 D** (about 18 pt, was 8 pt). Neighbouring rings only need clear space between their outer edges, which 0.15 D gives while each gauge shrinks by about 6 % (118.8 pt against 127.0 pt).
- Raising a ratio shrinks the gauges. At 0.20 D the landscape gauges are about 12 % smaller than with the old 8 pt gap.

The console drawer covers only the chart and bars, so the meters stay visible.

### Project structure

```text
FluentMon/
  FluentMonApp.swift          // scene, all orientations, isIdleTimerDisabled while connected
  Net/Messages.swift          // Codable structs, one per message type
  Net/MonitorClient.swift     // URLSessionWebSocketTask, auth header, backoff, decode
  Net/Discovery.swift         // NWBrowser for _fluentmon._tcp
  Model/MonitorStore.swift    // @Observable @MainActor: session, series, bars, sys, console
  Model/SeriesBuffer.swift    // full arrays + decimated view for the chart
  Views/DashboardView.swift   // places views at DashboardGeometry frames
  Views/ResidualChart.swift
  Views/ProgressBars.swift
  Views/ArcMeter.swift
  Layout/DashboardGeometry.swift  // pure: size -> frames, unit-tested
  Layout/BarMetrics.swift
  Layout/SceneProbe.swift
  Views/HeaderBand.swift      
  Views/ConsoleDrawer.swift
  Views/ConnectSheet.swift    // discovered servers, manual host, token entry (Keychain)
  Views/SettingsSheet.swift   // target, exclude list, meter CPUs
```

Info.plist keys: `NSLocalNetworkUsageDescription`, `NSBonjourServices = [_fluentmon._tcp]`, `NSAppTransportSecurity > NSAllowsLocalNetworking = YES`, and all four orientations in `UISupportedInterfaceOrientations~ipad`. A free Apple ID can install the app from Xcode, but the provisioning profile lasts 7 days, so plan to re-run it from Xcode weekly.

### Layout

All geometry lives in one value type that takes the available size and the bars' height and returns every frame. It is a pure function, so XCTest can check the alignment rules directly; the views only place what it returns.

```swift
struct DashboardGeometry: Equatable {
    enum Mode { case landscape, portrait }
    static let pad: CGFloat = 16, gap: CGFloat = 12, headerHeight: CGFloat = 32
    static let vGapRatio: CGFloat = 0.20, hGapRatio: CGFloat = 0.15
    static let k: CGFloat = (1 + sin(.pi / 4)) / 2

    let mode: Mode
    let header: CGRect, chart: CGRect, bars: CGRect
    let meters: [CGRect]
    let gaugeDiameter: CGFloat

    init(size: CGSize, mode: Mode, barsHeight: CGFloat, displayScale: CGFloat) {
        let p = Self.pad, g = Self.gap, h = Self.headerHeight, k = Self.k
        let W = size.width - 2 * p, H = size.height - 2 * p
        let top = p + h + g
        let bottom = p + H
        let d: CGFloat
        let m: [CGRect], c: CGRect, b: CGRect

        switch mode {
        case .landscape:
            d = (H - h - g) / (6 * k + 5 * Self.vGapRatio)
            let s = Self.vGapRatio * d
            let xR = p + W - d
            let leftW = W - g - d
            m = (0..<6).map { i in
                CGRect(x: xR, y: top + CGFloat(i) * (k * d + s), width: d, height: k * d)
            }
            b = CGRect(x: p, y: bottom - barsHeight, width: leftW, height: barsHeight)
            c = CGRect(x: p, y: top, width: leftW, height: b.minY - g - top)
        case .portrait:
            d = W / (6 + 5 * Self.hGapRatio)
            let s = Self.hGapRatio * d
            m = (0..<6).map { i in
                CGRect(x: p + CGFloat(i) * (d + s), y: top, width: d, height: k * d)
            }
            b = CGRect(x: p, y: bottom - barsHeight, width: W, height: barsHeight)
            let chartTop = top + k * d + g
            c = CGRect(x: p, y: chartTop, width: W, height: b.minY - g - chartTop)
        }

        self.mode = mode
        header = Self.snap(CGRect(x: p, y: p, width: W, height: h), displayScale)
        chart = Self.snap(c, displayScale)
        bars = Self.snap(b, displayScale)
        meters = m.map { Self.snap($0, displayScale) }
        gaugeDiameter = d
    }

    static func snap(_ r: CGRect, _ scale: CGFloat) -> CGRect {
        let x0 = (r.minX * scale).rounded() / scale
        let y0 = (r.minY * scale).rounded() / scale
        let x1 = (r.maxX * scale).rounded() / scale
        let y1 = (r.maxY * scale).rounded() / scale
        return CGRect(x: x0, y: y0, width: x1 - x0, height: y1 - y0)
    }
}

enum LayoutMode {
    static func resolve(size: CGSize, isFullScreen: Bool, orientation: UIInterfaceOrientation,
                        previous: DashboardGeometry.Mode) -> DashboardGeometry.Mode {
        if isFullScreen, orientation != .unknown {
            return orientation.isPortrait ? .portrait : .landscape
        }
        let ratio = size.width / size.height
        switch previous {
        case .landscape: return ratio < 0.95 ? .portrait : .landscape
        case .portrait: return ratio > 1.05 ? .landscape : .portrait
        }
    }
}

extension View {
    func place(_ r: CGRect) -> some View {
        frame(width: r.width, height: r.height).offset(x: r.minX, y: r.minY)
    }
}

struct SceneState: Equatable {
    var isFullScreen = true
    var orientation: UIInterfaceOrientation = .landscapeLeft
}

struct DashboardView: View {
    @Environment(MonitorStore.self) private var store
    @Environment(\.displayScale) private var displayScale
    @ScaledMetric(relativeTo: .body) private var textScale: CGFloat = 1
    @State private var scene = SceneState()
    @State private var mode: DashboardGeometry.Mode = .landscape

    var body: some View {
        GeometryReader { proxy in
            let full = BarMetrics(scale: textScale)
            let first = DashboardGeometry(size: proxy.size, mode: mode, barsHeight: full.height, displayScale: displayScale)
            let compact = first.chart.height < 300
            let metrics = compact ? full.compact : full
            let geo = compact
                ? DashboardGeometry(size: proxy.size, mode: mode, barsHeight: metrics.height, displayScale: displayScale)
                : first

            ZStack(alignment: .topLeading) {
                HeaderBand(session: store.session, connection: store.connection,
                           lastIteration: store.lastIterationAt).place(geo.header)
                ResidualChart(store: store).place(geo.chart)
                ProgressBars(bars: store.bars, metrics: metrics).place(geo.bars)
                ForEach(Array(store.meters.enumerated()), id: \.offset) { i, meter in
                    ArcMeter(meter: meter, diameter: geo.gaugeDiameter).place(geo.meters[i])
                }
                ConsoleDrawer(console: store.console).place(geo.chart.union(geo.bars))
            }
            .animation(.snappy(duration: 0.35), value: geo)
            .onChange(of: proxy.size, initial: true) { _, size in
                mode = LayoutMode.resolve(size: size, isFullScreen: scene.isFullScreen,
                                          orientation: scene.orientation, previous: mode)
            }
            .onChange(of: scene) { _, s in
                mode = LayoutMode.resolve(size: proxy.size, isFullScreen: s.isFullScreen,
                                          orientation: s.orientation, previous: mode)
            }
        }
        .background(SceneProbe(state: $scene))
        .background(Color(white: 0.06))
        .preferredColorScheme(.dark)
    }
}
```

Rules the unit tests assert, for every size in a table of iPad windows (full screen, Split View, Stage Manager):

- Landscape: `header.minY == pad`, `header.maxY + gap == chart.minY == meters[0].minY`, `meters.last!.maxY == bars.maxY == size.height - pad` (one snapped number, so exact), `chart.maxY + gap == bars.minY`, `meters[i+1].minY - meters[i].maxY == vGapRatio * gaugeDiameter` within 1 / displayScale, `meters[i].maxX == size.width - pad`, and `chart.width + gap + meters[0].width == size.width - 2 * pad` within 1 / displayScale.
- Portrait: `header.maxY + gap == meters[0].minY`, all meters share one `minY`, `meters[i+1].minX - meters[i].maxX == hGapRatio * gaugeDiameter` within 1 / displayScale, `meters.last!.maxX == size.width - pad`, `chart.minY == meters[0].maxY + gap` within 1 / displayScale, `bars.maxY == size.height - pad`.
- Both: the header spans `size.width - 2 * pad`; every edge is a multiple of `1 / displayScale`; `chart.height >= 220` down to the smallest supported window, or the test names the size that breaks it. Other equality checks use a 0.01 pt tolerance.
- `LayoutMode.resolve`: full-screen rotation in all four orientations, windowed sizes either side of the 0.95 and 1.05 band, and the previous-mode hysteresis between them.

Resulting sizes at the default text size with the 32 pt header band, assuming a 24 pt status bar and a 20 pt home-indicator inset:

| Window | Content area W × H (pt) | Gauge diameter D (pt) | Gauge gap s (pt) | Chart (pt) | Bars block (pt) |
| --- | --- | --- | --- | --- | --- |
| 11-inch, landscape | 1162 × 758 | 116.6 | 23.3 (vertical) | 1033 × 448 | 254 |
| 13-inch, landscape | 1344 × 956 | 149.0 | 29.8 (vertical) | 1183 × 646 | 254 |
| 11-inch, portrait | 802 × 1118 | 118.8 | 17.8 (horizontal) | 802 × 695 | 254 |
| 13-inch, portrait | 1000 × 1300 | 148.1 | 22.2 (horizontal) | 1000 × 852 | 254 |

Portrait is still provisional: it keeps the simplest defaults (header band, then the meters full width in one row, then the chart and bars full width). The orientation trigger and the header band are fixed; the remaining relations are open and listed in Improvements. Changing them touches only the `.portrait` case and its tests.

### Residual chart (Swift Charts)

```swift
Chart {
    ForEach(store.visibleSeries) { s in
        ForEach(s.points) { p in                       // decimated, NaN and <= 0 removed
            LineMark(x: .value("Iteration", p.iter),
                     y: .value("Residual", p.value),
                     series: .value("Equation", s.name))
                .foregroundStyle(by: .value("Equation", s.name))
                .lineStyle(StrokeStyle(lineWidth: 1.5))
        }
    }
    RuleMark(y: .value("Target", store.target))
        .lineStyle(StrokeStyle(lineWidth: 2.5, dash: [8, 5]))
        .foregroundStyle(.white.opacity(0.75))
}
.chartYScale(domain: store.yDomain, type: .log)       // whole decades, always includes target / 10
.chartXScale(domain: store.xDomain)
.chartForegroundStyleScale(domain: store.names, range: Palette.residuals)
.chartLegend(position: .overlay, alignment: .topTrailing, spacing: 8)
.chartXSelection(value: $store.scrubIter)             // drag to read all values at one iteration
```

- **Domains.** y spans whole decades from floor(log10 min) to ceil(log10 max), and never above 1e1 or below target / 10. x offers three ranges from a segmented control: All, Last 500, Last 100.
- **Point budget.** `SeriesBuffer` keeps every point but exposes at most 1,200 per series, rebuilt with min/max bucketing when the count grows 5 % or the x-range changes. The chart is invalidated at most 4 times a second, however fast residuals arrive.
- **Fallback.** If Instruments shows chart frames over 16 ms with 8 series, replace the `Chart` with a `Canvas` that maps log10 values to y and strokes one `Path` per series. The view's inputs stay the same.
- **Colours.** Fixed per equation so they never reshuffle: continuity red, x/y/z-velocity blue, green, amber, energy purple, k cyan, omega or epsilon pink, others from a categorical list. Report columns are hidden by default (toggle in Settings) because their scale is unrelated.
- **Legend and target**. The legend sits in the plot's top-right corner, over the empty area above the falling residuals, 11 pt, 8 entries at most. The 1e-6 target line is 2.5 pt, 75 % white and dashed 8 / 5, so it reads as the goal and not as a gridline. The old header strip is now the header band in the geometry.

### Progress bars

Sizes are chosen for reading the iPad on a desk stand at arm's length, not in the hand. All of them come from one `BarMetrics` value, so the geometry knows the block's height before layout and never has to measure it.

| Element | Size | Why |
| --- | --- | --- |
| Title (left) and value (right) | 17 pt semibold; value with `.monospacedDigit()` | iOS body size; fixed-width digits stop the value jittering as it counts |
| Gap to track | 8 pt | separates the text from the bar it describes |
| Track | 12 pt capsule, white at 12 % behind the fill | thick enough to compare fills at a glance |
| Gap to caption | 6 pt | ties the caption to its bar |
| Caption | 14 pt secondary, one line, truncated in the middle | keeps both ends of `Limiting: continuity 2.3e-03 · 3.4 / 6.0 decades` |
| Row | 66 pt | 22 + 8 + 12 + 6 + 18 |
| Between rows | 14 pt | more than the 6–8 pt inside a row, so rows read as separate units |
| Card padding | 16 pt sides, 14 pt top and bottom |  |
| Whole block | 254 pt | 2 × 14 + 3 × 66 + 2 × 14 |

```swift
struct BarMetrics: Equatable {
    var scale: CGFloat = 1
    var showCaptions = true
    var padV: CGFloat { 14 * scale }
    var row: CGFloat { (22 + 8 + 12 + (showCaptions ? 6 + 18 : 0)) * scale }
    var rowGap: CGFloat { 14 * scale }
    var height: CGFloat { 2 * padV + 3 * row + 2 * rowGap }       // 254 at scale 1, 182 compact
    var compact: BarMetrics { var c = self; c.showCaptions = false; return c }
}
```

- Everything scales with Dynamic Type through the one `@ScaledMetric`; the bars view is capped at `.dynamicTypeSize(...DynamicTypeSize.xLarge)` so large text cannot squeeze the chart away.
- Compact mode: if the chart would end up shorter than 300 pt (small Split View windows), captions are dropped, rows become 42 pt and the block 182 pt. Fill animates with `.easeOut(duration: 0.3)` in both modes.
- Convergence tick marks: 1 pt wide, white at 30 % opacity, drawn on top of the fill and the grey track, track height plus 3 pt, so they stay readable in both. They are dropped in compact mode only if two ticks would be closer than 8 pt. Style is assumed; confirm by looking at the bar on the desk stand in phase 0.

| Bar | Fill colour | Caption |
| --- | --- | --- |
| Iterations | blue accent; green at converged | `518 left · ETA 0:12:38` |
| Convergence to 1e-6 | teal when `ok`, amber `stalled`, red `rising` or `nan` | `Limiting: continuity 2.3e-03 · 3.4 / 6.0 decades · ≈ 1,900 iters` |
| Time steps | purple; grey and `Steady run` when `step` is null | `t = 0.0120 / 0.1000 s · ETA 2:41:05` |

### Arc meters (no needles, no scales)

The arc is a `Circle().trim` rotated 135°, which gives a 270° sweep opening at the bottom; `trim` animates on its own, so no custom `Animatable` shape is needed.

The meter is described by data, so the same view draws a CPU, the power gauge with its inner ring, and RAM.

```swift
struct Segment: Identifiable {
    let id: Int
    let from: Double, to: Double
    let color: Color
}

struct Meter: Identifiable {
    let id: Int
    let title: String
    let major: String
    let minor: String?
    var minorColor: Color = .primary.opacity(0.85)
    let outer: [Segment]
    var inner: [Segment] = []
}

struct ArcMeter: View {
    let meter: Meter
    let diameter: CGFloat
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    private let sweep = 0.75
    private var lw: CGFloat { max(8, 0.085 * diameter) }
    private var lwInner: CGFloat { max(5, 0.045 * diameter) }
    private var innerInset: CGFloat { lw + 0.03 * diameter }

    var body: some View {
        ZStack {
            ring(0, 1, .white.opacity(0.10), width: lw, inset: 0)
            ForEach(meter.outer) { s in ring(s.from, s.to, s.color, width: lw, inset: 0) }
            if !meter.inner.isEmpty {
                ring(0, 1, .white.opacity(0.10), width: lwInner, inset: innerInset)
                ForEach(meter.inner) { s in ring(s.from, s.to, s.color, width: lwInner, inset: innerInset) }
            }
            VStack(spacing: 0.02 * diameter) {
                Text(meter.title)
                    .font(.system(size: max(10, 0.075 * diameter), weight: .light))
                    .foregroundStyle(.secondary)
                Text(meter.major)
                    .font(.system(size: 0.17 * diameter, weight: .regular, design: .rounded).monospacedDigit())
                if let minor = meter.minor {
                    Text(minor)
                        .font(.system(size: max(10, 0.085 * diameter), weight: .regular).monospacedDigit())
                        .foregroundStyle(meter.minorColor)
                }
            }
            .lineLimit(1).minimumScaleFactor(0.8)
            .frame(maxWidth: 0.6 * diameter)
        }
        .frame(width: diameter, height: diameter)
        .frame(height: DashboardGeometry.k * diameter, alignment: .top)
    }

    private func ring(_ a: Double, _ b: Double, _ c: Color, width: CGFloat, inset: CGFloat) -> some View {
        Circle()
            .inset(by: inset + width / 2)
            .trim(from: a * sweep, to: max(a, b) * sweep)
            .stroke(c, style: StrokeStyle(lineWidth: width, lineCap: .butt))
            .rotationEffect(.degrees(135))
            .animation(reduceMotion ? nil : .linear(duration: 0.2), value: b)
    }
}
```

The thermal scale and the three meter builders:

```swift
struct Thermal {
    let baseC: Double, warnC: Double, maxC: Double

    func fraction(_ t: Double) -> Double {
        min(1, max(0, (t - baseC) / (maxC - baseC)))
    }

    func color(_ t: Double) -> Color {
        if fraction(t) > 0.9 { return .red }
        if t > warnC { return .yellow }
        return Palette.cool
    }
}

func cpuMeter(_ i: Int, id: Int, load: Double, mhz: Double?) -> Meter {
    Meter(id: i, title: "CPU \(id)", major: "\(Int(load.rounded())) %",
          minor: mhz.map { String(format: "%.1f GHz", $0 / 1000) } ?? "—",
          outer: [Segment(id: 0, from: 0, to: load / 100, color: Palette.cool)])
}

func powerMeter(watts: Double, maxW: Double, tdpW: Double, tempC: Double, thermal: Thermal) -> Meter {
    let c = thermal.color(tempC)
    return Meter(id: 4, title: "Power", major: "\(Int(watts.rounded())) W",
                 minor: "\(Int(tempC.rounded())) °C", minorColor: c,
                 outer: [Segment(id: 0, from: 0, to: watts / maxW, color: watts > tdpW ? Palette.hot : Palette.cool)],
                 inner: [Segment(id: 0, from: 0, to: thermal.fraction(tempC), color: c)])
}

func ramMeter(used: Double, fluent: Double, total: Double) -> Meter {
    Meter(id: 5, title: "RAM", major: "\(Int((used / total * 100).rounded())) %",
          minor: String(format: "Fluent %.1f GB", fluent / 1e9), minorColor: Palette.fluent,
          outer: [Segment(id: 0, from: 0, to: fluent / total, color: Palette.fluent),
                  Segment(id: 1, from: fluent / total, to: used / total, color: .gray.opacity(0.6))])
}
```

Every meter has three text tiers, all scaled by the diameter D: a small, light label above (7.5 % of D, secondary colour); the major value in the middle (17 % of D, regular weight); and a minor value below in the same small size class as the label but a little larger and brighter (8.5 % of D, regular weight, 85 % white). Text never goes below 10 pt. The outer stroke is 8.5 % of D (12 pt at 140 pt). The power meter's inner ring is 4.5 % of D thick and starts one outer stroke plus 3 % of D inside the edge, which leaves a clear inner circle about 0.68 D across for the three lines. The meter takes only k × D of height, which is what lets the six gauges meet the bottom edge exactly.

| Meter | Label | Major | Minor | Arcs |
| --- | --- | --- | --- | --- |
| CPU 0 / 4 / 8 / 12 | `CPU 4` | load, `97 %` | core clock, `4.9 GHz` | load ÷ 100 in one cool accent (#3BA7FF); a pinned Fluent rank sits near full by design, so no red |
| Power | `Power` | package power, `71 W` | CPU temperature, `68 °C` | outer: W ÷ `pkg_max_w`, cool accent up to the 120 W TDP, orange above it, hatched track if `pkg_w` is null. Inner (thinner, concentric): (T − 20) ÷ (limit − 20), cool below 70 °C, yellow above, red above 90 % of the arc |
| RAM | `RAM` | used share of total, `62 %` | Fluent's memory, `Fluent 12.0 GB` in the Fluent tint | Fluent ÷ total in bright magenta (#FF2D95), then other used ÷ total in grey at 60 %; whole arc tinted red above 90 % used |

There is no separate stack container: `DashboardGeometry` places each meter directly, so the landscape column and the portrait row use the same `ArcMeter`. CPU values are smoothed with an EMA (α = 0.5) when a `sys` message arrives. In landscape the meter column is exactly one gauge wide, so it has no side margin.

### Store and networking

- `MonitorClient` decodes on a background task: read the envelope's `t` first, then decode the matching struct; hop to `@MainActor` only to mutate `MonitorStore`.
- Reconnect on `scenePhase == .active`; iPadOS suspends sockets in the background, which is fine because every reconnect starts with a snapshot.
- `UIApplication.shared.isIdleTimerDisabled = true` while connected, so the iPad can sit on a stand as a dedicated monitor.
- `MonitorStore` also keeps `connection` (live, stale or offline), `sysAt` and `lastIterationAt`. The connection is stale when no `sys` has arrived for 7 s; the header and the gauges read these values.

## 8. Console livestream (suggested design)

Stream the console as text, not video: every line Fluent prints already lands in the transcript the server is tailing, so the livestream is the same tailer sending each line as a `con` message to a terminal-style view on the iPad. It costs almost nothing extra, works for GUI and batch runs, and stays readable on a tablet, which a screen capture of the Fluent window would not.

| Option | How it works | Verdict |
| --- | --- | --- |
| **Transcript tail** | Same file as the residuals; lines sent in `con` messages | **Recommended.** Read-only, about 1 s latency, no extra setup |
| stdout pipe | Launcher starts Fluent with stdout piped to the server | Batch (`-g`) runs only; the GUI console does not go to stdout |
| PyFluent transcript stream | gRPC callback per line from an attached session | Fallback if the transcript flushes slowly; needs `-sifile` and version-specific calls |
| Window capture | Remote desktop (RDP, RustDesk, Parsec) of the console window | Do not build it; install a remote desktop app if you need full control |

### Server side

- Keep a `collections.deque(maxlen=20_000)` of `(line_no, kind, text)`; `line_no` is absolute from the start of the transcript.
- Classify each line once when parsed (`h`, `r`, `w`, `e`, `s`, `n`, as in section 6) and send the classification with it.
- Send the last 500 lines in the snapshot; answer `con_back` requests for older lines from the deque, or by re-reading the file if they have fallen out.
- Stop sending `con` to clients that sent `{"t":"sub","console":false}` (drawer closed); the snapshot tail keeps them current when it opens.

### iPad side: `ConsoleDrawer`

- **Three heights.** Collapsed: a 28 pt handle showing the newest non-residual line as a ticker, so warnings are visible without opening anything. Half: 45 % of the left column, chart still visible above. Full: covers the chart and bars; the meters stay visible on the right. Drag the handle or tap to cycle.
- **Rendering.** `ScrollViewReader` + `LazyVStack(alignment: .leading)` of one `Text` per line, `.font(.system(size: 12, design: .monospaced))`, `.lineLimit(1)` with a wrap toggle, each row's `id` = its line number. Keep 5,000 lines on the client.
- **Follow mode.** While the view is scrolled to the bottom, new batches call `proxy.scrollTo(lastID, anchor: .bottom)` (without animation when a batch has more than 50 lines). Scrolling up turns follow off and shows a `Live ↓ 128 new` pill that jumps back. On iPadOS 17 use `.defaultScrollAnchor(.bottom)` and `.scrollPosition(id:)` to know where you are.
- **Colours by kind.** Error red, warning amber, time-step lines purple, headers dim bold, residual rows dim grey, everything else primary. A `Hide residual rows` toggle (on by default) leaves only messages, since the chart already shows the numbers.
- **Search.** A filter field that shows only matching lines, and long-press to copy a line.
- **If it stutters.** At very high line rates a `LazyVStack` can drop frames; swap in a `UITextView` (wrapped in `UIViewRepresentable`) and append `NSAttributedString` chunks to its `textStorage`. The drawer's API does not change.

### Sending commands back (later, deliberately limited)

Typing arbitrary TUI commands from the iPad is possible through PyFluent but risky: one wrong command can overwrite a case file. If you add control, make it two or three fixed buttons behind a confirmation dialog:

- **Save and stop** and **Save and continue** via Fluent's checkpoint files: Fluent checks during the run for an exit file and a check file and, when one appears, writes data (and exits, for the exit file). The server only has to create an empty file. Confirm the paths your version watches with `(rpgetvar 'checkpoint/exit-filename)` and `(rpgetvar 'checkpoint/check-filename)` in the Fluent console before relying on it.
- **Interrupt** via the PyFluent connection from section 1, if that phase is built.

## 9. Networking, discovery and security

Use Bonjour at home, Tailscale everywhere else, a bearer token always, and keep the server read-only by default. Campus Wi-Fi commonly blocks device-to-device traffic and mDNS, so do not rely on the LAN path working at university.

### Discovery on a home LAN

- Server: `zeroconf` advertises `_fluentmon._tcp.local.` on port 8765 with TXT `proto=1`, `ver=0.1.0`, `name=YOUNGMIN-PC`.
- iPad: `NWBrowser(for: .bonjour(type: "_fluentmon._tcp", domain: nil), using: .tcp)` lists servers in `ConnectSheet`. Resolve a result by opening a short `NWConnection` and reading `currentPath?.remoteEndpoint` for the host and port, then hand `ws://host:port/ws` to `URLSessionWebSocketTask`.
- Always offer manual entry (host, port) as well; it is the path you will actually use on campus.

### Anywhere else: Tailscale

1. Install Tailscale on the workstation and the iPad, same account.
2. Simplest: connect to the PC's Tailscale IP, `ws://100.x.y.z:8765/ws`. Plain `ws://` to an IP literal is allowed with `NSAllowsLocalNetworking`.
3. Better: run `tailscale serve --bg 8765` on the PC. Tailscale terminates HTTPS with a valid certificate, so the app connects to `wss://youngmin-pc.<tailnet>.ts.net/ws` with no ATS exceptions at all, and FluentMon can bind to `127.0.0.1` only.
4. Never port-forward 8765 to the internet.

### Authentication and permissions

- Token: generated once with `secrets.token_urlsafe(24)` into `config.toml`; entered on the iPad once and stored in the Keychain.
- Pairing shortcut: at startup the server prints a QR code (Python `qrcode` package) encoding `fluentmon://connect?host=...&port=8765&token=...`; the app registers the `fluentmon` URL scheme, so scanning it with the iPad camera fills in the connect sheet.
- Read-only by default. Any control endpoint (section 8) is compiled in but refused unless `allow_control = true` in `config.toml`, and each request is logged.
- LibreHardwareMonitor's port 8085 stays local; only FluentMon reads it.

### Alerts while the iPad is asleep

iPadOS suspends the WebSocket when the app is in the background, and real push notifications (APNs) need the paid Apple Developer Program. A free workaround: on `converged`, `problem` and `idle` events the server posts to an ntfy topic, and the ntfy iOS app shows it as a normal notification.

```python
await client.post(f"https://ntfy.sh/{cfg.ntfy_topic}",            # long random topic name
                  content=f"{case}: converged at iteration {it}",
                  headers={"Title": "FluentMon", "Priority": "default", "Tags": "white_check_mark"})
```

A Telegram bot message via the Bot API works just as well if you prefer it; either way it is about 10 lines in a `notify.py` subscribed to the event bus.

## 10. Testing, build order and things to verify

Build against recorded transcripts first, so the parser and the iPad UI can be developed without Fluent running; the solver only comes back in at the end-to-end step.

### Test harness

- **Fixtures (phase 0).** Record four transcripts: a steady RANS case, a transient case with a few hundred time steps, a deliberately diverging case (for example, all under-relaxation factors at 1), and a case with report definitions printed to the console.
- **Replay tool.** `python tools/replay.py fixtures/transient_les.trn --ips 10` writes the fixture into `C:\FluentMon\transcripts\replay.trn` at 10 iterations per second (everything up to the next data line per tick). `--burst` writes it all at once to test catch-up mode.
- **Parser tests (pytest).** Header parsing, value-count mismatch, NaN tokens, CRLF, re-initialisation, transient steps, convergence. Add a property test with `hypothesis`: feeding a fixture in random chunk sizes must produce exactly the same events as feeding it whole, which catches every partial-line bug.
- **Progress tests.** Synthetic residual sequences with known answers (1 → 1e-3 must give 50 %; a rising sequence must give `rising`; a flat one `stalled`).
- **Message recording.** `python -m fluentmon --record out.jsonl` saves every outgoing message. The iPad's `MockClient` plays that file back, which drives SwiftUI previews and lets you build the whole UI away from the PC.
- **Soak.** 12-hour replay at 20 iterations/s; watch server memory in Task Manager and the app in Instruments (Allocations, Hitches).

### Build order

1. **Recon (half a day).** Record the fixtures, count change notifications against appended lines, check pipe buffering, confirm the residual-line format and Fluent's process names in Task Manager, and confirm LHM shows a CPU package power sensor, and record the settings-file keys that switch on LHM's web server and the release asset name, which section 14 assumes. Exit: four fixture files and notes.
2. **Parser and replay (1–2 days).** Exit: pytest green on all fixtures, including the chunking property test.
3. **Server stream (1 day).** PC poll after every broadcast, WebSocket, snapshot, coalescing. Exit: a 20-line Python test client prints sensible `bars` and `sys` messages during a replay.
4. **iPad dashboard (2–3 days).** Layout split, residual chart, bars, arc meters, first on `MockClient`, then live. Exit: a real Fluent run shown end to end.
5. **Console drawer (1 day).** Exit: follow mode, scroll-back and colouring work during a replay at 30 lines/s.
6. **Hardening (1 day).** Bonjour, token and QR pairing, `tailscale serve`, ntfy alerts.
7. **Installer (1–2 days).** `fmon setup`, `fmon doctor`, `fmon uninstall` and `install.ps1` (section 14), which take over the scheduled tasks and the firewall rule. Exit: on a clean Windows 11 VM the installer ends with a green `fmon doctor`, and the iPad pairs by QR code.
8. **Optional.** PyFluent channel, checkpoint buttons, rank pinning, CSV export button.

The effort figures are rough estimates for one person who already knows Swift and Python.

### Things to verify before relying on them

| Item | What could differ | Check and fallback |
| --- | --- | --- |
| Transcript flush | Lines arrive in delayed chunks | `Get-Content -Wait` during a run; fall back to PyFluent's transcript stream |
| Residual line format | Column order, ETA column or wording varies by version | Phase 0 fixtures and parser tests |
| Transient line wording | `Flow time` / `more time steps` phrasing | Transient fixture; patterns live in one module |
| Fluent process names | `fl_mpi*`, `cx*` naming changes | Task Manager > Details during a run; regex in config |
| Rank pinning | Fluent or MPI ignores `I_MPI_PIN_PROCESSOR_LIST` | Task Manager affinity; psutil `cpu_affinity` fallback |
| LHM power sensor | Different tree path, or driver blocked | Note the path in LHM's GUI; meter shows `no sensor` instead of 0 |
| Checkpoint file paths | Path or behaviour differs | `(rpgetvar 'checkpoint/exit-filename)` in the Fluent console |
| Student edition and PyFluent | gRPC session access may be limited | Try `connect_to_fluent` before starting phase 7 |
| Swift Charts speed | Dropped frames with many points | 1,200-point budget and 4 Hz refresh; `Canvas` fallback |
| Campus network | Client isolation blocks LAN and mDNS | Tailscale |
| Free provisioning | App stops launching after 7 days | Re-run from Xcode weekly |
| LHM clock and temperature sensors | Core numbering or the (Effective) clock sensors differ from the names assumed here | Read the names in LHM's GUI, fall back to Core #n, show a dash for a missing clock |
| PC poll cost | Polling psutil and LHM after every broadcast slows Fluent or delays a push | Benchmark P0; raise the 200 ms spacing or reuse the LHM read for 1 s |

### Open questions: UI geometry, readability and UX

None of these is decided yet.

1. **Safe areas and window size.** How the 16 pt padding combines with safe-area insets, and the smallest window (Split View, Slide Over, Stage Manager) before gauge text drops under the 10 pt floor.
2. **Orientation switch.** Portrait is provisional. Define the test for near-square windows and whether the chart keeps its zoom and scroll position across rotation.
3. **Bars block on steady runs.** Whether it stays 254 pt without the time-step bar or shrinks, and whether the chart takes the freed height.
4. **Minimum chart height.** Only the bars have a compact mode today; define what happens when the chart gets too short.
5. **Console drawer.** The 28 pt handle is under the 44 pt touch minimum; define its hit area, its portrait heights and its behavior when the keyboard opens.
6. **Text size.** All text scales with the gauge diameter; decide whether Dynamic Type applies, and set a minimum size for reading from 1 to 2 m on a stand.
7. **Colour-only meaning.** Cool, yellow, red and orange fills need a second cue (a tick at 120 W and at the red temperature, or a word), and the Fluent RAM tint needs a contrast check against the grey.
8. **Number stability.** Monospaced digits and fixed widest strings ("100 %", "162 W", "4.95 GHz") so values do not jitter.
9. **State views.** Disconnected, stale `sys`, no session, converged, diverged and missing-sensor states for every block, including what each gauge shows.
10. **Chart interaction.** Legend and series toggles, log-axis labels, x-axis behavior at 50,000 iterations, scrubbing to read values, and pinch zoom versus live follow.
11. **Foreground alerts.** Converged and problem events reach the user only through ntfy; decide whether the app shows its own banner.
12. **Idle display.** Brightness dimming and burn-in protection for long runs on a stand.

## Open questions: UI geometry and readability

Each item is undecided. A proposed default is marked assumed and says how it will be confirmed.

**Geometry still undefined**

- **Portrait fit.** At 11 inches the gauge diameter is about 119 pt, so the three text tiers (label, major, minor) may not fit "4950 MHz" or "96.8 %" at the 10 pt floor. Assumed: drop the clock line below a diameter of 110 pt. Confirm on an iPad Pro 11-inch in portrait.
- **Minimum window size.** Stage Manager, Split View and Slide Over give sizes that are not iPad screens. The geometry has no minimum and no behaviour for extreme aspect ratios. Assumed: below 640 × 480 pt show a single column of meters and a chart without bars. Confirm in Stage Manager.
- **Chart minimum height.** Compact bars trigger at a chart height of 300 pt, but nothing says what happens when the chart is still shorter in portrait. Assumed: hide the chart's axis labels first, then the bars' secondary lines.
- **Safe areas and rotation.** The 16 pt padding is not tied to the safe area or the home indicator, and the rotation animation is not defined. Assumed: padding sits inside the safe area, rotation is a 0.25 s cross-fade.
- **Console drawer geometry.** Landscape heights are defined (28 pt, 45 %, full). Portrait heights and whether the drawer covers the gauge row are not.

**Readability and UX still undefined**

- **Colour is the only signal.** Orange, yellow and red on the power gauge carry meaning without any second cue. Needs a colour-blind-safe palette check and one non-colour cue, such as the minor text turning bold.
- **Stale or missing data.** No visual state exists for a missing sensor, a stale `sys` (older than about 6 s, given the 5 s idle fallback), a dropped connection or a finished session. Assumed: dim the arc and show a dash.
- **Chart reading.** Undefined: which residual is highlighted (the limiting one), series toggles, tap-to-inspect an iteration, and how scroll-back relates to the live edge.
- **In-app alerts.** Converged, diverged and idle currently reach the user only through ntfy. Decide whether the dashboard also shows a banner.
- **Arc motion.** With `sys` up to 5 times a second, the arc animation duration and the EMA smoothing could fight each other. Assumed: 0.2 s ease with the existing EMA kept. Confirm by eye on a real run.
- **Dynamic Type and brightness.** Fixed type sizes ignore Dynamic Type. A dedicated stand monitor may also need a dim mode. Decide both.

## Open questions: UI geometry, readability and UX

Each point lists what is undefined and a proposed default. Every default is assumed until confirmed on the real iPad.

### Geometry

- **Window sizes.** Only four full-screen sizes are tabulated, but Stage Manager and Split View give arbitrary windows, and the landscape/portrait switch is width ≥ height. Define a minimum window and a fallback below it. Assumed: full-screen only in v1.
- **Safe areas.** The 16 pt pad is fixed. Define it as max(16, safe-area inset) per edge. Assumed.
- **Portrait console drawer.** Its three heights (28 pt, 45 %, full) are specified against the left column only. Define what it covers in portrait and whether the gauge row stays visible. Assumed: gauge row stays visible.
- **Narrow gauge text.** At portrait D ≈ 119 pt the minor line ("4.9 GHz", "Fluent 12.0 GB") must fit at the 10 pt floor. Define truncation, for example drop the unit first. Assumed.
- **Very short chart.** Compact bars start below a 300 pt chart; nothing is defined below about 150 pt. Assumed: hide the convergence prediction text there.
- **Rotation.** Define whether drawer height and scroll-back position survive a rotation. Assumed: they do.

### Readability

- **Colour and contrast.** Choose dark-only or system theme, tokens with at least 4.5:1 contrast, and a non-colour cue for the temperature ring (a state word or symbol) for colour-blind users. Assumed: dark only.
- **Viewing distance.** At 1 m on a stand, the 10 pt floor and the 8.5 % minor tier are too small. Decide between arm's length only and a far-view mode that enlarges values. Assumed: arm's length.
- **Residual chart.** Undefined: series colours beyond 8 columns, which series is emphasised (the limiting one), line widths, the 1e-6 target line style, the x-axis window (all iterations or the last N) and log-axis tick labels.
- **Chart inspection.** Decide whether touch shows a crosshair or the chart is display-only. Assumed: display-only.
- **Number formats.** Fix one table for ETA (h:mm:ss or "12 min"), residuals (2.3e-3), the transient time-step label and the "—" placeholder.
- **Motion.** Arc animation duration and gauge EMA α (0.5, assumed) at up to 5 updates a second; suppress animation for changes under 1 % of the arc.
- **Dynamic Type.** Meter text scales with D. Decide whether it also follows the system text size. Assumed: no.

### UX

- **Stale and disconnected.** Define when data counts as stale (assumed: no `sys` for 10 s), how the gauges dim, the reconnect banner and what shows before the first data.
- **No session, paused, finished.** Define what the bars, chart and RAM tint show when Fluent is not running, idle, converged or diverged, and how long that state stays on screen.
- **Multiple sessions.** Define which run is shown when two exist and how to switch.
- **Console drawer.** Define auto-follow versus pause on scroll, tap to pause, font size and long-line handling.
- **First run.** Define the discovery, token entry and manual host screens and the wrong-token message.
- **On-screen alerts.** Define the treatment of `converged` and `problem` events beyond ntfy. Assumed: a banner and a bar colour change, no sound.

## Sources

- [Microsoft Defender alert: VulnerableDriver:WinNT/Winring0](https://support.microsoft.com/windows/security/threat-malware-protection/microsoft-defender-antivirus-alert-vulnerabledriver-winnt-winring0) — why older LibreHardwareMonitor builds get quarantined
- [Neowin: Windows is flagging WinRing0 in monitoring and fan-control apps](https://www.neowin.net/news/windows-1110-is-flagging-winring0-on-your-pc-monitoring-fan-control-apps-heres-why/) — includes the move to the PawnIO build of LHM

* [AMD Ryzen 7 7800X3D product page](https://www.amd.com/en/products/processors/desktops/ryzen/7000-series/amd-ryzen-7-7800x3d.html) — core and thread count, base and boost clock, 89 °C maximum
* [AnandTech: Ryzen 7 7800X3D review](https://www.Anandtech.Com/show/18795/the-amd-ryzen-7-7800x3d-review-a-simpler-slice-of-v-cache-for-gaming/2) — 120 W TDP and 162 W PPT

Everything else (Fluent transcript formats, PyFluent calls, iOS and Python APIs) is from general knowledge and is listed under "Things to verify" where it matters.

## 11. Improvements

Unresolved layout and UX points, each with the temporary fix that applies now. The third column is the open work.

| Point | Temporary fix now | Still to improve | Status |
| --- | --- | --- | --- |
| Portrait gauge text fit | Labels hold the 10 pt floor; text stays on one line with `minimumScaleFactor(0.8)` | Measure "CPU 12" and "Fluent 12.0 GB" at D of about 119 pt on the 11-inch; shorten the minor text if it truncates | assumed |
| Small windows and safe area | The content area is the safe-area size and the 16 pt pad sits inside it. Below 640 × 480 pt the dashboard shows "Enlarge the window" | Set a real minimum and a reduced layout (fewer gauges, no minor lines) | assumed (640 × 480) |
| Console drawer rectangle | Covers the union of chart and bars in both orientations; header and meters stay visible | Check that its drag handle does not fight chart scrubbing | decided |
| Label weight and contrast | Keep the light 10 pt label at secondary colour, as requested earlier | Measure contrast on the dark background; raise weight or size if it is below 4.5:1 | assumed |
| Digit jitter | `monospacedDigit()` is already on meter and bar values; add it to the header age | Fixed-width slots for values that gain a digit (9 to 10 %) | decided |
| Chart internals | Legend 11 pt, 8 entries at most, fixed equation colours | Decade tick labels, a colour-blind check of the palette, a highlight for the limiting residual | assumed |
| Colour as the only cue | The status word in the header; power and temperature keep their coloured minor text | A non-colour cue on the arcs, such as a marker | assumed |
| Stale or missing data | No `sys` for 7 s: gauges grey with a dash. Connection lost: the header says Disconnected and the chart and bars dim to 50 % | Tune the 7 s; handle a single missing sensor separately | assumed (7 s) |
| Empty and idle states | No session: header reads "No session", chart shows "Waiting for a Fluent run", gauges stay live | A finished-run summary | decided |
| Interaction | Only chart scrubbing and the All / Last 500 / Last 100 control | Pinch zoom, a follow-live toggle, hiding a series | deferred: not needed for a first version |
| Arc motion | Linear 0.2 s fill, none with Reduce Motion | Check at 5 updates a second on the device | assumed |
| Distance mode | Text follows Dynamic Type; the bars are capped at xLarge | An optional large-text mode that hides minor lines | deferred: needs the device on its stand to judge |
| Scene APIs | `SceneProbe` reads `isFullScreen` and the interface orientation from the window scene | Confirm `isFullScreen` exists on the target iPadOS 17 in phase 0; if not, treat a window equal to the screen size as full screen | assumed |
| Decided or assumed marks | Values added in this revision are marked | Mark every older value in the plan | open |

## 12. Changelog

Interface changes since the architecture stage, each made on the user's request in the refinement stage.

| Date | Change | Interface touched |
| --- | --- | --- |
| 2026-11-25 | `sys` follows every `resid`, `bars`, `con` and `event` message, at most 5 times a second, and arrives alone every 5 s when none is flowing | Wire protocol rule |
| 2026-11-25 | New keys `poll_min_ms`, `idle_poll_s` and `fluent_ram_s` under `[meters]` | Config keys |
| 2026-11-25 | `DashboardGeometry.init(size:barsHeight:)` became `init(size:mode:barsHeight:displayScale:)`, with a new `header` rectangle and a `headerHeight` constant | Public function signature |
| 2026-11-25 | New `LayoutMode.resolve(size:isFullScreen:orientation:previous:)`, kept in `Layout/DashboardGeometry.swift` | Public function signature |
| 2026-11-25 | New files `Layout/SceneProbe.swift` and `Views/HeaderBand.swift` | File layout |
| 2026-11-25 | The header band needs no wire change: the server still sends the raw case name and the app maps "FFF" to "Unnamed" | none |
| 2026-11-25 | New commands `fmon setup`, `fmon doctor` and `fmon uninstall`, with flags `--dry-run`, `--yes`, `--rotate-token`, `--lhm-zip`, `--config`, `--json` and `--purge` | Command line |
| 2026-11-25 | The server looks for its config in the order `--config`, `FLUENTMON_CONFIG`, `%ProgramData%\FluentMon\config.toml`, then `config.toml` in the working directory | File layout |
| 2026-11-25 | New files `%ProgramData%\FluentMon\setup-state.json`, `%ProgramData%\FluentMon\logs\setup.log` and `installer/install.ps1`; new modules `fluentmon/cli.py` and `fluentmon/setup/` | File layout |
| 2026-11-25 | Scheduled tasks are named `FluentMon` and `FluentMon LHM` and start at log on of the current user instead of a named user; the firewall rule is bound to the `pythonw.exe` of the `uv tool` environment, and the Tailscale range is added only when accepted | Deployment (tasks, firewall rule) |
| 2026-11-25 | No new config keys: `setup` fills the existing `[server]` and `[meters]` keys, and leaves a sensor key out when nothing matches | Config keys (sensor keys optional) |
| 2026-11-25 | `GET /api/health` returns `ok`, `version` and `lhm` only. `session` is not returned: the endpoint is open, and a session id would reveal the case name to anyone on the network. `lhm` is true when the last LibreHardwareMonitor read succeeded | HTTP endpoint |
| 2026-11-25 | `hello` keeps the section 6 shape (`proto`, `server`, `version`, `session`, `thermal`). The B12 keys `ver`, `server_time`, `cfg` and `ref_window` are not sent. The B12 task text still describes the old keys and is revised with the tasks | Wire protocol (decision; no shape change) |
| 2026-11-25 | `PUT /api/config` accepts `pkg_max_w` as well as `target`, `cpus` and `exclude`. It is validated the way `load_config` validates it and is broadcast in the next `sys` message | HTTP endpoint |
| 2026-11-25 | Bonjour TXT record keys are `proto`, `ver` and `name` (section 9). The advertiser reads the host name itself, so its signature is unchanged | Discovery |
| 2026-11-25 | The batch journal is the section 1 text, not section 9. The two paths in it are examples that the launcher replaces with the real run folder and case path | Batch journal reference |
| 2026-11-25 | Installer: `setup` keeps existing `[meters]` keys that detection does not set, instead of resetting them to defaults | Config behaviour |
| 2026-11-25 | Installer: `doctor` reports `Sensors` as `warn` when no sensor key is configured | Doctor check |
| 2026-11-25 | Installer: `q` at the confirm screen stops setup at that step; the steps that already ran keep their manifest entries and are undone with `fmon uninstall`; an empty answer to a sub-prompt keeps the current value | Setup prompts |
| 2026-11-25 | Installer: `doctor` checks the app task's program path in the same way it checks the firewall rule's | Doctor check |
| 2026-11-25 | Installer: new read-only `Backend.task_program(name) -> Path \| None`, not in `MUTATING`, implemented by the fake (from `task_register`) and by `WindowsBackend` (I1, I10); the doctor's `Tasks` check uses it | Public function signature |
| 2026-11-25 | Installer: `Detected` gains `watch_dir` and `runs_dir`; the confirm screen has the nine rows and the footer of section 14, with `q` to cancel | Setup screen |
| 2026-11-25 | Installer: `ctx.dry_run` makes the LHM step skip its read-back, hash check and file check | Dry-run behaviour |
| 2026-11-26 | With no session attached the server sends `hello` only on connect, and no empty snapshot, because `SnapshotMsg.session` is required by the client | Wire protocol (decision; no shape change) |
| 2026-11-26 | `fmon launch` starts Fluent as an interactive GUI, without `-g` and without a stdout pipe; the default `version` argument is `3ddp`. Until the PyFluent source exists, a launched run is read through its `console.trn` in the run folder (the third source of section 1) | Launch behaviour |
| 2026-11-26 | New `SessionManager.transcript_path(session_id) -> Path \| None`, added in B12, so that `con_back` can reread an older range from the transcript | Public function signature |
| 2026-11-25 | Installer: the recon step (pinning the LHM version, URL and hash, and the settings key names) is complete before I9 is built | Build order |
| 2026-11-26 | Installer: `setup` also keeps existing `[server]` and `[convergence]` values on a rerun, except the token and the two folders | Config behaviour |
| 2026-11-26 | Installer: the TDP lookup removes a trailing `<n>-Core Processor` from the CPU name before the exact match, because the registry name carries it | Detection |
| 2026-11-26 | Installer: uninstall restores the LHM settings file only when setup did not install the LHM folder | Uninstall behaviour |
| 2026-11-26 | Installer: `WindowsBackend` returns text from PowerShell as base64 of UTF-8; `windows` is registered as a pytest marker in `pyproject.toml` | Windows backend |
| 2026-11-26 | Installer: supersedes the build-order row above. I9 and I10 are built with the placeholder pins; the recon step pins the real values afterwards, and no real install passes until then | Build order |

## 13. Implementation instructions: iPad app

Thirteen tasks for the lightweight model, in build order. Each one builds and tests on its own against the tasks before it. Every task is written from sections 6, 7 and 9 of this plan; where a task quotes code or a table, that text is the specification.

### Rules for every task

- Do one task at a time, exactly as written, and report it done only when its acceptance test passes.
- Write no comments in any source file.
- Do not change a fixed interface: wire messages, config keys, file layout, public function signatures. Ambiguity, a contradiction with the plan, or a needed design choice: stop and report the question for the refinement stage.
- Target iPadOS 17, Swift 5.9 or later, SwiftUI with the `@Observable` macro, dark appearance only.
- Tests use Swift Testing or XCTest, in `FluentMonTests/<Name>Tests.swift` (assumed; confirm when creating the test target).
- Report any deviation from the task text, however small.

### Task F1: project skeleton

- **Files:** the Xcode project, `FluentMonApp.swift`, `Info.plist`.
- **Do:** create an iPad-only SwiftUI app named FluentMon. Add the Info.plist keys from the project-structure paragraph of section 7: `NSLocalNetworkUsageDescription`, `NSBonjourServices = [_fluentmon._tcp]`, `NSAppTransportSecurity > NSAllowsLocalNetworking = YES`, and all four orientations in `UISupportedInterfaceOrientations~ipad`. Register the URL scheme `fluentmon`. `FluentMonApp` shows a dark `Color(white: 0.06)` full-screen view and sets `isIdleTimerDisabled = false` for now.
- **Edge cases:** the app must rotate to all four orientations on the simulator.
- **Acceptance:** builds with no warnings and launches on an 11-inch and a 13-inch iPad simulator in all four orientations.
- **Out of scope:** any dashboard view, networking or targets other than the app and its test target.

### Task F2: layout

- **Files:** `Layout/DashboardGeometry.swift` (also holds `LayoutMode`, `SceneState` and the `place` extension), `Layout/BarMetrics.swift`.
- **Do:** copy `DashboardGeometry`, `LayoutMode`, `View.place`, `SceneState` and `BarMetrics` from section 7 exactly as printed there, leaving out `DashboardView`, which is task F11. Signatures stay `init(size: CGSize, mode: Mode, barsHeight: CGFloat, displayScale: CGFloat)` and `static func resolve(size: CGSize, isFullScreen: Bool, orientation: UIInterfaceOrientation, previous: DashboardGeometry.Mode) -> DashboardGeometry.Mode`.
- **Edge cases:** orientation `.unknown` falls through to the aspect rule; a window exactly 1.0 wide to high keeps the previous mode; `displayScale` of 1, 2 and 3.
- **Acceptance:** unit tests for every rule in the section 7 test-rules list, over iPad window sizes 834 × 1194, 1194 × 834, 1024 × 1366, 1366 × 1024, 700 × 800 and 1000 × 600, at display scales 1 and 2. All pass. Equality checks use the plan's tolerances; do not loosen them.
- **Out of scope:** drawing anything, the sizes table, and any change to the constants (p = 16, g = 12, h = 32, k, vGapRatio, hGapRatio).

### Task F3: wire messages

- **Files:** `Net/Messages.swift`, `FluentMonTests/Fixtures/*.json`.
- **Do:** `Codable` structs, one per message in section 6: `hello`, `snapshot`, `resid`, `bars`, `sys`, `con`, `event` (server to client) and `sub`, `con_back`, `ping` (client to server). Field names follow the JSON samples in section 6, with `CodingKeys` mapping snake\_case to camelCase. Add `enum Incoming` with one case per server message and `func decodeIncoming(_ data: Data) throws -> Incoming?`, which reads the envelope's `t` first, decodes the matching struct, and returns `nil` for an unknown `t`. Add `func encodeOutgoing(_ m: Outgoing) throws -> Data`. Every number that the server can send as `null` is `Double?`: all values in `snapshot.values` and `resid.rows`, every entry of `sys.clk_mhz`, `bars.step` as a whole, and any field the plan marks nullable. A field the samples do not show is optional.
- **Edge cases:** `resid.rows` entries are `[iteration, [values]]` with mixed types, so give them a custom decoder; the number of values may differ from the column count (decode what is there); extra unknown keys are ignored; `bars.step` is `null` for steady runs; `conv.iters_to_target` may be absent when `state` is `stalled`.
- **Acceptance:** save each JSON sample from section 6 as a fixture. Tests decode every fixture and assert at least one value per message, including a `null` residual becoming `nil`, `step == nil` for a steady run, and an unknown `t` returning `nil` without throwing. Tests also encode `sub`, `con_back` and `ping` and compare to the strings in section 6.
- **Out of scope:** networking, any store, and any change to a message shape.

### Task F4: series buffer

- **Files:** `Model/SeriesBuffer.swift`.
- **Do:** a plain struct that holds full arrays per column and exposes the chart's view of them. `struct SeriesBuffer { mutating func reset(columns: [String], kinds: [String], iters: [Int], values: [[Double?]]); mutating func append(rows: [(iter: Int, values: [Double?])]); func visibleSeries(range: XRange) -> [Series]; func xDomain(range: XRange) -> ClosedRange<Double>; func yDomain(target: Double) -> ClosedRange<Double> }`, with `enum XRange { case all, last500, last100 }`, `struct Series: Identifiable { id: Int; name: String; points: [Point] }` and `struct Point: Identifiable { id: Int; iter: Int; value: Double }`. Only columns whose kind is `res` become series. Points that are `nil`, NaN or not above zero are left out. The y domain is the whole decades from floor(log10 min) to ceil(log10 max) over the visible points, never above 1e1 and never below `target / 10`.
- **Edge cases:** no points at all (return the domain 1e-6...1e1 scaled to the target); one point; a restart where the first appended iteration is lower than the last stored one (treat as a re-initialisation: clear, then append); a row with more or fewer values than columns.
- **Acceptance:** tests for each edge case above, for `last100` returning exactly the last 100 iterations, and for the y-domain rule with min 3e-7 and target 1e-6 (domain starts at 1e-7).
- **Out of scope:** decimation. Plan section 7 says the chart gets a decimated view but gives no algorithm; implement `visibleSeries` without decimation and report this as an open question for the refinement stage.

### Task F5: store and mock client

- **Files:** `Model/MonitorStore.swift`, `Net/MockClient.swift`, `Model/Palette.swift`.
- **Do:** `@Observable @MainActor final class MonitorStore` with: `session`, `series: SeriesBuffer`, `bars`, `sys`, `thermal`, `console` (placeholder type until F10), `connection: Connection` (`enum Connection { case live, stale, offline }`), `sysAt: Date?`, `lastIterationAt: Date?`, `target: Double`, and `func apply(_ msg: Incoming, now: Date)`. `hello` sets `thermal`. `snapshot` resets everything it carries. `resid` appends rows and sets `lastIterationAt = now`. `bars`, `sys` and `con` replace or append their state. `sys` sets `sysAt = now`. `func refreshConnection(now: Date)` sets `.stale` when `sysAt` is more than 7 s old, `.live` otherwise, and `.offline` only when `MonitorClient` says so (a `setOffline(_:)` method). `MockClient` plays the section 6 fixtures into `store.apply` on a timer, so every view can be built without a server; its interface is `final class MockClient { init(store: MonitorStore); func start(); func stop() }`. `Palette` holds the named colours the plan uses (`cool`, `hot`, `fluent`, `residuals`); the plan does not give their values, so use the temporary values `.teal`, `.red`, `.orange` and eight system colours, and report the missing values.
- **Edge cases:** `sys` with a `nil` `clk_mhz` entry; a `snapshot` after a reconnect replaces the series rather than appending; `now` is passed in so tests never read the clock.
- **Acceptance:** tests feed the fixtures from F3 through `apply`: series count and last iteration are right, `connection` flips to `.stale` exactly when `now` is 7 s after `sysAt` and back to `.live` on the next `sys`, and a second `snapshot` leaves no duplicate points.
- **Out of scope:** sockets, views, the console model and any colour decision beyond the temporary values.

### Task F6: arc meters

- **Files:** `Views/ArcMeter.swift`, `Model/MeterBuilders.swift`.
- **Do:** copy `Segment`, `Meter`, `ArcMeter` from section 7 and `Thermal`, `cpuMeter`, `powerMeter`, `ramMeter` from the thermal block, exactly as printed. Add `extension MonitorStore { var meters: [Meter] }` returning the six meters in order (CPU 0, 4, 8, 12 from `cpu_ids`, then power, then RAM), built from the latest `sys`. With no `sys` yet or a stale connection, return the same six meters with value `—`, zero fill and grey colour. Text sizes and strokes follow the three-tier rule in section 7: label 7.5 % of D, major 17 % of D, minor 8.5 % of D, each with the 10 pt floor; outer stroke 8.5 % of D; inner ring 4.5 % of D.
- **Edge cases:** `max == base` in `Thermal` (guard the division and return 0); RAM `total` of 0; wattage above `pkg_max_w` clamps the fill at 1; the fill animation is `.linear(duration: 0.2)` and absent under Reduce Motion, as the plan's code says.
- **Acceptance:** unit tests for `Thermal.fraction` and `color` at the plan's values (base 20, warn 70, max 89: 82.1 °C is the red threshold), for each builder's strings (`97 %`, `4.9 GHz`, `71 W`, `76 °C`, `Fluent 12.0 GB`), and an Xcode preview of all six meters at D = 120 pt that shows no clipped text.
- **Out of scope:** where the meters are placed, and any change to the meter data model.

### Task F7: progress bars

- **Files:** `Views/ProgressBars.swift`.
- **Do:** `struct ProgressBars: View { let bars: Bars?; let metrics: BarMetrics }` draws three rows inside the frame it is given: Iterations, Convergence to 1e-6, Time steps. Row heights, padding and caption visibility come from `BarMetrics`. Fill is `p` from the message. Colours per the bars table in section 7: iterations blue accent, green once converged; convergence teal for `ok`, amber for `stalled`, red for `rising` or `nan`; time steps purple, and grey with the title and the text `Steady run` when `step` is `nil`. Captions are the `label` strings. The convergence bar carries the tick marks from section 7: one 1 pt tick, white at 30 % opacity, at i / of of the track width for i = 1 to ⌈of⌉ − 1, drawn over the fill, taller than the track by 3 pt, dropped in compact mode when two would be closer than 8 pt. Fill animates with `.easeOut(duration: 0.3)`. The view is capped with `.dynamicTypeSize(...DynamicTypeSize.xLarge)`.
- **Edge cases:** `bars == nil` (draw empty grey tracks); `p` outside 0...1 (clamp); `of` of 1 or less (no ticks); a long convergence label (truncate in the middle, one line).
- **Acceptance:** a preview with six cases: steady run, transient run, stalled, rising, nan, and compact mode, with no overlap or clipping; a unit test for the tick positions at `of = 6.0` (five ticks at 1/6 ... 5/6) and `of = 3.4` (three ticks at i / 3.4).
- **Out of scope:** any server maths (all bar values arrive computed) and the bar block's placement.

### Task F8: residual chart

- **Files:** `Views/ResidualChart.swift`.
- **Do:** `struct ResidualChart: View { let store: MonitorStore }`. Implement the `Chart` block from section 7 exactly as printed (log y scale, 2.5 pt dashed target rule at 75 % white, legend `.overlay` in `.topTrailing`, `chartXSelection`). Add the segmented control for `XRange` (All, Last 500, Last 100) above the plot inside the chart frame, and a readout of all series values at `scrubIter` while the user drags. Series colours come from `Palette.residuals`.
- **Edge cases:** no data (show the text `Waiting for a Fluent run` centred, no axes); one series; a target outside the data range (the domain rule from F4 handles it); legend with more than 8 series (show the first 8).
- **Acceptance:** a preview fed from `MockClient` shows the falling curves, the thicker dashed target line and the top-right legend; changing the segmented control changes the x range; dragging shows the readout.
- **Out of scope:** the `Canvas` fallback, decimation, and series toggles.

### Task F9: header band

- **Files:** `Views/HeaderBand.swift`.
- **Do:** `struct HeaderBand: View { let session: Session?; let connection: Connection; let lastIteration: Date? }` fills the 32 pt band it is given. Left to right: case title, `Steady` or `Transient`, a status dot with its word (Running, Converged, Idle, Problem, Disconnected), and on the right the time since the last iteration. `static func caseTitle(_ name: String?) -> String` returns `Unnamed` for `FFF` in any letter case and for a nil or empty name, and the name unchanged otherwise. With no session the title reads `No session`. The age text redraws once a second with `TimelineView(.periodic(from: .now, by: 1))` and uses `monospacedDigit()`. `connection == .offline` shows Disconnected whatever the session status is.
- **Edge cases:** very long case names (truncate at the tail); an age over an hour (show `1 h 05 min`); a `lastIteration` in the future (show `0 s`).
- **Acceptance:** unit tests for `caseTitle` (`FFF`, `fff`, empty, nil, `duct_kw`) and for the age formatter; a preview of all five statuses.
- **Out of scope:** colours beyond the status dot's, and any wire change (the server keeps sending the raw name).

### Task F10: console drawer

- **Files:** `Model/ConsoleBuffer.swift`, `Views/ConsoleDrawer.swift`.
- **Do:** `struct ConsoleBuffer { mutating func reset(first: Int, lines: [String], kinds: String); mutating func append(first: Int, lines: [String], kinds: String); mutating func prepend(first: Int, lines: [String], kinds: String); var lines: [ConsoleLine] }` with `struct ConsoleLine: Identifiable { id: Int; text: String; kind: Character }`, where `id` is the absolute line number. Append ignores lines whose number is already stored; the buffer keeps at most 5,000 lines (drop from the top). `ConsoleDrawer(console: ConsoleBuffer, onSubscribe: (Bool) -> Void, onLoadOlder: (Int) -> Void)` implements the four behaviours of section 8 exactly: three heights (28 pt handle with a ticker of the newest non-residual line, half = 45 % of the left column, full), `ScrollViewReader` + `LazyVStack` rendering in a 12 pt monospaced font, follow mode with the `Live ↓ N new` pill, colours by kind (e red, w amber, s purple, h dim bold, r dim grey, n primary), the `Hide residual rows` toggle on by default, and a filter field. Opening the drawer calls `onSubscribe(true)`, closing it `onSubscribe(false)`. Scrolling to the top calls `onLoadOlder(firstLoadedLine)`.
- **Edge cases:** a `kinds` string shorter than `lines` (treat missing as `n`); duplicate batches after a reconnect; a batch of more than 50 lines (scroll without animation); a filter that matches nothing.
- **Acceptance:** unit tests for the buffer (duplicate ignore, 5,000-line cap, prepend order, short `kinds`); a preview fed at 30 lines per second shows follow mode, the pill after scrolling up, and the colours.
- **Out of scope:** the `UITextView` fallback and sending commands to Fluent.

### Task F11: dashboard integration

- **Files:** `Views/DashboardView.swift`, `Layout/SceneProbe.swift`, `FluentMonApp.swift`.
- **Do:** `DashboardView` exactly as printed in section 7, with the views from F6 to F10 in place of any stand-ins. `SceneProbe` is a `UIViewRepresentable` that reads `isFullScreen` and `interfaceOrientation` from its window scene and writes them to a `Binding<SceneState>` on appearance and on every scene or size change. `FluentMonApp` creates one `MonitorStore`, injects it with `.environment`, and starts `MockClient` for now.
- **Edge cases:** Split View and Stage Manager sizes; rotation while the console drawer is open; `connection == .stale` greys the gauges and dims chart and bars to 50 %, as the Improvements section says.
- **Acceptance:** on the 11-inch and 13-inch simulators, driven by `MockClient`: landscape shows the header band, the chart, three bars and six gauges with the last gauge and the bars ending on one line; rotating to portrait gives the meters row on top; a Stage Manager window dragged across square switches layout only after 0.95 and 1.05 are crossed. Take a screenshot of each of the four cases.
- **Out of scope:** real networking, settings and any change to the layout constants.

### Task F12: network client

- **Files:** `Net/MonitorClient.swift`.
- **Do:** `final class MonitorClient` with `init(store: MonitorStore, endpoint: Endpoint)`, `func start()`, `func stop()`, `func send(_ m: Outgoing)`, where `struct Endpoint { var url: URL; var token: String }`. It opens a `URLSessionWebSocketTask` with the header `Authorization: Bearer <token>`, reads binary frames in a background task, decodes with `decodeIncoming` there, and hops to `@MainActor` only to call `store.apply`. Reconnect backoff is 0.5, 1, 2, 4, 8, then 10 s, reset on a successful snapshot. Close code 4401 stops reconnecting and reports a wrong token. It reconnects when `scenePhase` becomes `.active`. While connected it sets `UIApplication.shared.isIdleTimerDisabled = true`. A `con_back` and `sub` helper wrap `send`.
- **Edge cases:** a decode failure on one frame (skip it, keep the socket); the server closing during a snapshot; two quick `start()` calls (the second does nothing); `stop()` during a backoff wait.
- **Acceptance:** against the Python test client from the server stage, or a local echo server in a test: connects, receives a snapshot and live messages, survives a server restart with the right backoff sequence, and stops with a clear error on a 4401.
- **Out of scope:** discovery, the connect sheet, Keychain and wss certificate handling.

### Task F13: connect, discovery and settings

- **Files:** `Net/Discovery.swift`, `Views/ConnectSheet.swift`, `Views/SettingsSheet.swift`, `Model/Keychain.swift`.
- **Do:** follow section 9. `Discovery` runs an `NWBrowser(for: .bonjour(type: "_fluentmon._tcp", domain: nil), using: .tcp)` and publishes the found servers; resolve one with a short `NWConnection` and read `currentPath?.remoteEndpoint` for host and port, then build `ws://host:port/ws`. `ConnectSheet` lists discovered servers, always offers manual entry (host, port, and a `wss` switch), and takes the token. The token is stored in the Keychain; host and port in `UserDefaults`. Handle the URL `fluentmon://connect?host=...&port=...&token=...` by filling in the sheet. `SettingsSheet` edits `target`, the `exclude` list and the meter CPUs, as named in sections 3 and 7.
- **Edge cases:** no local-network permission (show the system prompt's result as a message); an empty or malformed URL; a token that is rejected (F12 reports 4401, the sheet reopens); a host that is a Tailscale name.
- **Acceptance:** tests for the URL parser and the Keychain wrapper; on a device or simulator: manual entry connects to the Python server; the discovered server appears when the server advertises; the pairing URL fills the sheet.
- **Out of scope:** any server-side setting write. The plan lists no wire message for changing `target` or `exclude` on the server, so the settings sheet changes only the client's own copy until the refinement stage defines one.

### Open questions for the refinement stage

1. Decimation: section 7 says the chart gets a decimated view but defines no algorithm (F4).
2. Palette values for `cool`, `hot`, `fluent` and the eight residual colours (F5).
3. `MockClient` and `ConsoleBuffer` interfaces are minimal inventions of this task list; confirm or replace (F5, F10).
4. `SettingsSheet`: no wire message carries `target`, `exclude` or the meter CPUs from the iPad to the server (F13).
5. Test target location and naming (F2 onward).
6. Whether `isFullScreen` exists on the target iPadOS 17 (already in Improvements, needed by F11).

## 14. Installer and setup

One command on a fresh Windows 10 or 11 PC installs FluentMon and LibreHardwareMonitor (LHM), registers the scheduled tasks and the firewall rule, detects the machine's sensors and folders, and ends with a passing `fmon doctor`. It is built for one PC first, with no machine-specific value in the code: every such value comes from detection and a confirm step. The console script is `fmon`, the name `fmon launch` already uses.

### Decisions

Status follows the project rule: decided values are final, assumed values say how they will be confirmed.

| Item | Status | Value | How an assumed value is confirmed |
| --- | --- | --- | --- |
| Audience | decided | Your PC first; nothing machine-specific hard-coded | none needed |
| Packaging | decided | `installer/install.ps1` installs `uv`, then `uv tool install`s the package, then runs `fmon setup` | none needed |
| Elevation | decided | `fmon setup` relaunches itself through one UAC prompt; the installed server runs unelevated | none needed |
| Applying changes | decided | `setup` changes the machine itself; `--dry-run` prints the same steps without applying them; `--yes` accepts every proposed value | none needed |
| Detected values | decided | Detect, show a table, confirm each value (Enter accepts, a number switches) | none needed |
| Install locations | decided | LHM in `C:\Program Files\FluentMon\LibreHardwareMonitor`; config and manifest in `%ProgramData%\FluentMon`; `watch_dir` and `runs_dir` keep the `C:/FluentMon` defaults from section 5 | none needed |
| Python | assumed | `uv` fetches Python 3.12; no system Python needed | run `install.ps1` on a clean Windows 11 VM; exit of the installer build step |
| LHM download | assumed | A pinned release and SHA-256 in `setup/lhm.py`, fetched from the project's GitHub releases | recon step: check that the release asset name is stable |
| LHM web server | assumed | Enabled on 127.0.0.1:8085 by writing LHM's settings file; the key names are not yet verified | recon step: toggle it in the LHM window and diff the settings file |
| Sensor name patterns | assumed | The AMD and Intel patterns in the detection table below | run `fmon doctor` on a second PC, and keep both machines' `data.json` as fixtures |
| TDP and Tjmax defaults | assumed | A small bundled table keyed by CPU name; an unknown CPU prompts for both values | confirm step on the second PC |
| Firewall program path | assumed | The rule is bound to the `pythonw.exe` of the `uv tool` environment, and that path survives `uv tool upgrade` | `fmon doctor` after an upgrade |
| Autostart scope | assumed | Both tasks start at log on of the current user only, because Fluent runs under that user | reboot test: log on, check `fmon doctor` |

### What `fmon setup` does

Steps run in this order. Each checks the state it owns before it acts, so a second run skips finished steps, and after each step the manifest records what changed. A failure stops at that step; running `setup` again continues from it.

1. **preflight.** Windows 10 or 11, GitHub reachable, ports 8765 and 8085 free (or already held by FluentMon and LHM), then the UAC relaunch.
2. **lhm.** Download the pinned release, verify its SHA-256, unpack it into the install folder. `--lhm-zip PATH` takes an already downloaded zip and checks it against the same hash.
3. **lhm\_settings.** Back up LHM's settings file, then enable its web server on 127.0.0.1:8085.
4. **task\_lhm.** Register `FluentMon LHM`: at log on of the current user, highest privileges, restart on failure. Start it.
5. **detect.** Wait up to 20 s for LHM's `data.json`, apply the detection rules below, show the confirm screen.
6. **config.** Create `watch_dir` and `runs_dir` if missing, then write `%ProgramData%\FluentMon\config.toml` from the confirmed values and a new token from `secrets.token_urlsafe(24)`. An existing token is kept unless `--rotate-token` is given. The file's ACL allows the current user and Administrators only.
7. **firewall.** Create the inbound rule `FluentMon`: TCP 8765, Private profile, `LocalSubnet`, bound to the program.
8. **task\_app.** Register `FluentMon`: at log on of the current user, `pythonw.exe -m fluentmon`, restart on failure every 1 minute (section 5). Start it.
9. **tailscale.** Only if `tailscale.exe` is found and the user accepts: add `100.64.0.0/10` to the rule and print the `tailscale serve` command from section 9. Setup never installs Tailscale.
10. **finish.** Run `fmon doctor`, then print the pairing QR code and the host and port, using the function the server calls at startup (section 9).

### Confirm screen

Every detected value is listed. Enter accepts all, a number picks another candidate or lets you type a value. The screen for the current PC:

```text
CPU            AMD Ryzen 7 7800X3D, 8 cores, 16 threads
1  cpus           [0, 4, 8, 12]
2  power_sensor   AMD Ryzen 7 7800X3D/Powers/Package
3  clock_sensors  Core #1, #3, #5, #7 (Effective)
4  temp_sensor    AMD Ryzen 7 7800X3D/Temperatures/Core (Tctl/Tdie)
5  tdp_w          120
6  pkg_max_w      162
7  tjmax_c        89
8  watch_dir      C:/FluentMon/transcripts
9  runs_dir       C:/FluentMon/runs
Fluent         installs found under ANSYS_INC: 1
[Enter] accept all   [1-9] change a value   [q] quit without changes
```

### Detection rules

More than one match: the first is preselected and the rest are listed as alternatives. These are the `[meters]` keys of section 5; setup adds no config key.

| Value | Rule | If nothing matches |
| --- | --- | --- |
| `power_sensor` | Under the CPU hardware node: `Powers/Package` on AMD, `Powers/CPU Package` on Intel | The `Powers` sensor with the highest reading, else the key is left out |
| `cpus` | Four physical cores at indices 0, n/4, n/2 and 3n/4 of n cores, each mapped to its first logical CPU | All cores when n is below 4 |
| `clock_sensors` | `Core #k (Effective)` for the same four cores, with k counted from 1 | `Core #k`, else the key is left out |
| `temp_sensor` | `Temperatures/Core (Tctl/Tdie)` on AMD, `Temperatures/CPU Package` on Intel | `Core Max`, else the hottest `Temperatures` sensor, else the key is left out |
| `tdp_w`, `tjmax_c` | Bundled table keyed by CPU name | Prompt |
| `pkg_max_w` | round(1.35 × `tdp_w`), the ratio of today's 120 W and 162 W pair | Typed value |

A key that is left out makes the server report that value as `null`, which is how section 6 already treats a missing sensor.

### Files and config location

| Path | Holds | Written by |
| --- | --- | --- |
| `C:\Program Files\FluentMon\LibreHardwareMonitor\` | LHM binaries and its settings file | lhm, lhm\_settings |
| `%ProgramData%\FluentMon\config.toml` | the config of section 5 | config |
| `%ProgramData%\FluentMon\setup-state.json` | the manifest | every step |
| `%ProgramData%\FluentMon\logs\setup.log` | one line per action and result | every step |
| `C:\FluentMon\transcripts`, `C:\FluentMon\runs` | `watch_dir` and `runs_dir` | config |
| `installer\install.ps1` in the repository | the bootstrap script | not installed |

The server looks for its config in this order and uses the first file that exists: `--config PATH`, the environment variable `FLUENTMON_CONFIG`, `%ProgramData%\FluentMon\config.toml`, then `config.toml` in the working directory, which is the development setup of section 5.

### Manifest and undo

`setup-state.json` holds `schema` (1) and a `steps` list. Each entry has the step `id`, `done`, a timestamp `at`, and the fields in the table. `fmon uninstall` shows the done steps, asks once (`--yes` skips the question), then undoes them in reverse order.

| Step | Records | Undo |
| --- | --- | --- |
| lhm | folder, SHA-256, version | stop LHM, delete the folder |
| lhm\_settings | settings path, backup path | restore the backup |
| task\_lhm | task name | stop and unregister |
| config | path, created or updated, backup of the previous file | kept; deleted only with `--purge` |
| firewall | rule name, address ranges | remove the rule |
| task\_app | task name | stop and unregister |

`fmon uninstall` leaves the config, the `C:\FluentMon` folders and the `uv` install alone. `--purge` also deletes the config and `%ProgramData%\FluentMon`. It cannot remove the package it is running from, so it ends by printing `uv tool uninstall fluentmon` for you to run.

### Commands

| Command | Flags | Effect |
| --- | --- | --- |
| `fmon setup` | `--dry-run`, `--yes`, `--rotate-token`, `--lhm-zip PATH`, `--config PATH` | the ten steps above |
| `fmon doctor` | `--json`, `--config PATH` | read-only checks; exit code 0 when no check fails, 1 otherwise |
| `fmon uninstall` | `--yes`, `--purge` | undo through the manifest |

### `fmon doctor`

It changes nothing, prints one line per check (pass, warn or fail with a hint), and is the last step of `setup`.

| Check | Passes when | Level if it fails |
| --- | --- | --- |
| LHM | `lhm_url` returns `data.json` | fail |
| Sensors | every configured sensor path exists in `data.json` with a numeric value; the values are printed; with no sensor configured the check warns and says to run `fmon setup` | fail (warn when none is configured) |
| Server | `GET /api/health` returns `ok` and `lhm: true` | fail |
| Port | a connection to this PC's LAN address on 8765 succeeds | fail |
| Network profile | the active connection is Private; a Public profile leaves the rule inactive | warn |
| Firewall | rule `FluentMon` exists, is enabled, covers the Private profile, and names the current program path | fail |
| Tasks | `FluentMon` and `FluentMon LHM` are registered and their last result is 0; the `FluentMon` task runs the current program path (a different path warns) | fail (warn for a different path) |
| Folders | `watch_dir` and `runs_dir` exist and are writable | fail |
| Config | parses, and the token has at least 32 characters | fail |
| Fluent | an install is found under `ANSYS_INC` or `Program Files\ANSYS Inc` | warn |

### Installer tests

- Every `setup` step talks to Windows through one `Backend` interface: processes, scheduled tasks, firewall, files and downloads. The real backend acts on the PC, a fake records calls, and `--dry-run` uses a backend that only prints. Decided.
- `tests/test_detect.py` runs the detection rules on `tests/fixtures/lhm_amd.json` (this PC) and `lhm_intel.json` (the second PC), and checks the `cpus` mapping for 2, 6, 8 and 16 cores.
- `tests/test_manifest.py` interrupts `setup` after each step, reruns it, and expects the same final state; it also checks that `uninstall` undoes in reverse order.
- By hand, on a clean Windows 11 VM: `install.ps1`, `fmon setup --yes`, a green `fmon doctor`, a reboot, a green `doctor` again, then the iPad pairs by QR code. This is the exit of the installer build step.

### Installer open questions

1. **Wrapper installer.** Deferred: a PyInstaller and Inno Setup `.exe` has no second user to serve yet, and it would need code signing to avoid SmartScreen warnings.
2. **LHM updates.** Deferred: the pinned version changes only with a FluentMon release; add `fmon setup --update-lhm` when the first update is needed.
3. **Missing sensor keys.** Open: check that the config loader and the meter code accept absent `power_sensor`, `clock_sensors` and `temp_sensor` and send `null` (section 6). Resolve in the server stream build step.
4. **Antivirus and the LHM driver.** Open: security software has flagged LHM's kernel driver before. Decide what `setup` and `doctor` print when LHM starts but `data.json` has no CPU sensors.
5. **Tailscale.** Open: whether `setup` should run `tailscale serve` itself instead of printing the command.
6. **Several network adapters.** Open: which LAN address the QR code and the `doctor` port check use when a VPN or a second adapter is present.

## 15. Implementation instructions: server

Fifteen tasks for the lightweight model, in build order. Each one builds and tests on its own against the tasks before it. They are written from sections 1 to 6, 8 and 9; where a task quotes code or a table, that text is the specification. The installer (section 14) and the optional items of the build order (PyFluent adapter, rank pinning, checkpoint buttons) are not covered here.

### Rules for every task

- Do one task at a time, exactly as written, and report it done only when its acceptance test passes.
- Write no comments in any source file.
- Do not change a fixed interface: wire messages, config keys, file layout, public function signatures. Ambiguity, a contradiction with the plan, or a needed design choice: stop and report the question for the refinement stage.
- Python 3.12, type hints on every function signature and attribute, standard `logging` with logger names `fluentmon.<module>`. Print only where a task says so.
- JSON on the wire goes through `orjson` only, so NaN becomes `null` as section 6 requires.
- Tests are `pytest` files in `tests/test_<module>.py`. Async code is tested by calling `asyncio.run(...)` inside ordinary test functions. No test sleeps longer than 0.5 s unless the task says so, and none needs Fluent installed.
- The four recorded fixtures of the recon step may not exist yet. Build every test from the sample lines printed in sections 1, 2 and 6; a test that needs a real recording is skipped with `pytest.mark.skipif` when the file is absent.
- Report any deviation from the task text, however small.

### Assumed values these tasks introduce

The plan does not print these. Each is assumed, with the way it will be confirmed.

| Item | Status | What the tasks assume | How it will be confirmed |
| --- | --- | --- | --- |
| Task order and split | decided | B1 to B15 as listed; each uses only earlier tasks | none needed |
| Libraries | decided | The six in the section 5 tree comment, plus `orjson`, `watchdog` and `qrcode`, which the plan text names | refinement updates the tree comment |
| Test tools | assumed | `pytest` and `hypothesis`; no `pytest-asyncio` | review of B1's `dev` extra |
| Names the plan does not print | assumed | `Config`, `LineAssembler`, `Parser` and its event classes, `ConvTracker`, `StepTracker`, `Changes`, `Coalescer`, `Sampler`, `SessionManager`, `RunSpec`, `build_app` and their methods | each becomes fixed when its task passes review |
| New files | assumed | `api/coalescer.py`, `system/sampler.py`, `transcript/manager.py` and `notify.py`, none of them in the section 5 tree | refinement adds them to the tree and the Changelog |
| Config keys the sample omits | assumed | `host`, `safety_read_s`, `allow_control` and `ntfy_topic`, all under `[server]` | refinement adds them to the sample and the Changelog |
| Residual CSV header | assumed | The first column is named `iter`, followed by the column names of the session | check against what the export is opened with |
| Bar labels | assumed | The long forms of section 3, for example `Iteration 482 / 1000 · 518 left · ETA 0:12:40`; `conv` carries no label | check against how F7 draws captions |
| Console line numbers | assumed | 1-based and absolute from the start of the transcript | check `con.first` handling in F10 |
| Session status words | assumed | `running`, `converged`, `idle`, `problem`, `ended` | check against the header band (F9) |
| Snapshot after structural changes | assumed | A fresh `snapshot` is broadcast on session start, session switch, header change, re-initialisation and the first time step of a run | check F5's replace-on-snapshot rule |
| Decimation | assumed | Shared-index min and max per bucket, as written in B6 | B6 acceptance on a 50,000-iteration series |
| Recording format | assumed | JSON lines `{"at": seconds, "msg": message}` | agree with F5's `MockClient` |
| HTTP authentication | assumed | `/api/health` is open; every other endpoint needs the Bearer token | `fmon doctor` and the settings sheet |
| Idle detection | assumed | Checked on each 5 s idle tick, after max(30 s, 5 × mean gap between iterations) of silence on the server's own clock | the 12-hour soak of section 10 |

### Task B1: skeleton and config

- **Files:** `pyproject.toml`, `fluentmon/__init__.py`, `fluentmon/config.py`, an empty `__init__.py` in `transcript`, `model`, `system` and `api`, `tests/test_config.py`.
- **Do:** package `fluentmon` 0.1.0, `requires-python = ">=3.12"`, the libraries of the assumed-values table, an optional extra `dev` with `pytest` and `hypothesis`, and the console script `fmon = "fluentmon.cli:main"`. `__version__ = "0.1.0"`. In `config.py`, dataclasses `ServerCfg`, `ConvergenceCfg`, `MetersCfg` and `Config`, whose fields are exactly the keys of the section 5 sample, with the sample's values as defaults, plus the four `[server]` keys `host = "0.0.0.0"`, `safety_read_s = 0.0`, `allow_control = false` and `ntfy_topic = ""`. `Config` also holds `source: Path | None`, the file it was read from. `power_sensor` and `temp_sensor` are `str | None`; `clock_sensors` is a list, empty when absent. Functions: `find_config(explicit: Path | None = None) -> Path | None`, trying in order the explicit path, the environment variable `FLUENTMON_CONFIG`, `%ProgramData%\\FluentMon\\config.toml`, then `config.toml` in the working directory, and returning the first that exists; `load_config(path: Path | None = None) -> Config`, which reads with `tomllib` and returns the defaults when no file exists; `save_config(cfg: Config, path: Path) -> None`, which writes with `tomli_w` to a temporary file in the same folder and then `os.replace`s it.
- **Edge cases:** unknown keys are ignored with a logged warning; a wrong type raises `ConfigError(ValueError)` naming the key; `target` must be above 0 and `cpus` must be non-negative integers; a missing `token` loads as an empty string (B14 refuses to start with it); `%ProgramData%` unset (non-Windows) skips that candidate.
- **Acceptance:** the section 5 sample file loads, saves and loads again to an equal `Config`; a file without the three sensor keys loads with `None` and an empty list; the search order is proven with temporary folders and a patched environment; a save leaves no temporary file behind; `pip install -e .[dev]` and `pytest -q` pass.
- **Out of scope:** any runtime code, `cli.py` (B13) and the installer.

### Task B2: line assembler

- **Files:** `fluentmon/transcript/assembler.py`, `tests/test_assembler.py`.
- **Do:** `class LineAssembler` with `feed(self, chunk: bytes) -> list[str]` and `reset(self) -> None`, as the first bullet of section 2 describes. It buffers bytes, splits on `b"\n"`, keeps the trailing partial line for the next call, removes one trailing `\r`, and decodes each complete line as UTF-8 with `errors="replace"`; when the result contains U+FFFD it decodes the same bytes as `cp949` with `errors="replace"` instead.
- **Edge cases:** a multi-byte character split across two chunks (decode only complete lines, never partial ones); an empty chunk; a chunk without a newline returns `[]` and keeps its bytes; empty lines come back as empty strings; a buffer over 1 MiB without a newline is returned as one line, so memory stays bounded.
- **Acceptance:** a `hypothesis` test splits a text with Korean characters and mixed `\n` and `\r\n` endings into random chunks and gets the same lines as feeding it whole; a test feeds the `cp949` bytes of a Korean path and gets the right text; a partial line is returned only after its newline arrives.
- **Out of scope:** files, threads, parsing.

### Task B3: parser

- **Files:** `fluentmon/transcript/parser.py`, `tests/test_parser.py`.
- **Do:** copy `HEADER`, `DATA`, `FLOWTIME`, `MORESTEPS`, `CONVERGED`, `PROBLEM` and `num` exactly as printed in section 2. Add frozen, slotted dataclasses `HeaderEv(names: tuple[str, ...])`, `IterEv(it: int, values: tuple[float, ...], eta_s: int, left: int)`, `RunStartEv(it: int, left: int)`, `StepEv(flow_time: float, step: int, more: int)`, `ConvergedEv(it: int, text: str)`, `ProblemEv(text: str)` and `LineEv(text: str, kind: str)`, and `Event` as their union. `class Parser` has `feed(self, line: str) -> list[Event]` and the attribute `names: tuple[str, ...]`. For every line `feed` returns a `LineEv` first, then the derived events in this order: `HeaderEv`, `RunStartEv`, `IterEv`, `StepEv`, `ConvergedEv`, `ProblemEv`. Rules: `HeaderEv` only when the name list differs from the current one. A data line is accepted only when its value count equals `len(names)`; otherwise log a warning and emit no `IterEv`. `eta_s` converts `h:mm:ss` to seconds. `RunStartEv` comes before the first `IterEv` after the parser is created and whenever `left` is larger than on the previous accepted data line. `StepEv` is emitted by the `MORESTEPS` line, using the step and flow time of the latest `FLOWTIME` line; a `MORESTEPS` line with no earlier `FLOWTIME` is a plain line. Console kinds, as section 6 defines them: `h` for `HEADER`, `r` for `DATA` (accepted or not), `s` for `FLOWTIME` and `MORESTEPS`, `e` for lines containing `Error:`, `Floating point exception` or `Divergence detected`, `w` for other `PROBLEM` matches and for lines starting with `Warning`, `n` for the rest.
- **Edge cases:** NaN tokens (`nan`, `-nan(ind)`, `1.#QNAN`, `inf`) become `math.nan`; a repeated identical header; a value-count mismatch; a trailing `\r`; leading and trailing spaces.
- **Acceptance:** the steady sample lines of section 1 give a `HeaderEv` with seven names and an `IterEv(118, ..., eta_s=72, left=382)`; the transient pair gives `StepEv(0.012, 120, 880)`; a first data line gives `RunStartEv`, a following line with smaller `left` does not, one with larger `left` does; a mismatched data line gives only a `LineEv` of kind `r`; a `hypothesis` test feeds a fixture through `LineAssembler` and `Parser` in random chunk sizes and gets the same events as feeding it whole.
- **Out of scope:** the reducer, residual or report classification (B5), session mode, and any I/O.

### Task B4: progress math

- **Files:** `fluentmon/model/progress.py`, `tests/test_progress.py`.
- **Do:** pure code, no I/O and no clock. `fmt_hms(seconds: float) -> str` returns `h:mm:ss`. `iter_bar(i0: int, i: int, left: int, eta_s: int | None, converged: bool, per_step: bool) -> dict` returns `{"p", "done", "total", "left", "eta_s", "label"}` with `done = i - i0 + 1`, `total = done + left`, `p = done / total`, label `Iteration 482 / 1000 · 518 left · ETA 0:12:40` (the ETA part omitted when `eta_s` is `None`); with `per_step` the label is `Iterations this step 7 / 20`; with `converged`, `p = 1.0` and the label is `Converged at 482`. `class ConvTracker` with `__init__(self, target: float, exclude: Collection[str]) -> None`, `reset(self, ref_window: int) -> None` and `update(self, it: int, residuals: Mapping[str, float]) -> dict`, which implements Bar 2 of section 3 and returns exactly `{"p", "rmax", "limiting", "decades", "of", "iters_to_target", "state"}`: `rmax` is the raw maximum over the non-excluded columns, ignoring NaN; `r_ref` is the running maximum of `rmax` over the first `ref_window` iterations after `reset`, then frozen; the smoothed value is the log-space average with weight 0.2 on the new value, starting at the first value; `decades = log10(r_ref) - log10(smoothed)`, `of = log10(r_ref) - log10(target)`, `p = clamp(decades / of, 0, 1)`; `limiting` is the column with the largest value; the slope is a least-squares fit of `log10(rmax)` against iteration over the last 200 iterations; `iters_to_target` is `ceil((log10(rmax) + 6) / -slope)` generalised to the target, and the key is omitted when the slope is not below -1e-4. `state` is `nan` when any included residual is NaN (then `limiting` is that column), else `rising` when `rmax > r_ref` (then `p = 0`), else `stalled` when the slope is not below -1e-4 and at least 10 points exist, else `ok`. `class StepTracker` with `__init__(self, window: int = 10) -> None`, `reset(self) -> None` and `update(self, now: float, step: int, flow_time: float, more: int) -> dict`, which implements Bar 3 and returns `{"p", "n", "left", "flow_time", "t_end", "eta_s", "label"}`: `n0` is the first step seen, the time step size comes from consecutive flow times, wall time per step is the mean of the last `window` gaps between `update` calls, `t_end = flow_time + more * dt`, `eta_s = wall * more`; the label is `Step 120 · 880 left · t = 0.0120 / 0.1000 s · ETA 2:41:05`, with the `t` and `ETA` parts omitted while unknown.
- **Edge cases:** `rmax` of 0 (use 1e-300); `r_ref` at or below the target (`of` becomes 0 and `p` 1.0); no included columns (`p = 0`, `limiting = None`, state `ok`); fewer than two points for the slope; `left` of 0; a `reset` between transient steps.
- **Acceptance:** `iter_bar(1, 484, 516, ...)` gives `p = 0.484`, `done = 484`, `total = 1000`; a series that starts at 1.0 and then holds 1e-3 for 100 iterations gives `p` within 1e-6 of 0.5, `decades` near 3 and `of` 6; a rising series gives `rising` and `p = 0`; a flat series of 300 iterations gives `stalled` with no `iters_to_target`; a decay of 0.01 decades per iteration gives `iters_to_target` within 2 of the exact value; a NaN names its column; an excluded column is ignored; `StepTracker` reproduces the step 120 example with `now` advancing 5 s per step.
- **Out of scope:** reading the transcript, session status, any message framing, and a `label` key in `conv`.

### Task B5: session state

- **Files:** `fluentmon/model/session.py`, `tests/test_session.py`.
- **Do:** `@dataclass class Changes` with `rows: list[tuple[int, list[float]]]`, `lines: list[tuple[int, str, str]]` (line number, kind, text), `events: list[dict]`, `bars: bool`, `snapshot: bool`, and `merge(self, other: Changes) -> None`. `class SessionState` with `__init__(self, cfg: Config, session_id: str, case: str, started: datetime, clock: Callable[[], datetime] | None = None) -> None` and the methods `apply(self, ev: Event, now: float) -> Changes`, `check_idle(self, now: float) -> Changes`, `end(self, now: float) -> Changes`, `bars_msg(self) -> dict` and `set_options(self, target: float | None = None, exclude: Sequence[str] | None = None) -> None`. Attributes: `session_id`, `case`, `started`, `mode` (`steady` or `transient`), `status` (`running`, `converged`, `idle`, `problem`, `ended`), `columns: list[str]`, `kinds: list[str]` (`res` or `rep`), `iters: array('q')`, `values: list[array('d')]` with one array per column, `console: deque[tuple[int, str, str]]` with `maxlen=20_000`, and `line_count`. `now` is `time.monotonic()` passed in; `clock` supplies wall time for event timestamps. Behaviour:
  1. `LineEv`: append `(line_count + 1, kind, text)` to `console` and to `Changes.lines`.
  2. `HeaderEv` with a different name list: archive the current series if it has points, start an empty one, set `kinds` (residual when the name is `continuity`, `x-velocity`, `y-velocity`, `z-velocity`, `energy`, `k`, `epsilon`, `omega`, `nut`, `intermit`, `retheta`, `v2`, `uds-<n>`, or listed in `cfg.convergence.extra_residual_names`; otherwise report) and set `snapshot`.
  3. `IterEv`: ignored with a warning before the first header. An iteration lower than the last stored one is a re-initialisation: archive, start a new series, reset the trackers, set `snapshot`, emit a `run_start` event. Otherwise append the values, call `ConvTracker.update` with the residual columns, set `status` to `running` (emitting `resumed` when it was `idle`), and add the row and `bars`.
  4. `RunStartEv`: remember `i0`, reset `ConvTracker` with `ref_window` 1 in transient mode and 5 otherwise. Emit the `run_start` event only for the first one of a session, after a re-initialisation, and after an idle resume.
  5. `StepEv`: the first one switches `mode` to `transient` and sets `snapshot`; every one updates `StepTracker` and sets `bars`.
  6. `ConvergedEv`: `status = converged`, the iteration bar goes to 100 % with the label `Converged at <it>`, emit `converged`. `ProblemEv`: `status = problem` until the next `IterEv`, emit `problem`.
  7. `check_idle`: when `status` is `running` or `problem` and `now` minus the last iteration time exceeds max(30 s, 5 × the mean gap of the last 50 iterations), set `idle` and emit `idle`. `end`: `status = ended`, emit `session_end`.
  8. Events are dicts `{"t": "event", "kind": ..., "iter": <last iteration or None>, "text": ..., "ts": <ISO 8601 with offset, seconds>}`. `bars_msg()` returns `{"t": "bars", "iter": ..., "conv": ..., "step": ...}` with `null` for a part that does not exist yet and `step` `null` in steady runs.
- **Edge cases:** iterations before any header; a header repeated with the same names; console numbering after the deque overflows (numbers keep counting); `set_options` takes effect from the next iteration; a report column (`cd-1`) is stored but never enters `ConvTracker`.
- **Acceptance:** tests drive the state with `Parser` output for the section 1 and 2 sample lines and check series lengths, kinds, `bars_msg` for the 484 / 1000 example, the `converged` label, `running` to `idle` to `running` with the `resumed` event (using fake `now` values), a re-initialisation that archives and sets `snapshot`, the first-time-step switch to `transient`, absolute console numbers after 25,000 lines, and that no clock is read except through `clock`.
- **Out of scope:** sockets, coalescing, the snapshot message (B6), timers.

### Task B6: decimation and snapshot

- **Files:** `fluentmon/model/decimate.py`, additions to `fluentmon/model/session.py`, `tests/test_decimate.py`, `tests/test_snapshot.py`.
- **Do:** `decimate(iters: Sequence[int], columns: Sequence[Sequence[float]], max_points: int) -> list[int]` returns the ascending indices to keep, shared by all columns so the snapshot can carry one `iters` array. Up to `max_points` points: all indices. Otherwise split the index range into `max_points // 2` buckets; for every bucket keep the index of the minimum and of the maximum of every column, ignoring NaN; add the first and last index of the whole range; if the union exceeds `max_points`, halve the bucket count and repeat (minimum one bucket). Add to `SessionState`: `snapshot_msg(self, tail: int = 500, max_points: int = 1500) -> dict`, which returns the `snapshot` message of section 6 including `"t": "snapshot"`: the old part (everything but the last `tail` points) decimated to at most `max_points`, then the last `tail` points in full, shared `iters`, `values[column][k]`, `session` as `{"id", "case", "mode", "status", "started"}`, `bars` as the `bars_msg()` content without `t`, and `con_tail` as `{"first", "lines", "kinds"}` for the last 500 console lines (`first` is `line_count + 1` when there are none). Also `console_range(self, before: int, n: int) -> dict | None`, which returns a `con` message holding the lines numbered `max(1, before - n)` to `before - 1`, or `None` when the range starts before the oldest line in the deque.
- **Edge cases:** an all-NaN column; fewer points than `tail`; `max_points` below 4; exactly `max_points` points; a snapshot before any header (empty arrays); only the current series, not archived ones.
- **Acceptance:** 50,000 iterations of 10 columns with one injected spike decimate to at most 1,500 indices that include the spike's index, the first and the last; the snapshot holds at most 2,000 points with strictly increasing `iters`; building it takes under 0.5 s (report the measured time); `console_range` returns the right lines, and `None` below the deque start.
- **Out of scope:** sockets and re-reading the transcript file.

### Task B7: replay tool

- **Files:** `tools/replay.py`, `tests/test_replay.py`.
- **Do:** `python tools/replay.py FIXTURE [--ips N] [--burst] [--out PATH]`. `main(argv: Sequence[str] | None = None) -> int`. Read the fixture as bytes, split with `splitlines(keepends=True)`, and write it to `--out` (default `replay.trn` inside the configured `watch_dir`) through one handle kept open, with a flush after every tick. A tick is every line up to, but not including, the next data line (use the `DATA` pattern of B3 on the ASCII-decoded line); ticks come `1 / N` seconds apart, `--ips` defaulting to 10. `--burst` writes everything at once.
- **Edge cases:** a fixture with no data lines (written in one tick); the output folder missing (create it); Ctrl-C leaves a valid prefix of the fixture; a missing fixture prints a message and returns 2; the file is truncated when the tool starts.
- **Acceptance:** with `--burst` the output is byte-identical to the fixture; with a large `--ips` the concatenated ticks equal the fixture and every tick except the first ends right before a data line; a missing fixture returns 2.
- **Out of scope:** the server, soak testing, editing fixtures.

### Task B8: file tailer

- **Files:** `fluentmon/transcript/tailer.py`, `tests/test_tailer.py`.
- **Do:** `class FileTailer` with `__init__(self, path: Path, on_lines: Callable[[list[str], bool], None], on_live: Callable[[], None], on_restart: Callable[[], None], loop: asyncio.AbstractEventLoop, safety_read_s: float = 0.0) -> None`, `async run(self) -> None` and `stop(self) -> None`, written from the file adapter bullets of section 2: a `watchdog` `Observer` on `path.parent` whose handler only calls `loop.call_soon_threadsafe(drain_requested.set)` for events on `path`; one `drain()` loop that waits on the `asyncio.Event`, clears it, and reads from the saved offset to EOF in 1 MiB chunks through its own `open(path, "rb")` handle; the bytes go through a `LineAssembler`, and `on_lines(lines, live)` is called once per drain with all its lines. The first drain after attaching reads everything already in the file with `live=False`; after it, `on_live()` is called once and later drains use `live=True`. A file smaller than the saved offset is a restart: reset the assembler and offset, call `on_restart()`, read from the start. With `safety_read_s` above 0, one extra drain runs after that much silence while `run()` is active.
- **Edge cases:** the file does not exist yet (a creation event attaches it); another process holds it open for writing; events for other files in the folder are ignored; a burst of 50 notifications causes a few reads, not 50; `on_lines` is never called with an empty list; the file is deleted and recreated.
- **Acceptance:** tests use temporary files and a real `Observer`: ten lines appended in three writes arrive once and in order; 100 existing lines arrive with `live=False`, followed by one `on_live` call and then new lines with `live=True`; truncating and rewriting calls `on_restart` once and delivers the new lines; 50 rapid appends cause fewer reads than appends, counted through a `_read_chunk` hook; every test finishes in under 5 s on Windows.
- **Out of scope:** choosing which file to follow (B11), parsing, process waits.

### Task B9: bus, hub, coalescer and poller

- **Files:** `fluentmon/api/bus.py`, `fluentmon/api/hub.py`, `fluentmon/api/coalescer.py`, `tests/test_hub.py`, `tests/test_coalescer.py`.
- **Do:** `bus.py`: `class Bus` with `publish(self, ev: object) -> None` (a `put_nowait` to every subscriber), `subscribe(self) -> asyncio.Queue[object]` and `unsubscribe(self, q: asyncio.Queue[object]) -> None`; queues are unbounded. `hub.py`: copy `Hub`, `PcPoller` and `client_writer` from section 5 as printed, with only these additions: `Client` is a dataclass holding `ws`, `queue: asyncio.Queue[bytes]` (`maxsize=1000`), `console: bool = True` and `close(self, code: int) -> None`, which schedules `ws.close(code=code)`; `Hub.send` skips a client whose `console` is `False` when `msg["t"] == "con"`; `Hub.__init__` takes `idle_s: float = 5.0` in place of the literal 5.0 and sets `self.on_idle: Callable[[], None] | None = None`, which `idle_poll` calls before `poller.request()`; `Hub.start(self) -> None` calls `rearm_idle()` once; `Hub.send_to(self, c: Client, msg: dict) -> None` encodes with `orjson` and queues for that client only, closing with 4408 on a full queue; `PcPoller.__init__` takes `min_gap_s: float = 0.2` in place of the literal 0.2; `PcPoller.run` catches an exception from `self.sample`, logs it, and sends nothing for that poll. `coalescer.py`: `class Coalescer` with `__init__(self, hub: Hub, window_s: float = 0.1, max_lines: int = 400) -> None`, `push_rows(self, rows: Sequence[tuple[int, list[float]]], bars: dict) -> None` (`rows` may be empty, then only `bars` goes out), `push_lines(self, lines: Sequence[tuple[int, str, str]]) -> None`, `push_event(self, msg: dict) -> None`, `flush(self) -> None`, `drop_pending(self) -> None` and `close(self) -> None`. Leading edge: when no window is open, the first push is sent at once through `hub.broadcast` and opens a window of `window_s` with `loop.call_later`; pushes inside the window accumulate; at the window's end one `resid` message with all rows, one `bars` message with the latest bars, and `con` messages of at most `max_lines` lines with contiguous `first` numbers are sent, and a new window opens only if something was sent. `push_event` flushes everything pending first, then broadcasts the event at once, so order is kept. Message shapes are exactly those of section 6.
- **Edge cases:** a full client queue closes that client with 4408 while the others still receive the message; two requests to `PcPoller` during one running poll cause exactly one rerun; a window that ends with nothing pending opens no new window; `close()` cancels the timers; `con` lines are split across messages without losing or repeating a number.
- **Acceptance:** tests with a fake `ws` that records `send_bytes`: one `broadcast` calls `poller.request` once and a `sys` follows; six requests within 50 ms run the sample at most twice, never concurrently, and at least 0.2 s apart (a fake sample records start times); with `idle_s=0.05` a poll runs after silence and again after the next silence; the `con` filter works; the coalescer sends the first push at once, merges ten pushes inside the window into one `resid` and one `bars` at its end, splits 1,000 lines into three `con` messages of at most 400, and keeps an event after the rows that preceded it; `drop_pending` discards what is waiting.
- **Out of scope:** the WebSocket endpoint (B12), the real sampler (B10), and anything that touches a session.

### Task B10: system metrics

- **Files:** `fluentmon/system/cpu.py`, `fluentmon/system/power.py`, `fluentmon/system/memory.py`, `fluentmon/system/sampler.py`, `tests/test_power.py`, `tests/test_memory.py`, `tests/test_sampler.py`.
- **Do:** `cpu.py`: `prime() -> None` makes the first `psutil.cpu_percent(percpu=True, interval=None)` call, and `cpu_loads(ids: Sequence[int]) -> list[float]` returns the values at those indices, raising `ValueError` for an id the machine does not have. `power.py`: `@dataclass class LhmReading` with `pkg_w: float | None`, `clk_mhz: list[float | None]` and `tctl_c: float | None`; `parse_value(text: str) -> float | None` reads `87.3 W`, `4950.0 MHz`, `4.95 GHz` (returned in MHz), `68.0 °C` and a decimal comma, and returns `None` for `-` or anything else; `find_sensor(tree: dict, path: str) -> float | None` finds, depth first, the first node whose `Text` equals the first path segment and then follows the remaining segments through exact `Text` matches of `Children`, returning the parsed `Value`; `class LhmClient` with `__init__(self, base_url: str, power_sensor: str | None, clock_sensors: Sequence[str], temp_sensor: str | None, timeout_s: float = 1.0, transport: httpx.BaseTransport | None = None) -> None`, `read(self) -> LhmReading` and the attribute `ok: bool`. A clock sensor name resolves as `<hardware>/Clocks/<name>`, where `<hardware>` is the first segment of `power_sensor`, else of `temp_sensor`; with neither, every clock is `None`. `clk_mhz[k]` comes from `clock_sensors[k]`, or `None` when that entry is missing. `memory.py`: `ram_totals() -> tuple[int, int]` returns total and used, where used is total minus available; `class FluentTree` with `__init__(self, root_pid: int | None, refresh_s: float = 1.0, clock: Callable[[], float] = time.monotonic) -> None` and `uss(self) -> int`, which sums `memory_full_info().uss` over the tree and returns the cached sum until `refresh_s` has passed. The tree is `root_pid` and its descendants, or without a root the processes matching `^(fluent|cx\d{3,4}|fl\d{3,4}|fl_mpi\d{3,4})\.exe$` plus `hydra_pmi_proxy.exe` and `mpiexec.exe` descendants of those, resolved once and again only when a stored PID raises `NoSuchProcess`; on `AccessDenied` use `memory_info().private`. `sampler.py`: `class Sampler` with `__init__(self, cfg: Config, lhm: LhmClient, tree: FluentTree) -> None`, `__call__(self) -> dict`, `set_cpus(self, ids: Sequence[int]) -> None`, `set_pkg_max_w(self, w: float) -> None` and the property `lhm_ok`. `__call__` returns the `sys` message of section 6: `{"t": "sys", "cpu_ids", "cpu", "pkg_w", "pkg_max_w", "tdp_w", "clk_mhz", "tctl_c", "ram": {"total", "used", "fluent"}}`, with `fluent` clamped so that 0 ≤ fluent ≤ used ≤ total.
- **Edge cases:** LHM unreachable, slow or returning malformed JSON (every LHM value `None`, no exception, `ok = False`); a sensor missing from the tree; Fluent not running (`fluent = 0`); a process that disappears mid-sum; a `clock_sensors` list shorter than `cpus`.
- **Acceptance:** `find_sensor` returns the right value from `tests/fixtures/lhm_amd.json` when present, else from an inline tree using the names in the section 5 sample; `parse_value("87,3 W")` is 87.3 and `parse_value("4.95 GHz")` is 4950.0; `httpx.MockTransport` answers of 500 and a timeout both give an all-`None` reading; `ram_totals` agrees with `psutil` within 5 %; `FluentTree` over a stub child process started by the test returns a positive size; a `Sampler` result has exactly the keys and types of the section 6 `sys` sample, with one `clk_mhz` entry per CPU id.
- **Out of scope:** `PcPoller` (B9), rank pinning, the `pythonnet` option, any message sending.

### Task B11: session manager

- **Files:** `fluentmon/transcript/manager.py`, `tests/test_manager.py`.
- **Do:** dataclasses `SessionStarted(session_id: str, snapshot: dict)`, `SessionChanges(session_id: str, changes: Changes, bars: dict | None, snapshot: dict | None)` and `SessionEnded(session_id: str)`, all published on the `Bus`. `class SessionManager` with `__init__(self, cfg: Config, bus: Bus) -> None`, `async run(self) -> None`, `current(self) -> SessionState | None`, `sessions(self) -> list[SessionState]` (newest first, at most 20), `check_idle(self) -> None` and `find_fluent_pid() -> int | None` as a module function (the first process named `cx<nnn>.exe`, else `fluent.exe`). Detection is event driven: a `watchdog` `Observer` on `runs_dir` (recursive) and `watch_dir`, whose callbacks only use `call_soon_threadsafe`. A new `session.json` is read for `case`, `launched`, `pid` and `cores`, and its folder's `console.trn` is attached; a new `.trn` or `.log` file in `watch_dir` is attached without a `session.json`. At startup, scan both folders once and attach the newest `session.json` run, or the newest transcript modified within the last 60 s. Attaching builds a `SessionState` (id: the run folder name, or `<file stem>-<YYYYMMDD-HHMM>`; case: the case file name without `.cas.h5`, else the file stem), a `LineAssembler`, a `Parser` and a `FileTailer`. Each `on_lines` batch runs every line through the parser and `SessionState.apply`, merges the `Changes`, and publishes one `SessionChanges` with `bars = state.bars_msg()` and `snapshot = state.snapshot_msg()` only when `changes.snapshot` is set; lines delivered with `live=False` are applied without publishing, and `on_live` publishes one `SessionStarted` with the snapshot. A newer session ends the current one (`state.end`, publish its events and `SessionEnded`) before the new one attaches. The end of a run is `await asyncio.to_thread(psutil.Process(pid).wait)` for the PID in `session.json`, or for `find_fluent_pid()` on a manual transcript; with no PID, a session ends only on a switch or shutdown. `on_restart` ends the session and re-attaches the same file as a new session. `check_idle` calls `state.check_idle(time.monotonic())` and publishes the result.
- **Edge cases:** a half-written `session.json` (invalid JSON is ignored until the next modification); a stale PID; two sessions created within a second; files in `watch_dir` with other extensions; a deleted run folder; `check_idle` with no session.
- **Acceptance:** with temporary `runs_dir` and `watch_dir` and a stub process (`sys.executable -c "import time; time.sleep(2)"`): a run folder with `console.trn` and a `session.json` holding the stub's PID gives `SessionStarted`, then `SessionChanges` with rows for appended data lines, then `SessionEnded` within 2 s of the stub exiting; a second run folder ends the first session; a manual `.trn` in `watch_dir` attaches; a 1,000-line existing file gives exactly one `SessionStarted` and no `SessionChanges`.
- **Out of scope:** starting Fluent (B13), the hub, any WebSocket code.

### Task B12: API app

- **Files:** `fluentmon/api/app.py`, `fluentmon/transcript/manager.py` (only the new `transcript_path` method), `tests/test_app.py`, `tests/test_manager.py` (one added test).
- **Do:** `create_app(cfg: Config, hub: Hub, manager: SessionManager, sampler: Sampler) -> FastAPI`, a plain factory with no lifespan. Auth: `Authorization: Bearer <token>` for HTTP, and for `/ws` the `token` query parameter or the same header. `GET /api/health` is open and returns exactly `{"ok": true, "version": __version__, "lhm": sampler.lhm_ok}`, with no session id. `/ws`: accept the connection first; with a wrong or missing token close with code 4401 and stop. Otherwise build the `hello` message in exactly the section 6 shape and no other keys: `proto` 1, `server` from `socket.gethostname()`, `version` from `__version__`, `session` the value of `manager.current().session_id`, or `null` when no session is attached, and `thermal` with `base_c`, `warn_c` and `max_c` taken from `temp_base_c`, `temp_warn_c` and `tjmax_c` of `cfg.meters`; then, when `snapshot_message(manager)` is not `None`, the snapshot, all built synchronously with no `await` between building them and `hub.clients.add`; send them with `hub.send_to` (a client with no session attached gets `hello` only), add the client and start `client_writer` as a task. Then loop on `receive_text`: `sub` sets `client.console`; `con_back` calls `manager.current().console_range(before, n)` and answers with `hub.send_to`; when that returns `None` (the range is older than the deque) it answers with `reread_console(manager.transcript_path(session_id), before, n)`, and sends nothing when the path or the reread result is `None`, and nothing at all when no session is attached; `ping` and anything else are ignored. On disconnect remove the client and cancel its writer. `snapshot_message(manager) -> dict | None` returns the current session's `snapshot_msg()`, or `None` when no session is attached. `reread_console(path: Path, before: int, n: int) -> dict | None` reads the transcript through `LineAssembler` and `Parser` and returns the `con` message for that range. HTTP, all Bearer: `GET /api/sessions` lists `manager.sessions()` (newest first) as `{id, case, mode, status, started}`, taken from `session_id`, `case`, `mode`, `status` and `started.isoformat(timespec="seconds")`; `GET /api/sessions/{id}/residuals.csv` finds the session among `manager.sessions()` and streams its series as CSV: the header `iter` followed by `columns`, one row per entry of `iters`, and an empty field for NaN; `PUT /api/config` accepts `{target, cpus, exclude, pkg_max_w}`, every key optional, validates as `load_config` does (`pkg_max_w` must be a positive number), updates the matching fields of `cfg` (`cfg.convergence.target`, `cfg.convergence.exclude`, `cfg.meters.cpus`, `cfg.meters.pkg_max_w`), applies them through `manager.current().set_options(target, exclude)` when a session is attached, `sampler.set_cpus` and `sampler.set_pkg_max_w`, saves to `cfg.source` (or the default path when `None`), sends a fresh `hello` with `hub.send` and then, when a session is attached, the snapshot with `hub.broadcast`, and returns `{target, cpus, exclude, pkg_max_w}`. In `manager.py` add `SessionManager.transcript_path(self, session_id: str) -> Path | None`, which returns `source.path` of the attached session with that id, or `None` for an unknown id or a session without a source; change nothing else in that file.
- **Edge cases:** a client that connects while a message is being coalesced (no row lost or duplicated, which is why hello and snapshot are built synchronously); no session attached (`hello` only, `con_back` ignored, `PUT` still updates and saves the config); a `con_back` for lines before the first one; invalid JSON from a client (ignored); an unknown session id (404); a `PUT` with a `cpus` id the machine lacks, or a `pkg_max_w` of 0 or below (422); a `PUT` with no keys (returns the current values, writes nothing); LibreHardwareMonitor unreachable (`health` still answers, with `lhm` false); a closed socket during send.
- **Acceptance:** with `fastapi.testclient.TestClient` and stub manager and sampler: a wrong token gets close code 4401; a right one receives `hello` then `snapshot` (with no session attached, `hello` only), and the `hello` has exactly the keys `t`, `proto`, `server`, `version`, `session` and `thermal`; `sub` with `console` false stops `con` messages; a `con_back` request returns the right lines; a client attached during `Hub.broadcast` calls receives each row exactly once across snapshot and live messages; `health` works without a token, returns exactly `ok`, `version` and `lhm` (false when the stub sampler reports LHM down), and the other endpoints return 401 without one; `GET /api/sessions` lists a stub session; the CSV has the header `iter` plus the columns and the right number of rows, with an empty field for NaN; `transcript_path` returns the attached file for a known id and `None` for an unknown one (in `tests/test_manager.py`); a valid `PUT` changes `target` in the next `bars`, `pkg_max_w` in the next `sys` message, and the file on disk; a `PUT` with `pkg_max_w` 0 gets 422 and changes nothing.
- **Out of scope:** starting the server and the lifespan (B14), discovery and notifications (B15).

### Task B13: launcher, pipe and command line

- **Files:** `fluentmon/launcher.py`, `fluentmon/transcript/pipe.py`, `fluentmon/cli.py`, `tests/test_launcher.py`, `tests/test_pipe.py`, `tests/test_cli.py`.
- **Do:** `launcher.py`: `@dataclass(frozen=True) class RunSpec` with `case: Path`, `cores: int`, `fluent_exe: Path`, `version: str = "3ddp"`, `extra_args: tuple[str, ...] = ()`; `prepare_run(cfg: Config, spec: RunSpec, now: datetime | None = None) -> Path`, which creates `<runs_dir>/<YYYYMMDD-HHMMSS>-<case name>/`, where the case name comes from `case_name` of `fluentmon/transcript/manager.py` (`duct` for `duct.cas.h5`), writes `start.jou` as the journal of section 1, with its two example paths replaced by the forward-slash path of `console.trn` in the run folder and the forward-slash case path, and returns the folder (`start.jou` holds the two lines of the journal and one trailing newline); `launch(cfg: Config, spec: RunSpec) -> int` calls `prepare_run`, starts Fluent with the arguments `version`, `-t<cores>`, `-i`, `start.jou` and then `extra_args`, with no `-g` (the GUI stays interactive once the journal ends, and Fluent writes `console.trn` itself through the journal's `/file/start-transcript`), `I_MPI_PIN_PROCESSOR_LIST` set to the configured `cpus` as a comma list, working directory the run folder, standard output and error discarded, writes `session.json` atomically with exactly `{"case", "launched", "pid", "cores"}` (ISO 8601 launched time), waits for Fluent to exit and returns its exit code. `launch` does not use `PipeSource`. `pipe.py`: `class PipeSource` with `__init__(self, stream: IO[bytes], out: Path) -> None` and `run(self) -> None`, which reads 64 KiB chunks until EOF and appends them to `out` with a flush per chunk; a write error is logged and ends the copy while still draining the pipe. `cli.py`: `main(argv: Sequence[str] | None = None) -> int` with the subcommands `serve` (calls `fluentmon.app.build_app` and uvicorn, B14), `launch CASE --cores N [--fluent-exe PATH] [--config PATH]` and `version`; the installer commands remain reserved for the installer tasks and answer `not implemented` with exit code 2. A missing config file means the defaults.
- **Edge cases:** a case path with spaces or Korean characters; a missing Fluent executable (message, exit 2); a Fluent that exits at once (its exit code is returned, `session.json` is already written); for `PipeSource`, a stream that closes at once; `runs_dir` not existing (create it); the folder name clashing within one second (append `-2`); Fluent exiting non-zero (returned).
- **Acceptance:** a stub executable (a small script file marked executable, with a shebang line for `sys.executable`, that writes its arguments and the value of `I_MPI_PIN_PROCESSOR_LIST` to `args.txt` in its working directory and exits with code 3) is launched: `session.json` has exactly the four keys and a positive PID, `args.txt` holds `-t<cores>`, `-i`, `start.jou` and the pin list but no `-g`, and the exit code 3 is returned; the run folder is named with `duct` for `duct.cas.h5`; `PipeSource` copies three lines from a `BytesIO` into its file byte for byte, and a write error is logged while the stream is still drained; the `start.jou` file equals the section 1 journal with both paths filled in; `main(["version"])` prints the version and returns 0; until I9 replaces them, an installer subcommand returns 2 (I9 replaces that test).
- **Out of scope:** the server, ANSYS discovery (installer), PyFluent.

### Task B14: server assembly

- **Files:** `fluentmon/app.py`, `fluentmon/record.py`, changes to `fluentmon/cli.py` (the `serve` subcommand only), `tests/test_assembly.py`.
- **Do:** `build_app(cfg: Config, record: Path | None = None) -> FastAPI` creates, in this order: `Bus()`; `LhmClient(cfg.meters.lhm_url, cfg.meters.power_sensor, cfg.meters.clock_sensors, cfg.meters.temp_sensor)`; `FluentTree(None, cfg.meters.fluent_ram_s)`, which finds the Fluent processes by name; `Sampler(cfg, lhm, tree)`; `PcPoller(lambda msg: hub.send(msg), sampler, cfg.meters.poll_min_ms / 1000)`, whose send callable forwards to the hub through the closure because the hub needs the poller and the poller needs the hub; `Hub(poller, cfg.meters.idle_poll_s)`; `Coalescer(hub)`; `SessionManager(cfg, bus)`; and the app of B12 through `create_app(cfg, hub, manager, sampler)`. It installs a lifespan that: refuses an empty `token` with `ConfigError`, calls `prime()`, `hub.start()`, starts `manager.run()`, the pump task and, when `record` is set, a `RecorderClient`. The pump subscribes to the bus and, for `SessionStarted`, calls `coalescer.drop_pending()` then `hub.broadcast(snapshot)`; for `SessionChanges`, when `snapshot` is set calls `coalescer.drop_pending()` and `hub.broadcast(snapshot)`, otherwise calls `push_rows(changes.rows, bars)` when `bars` is not `None` and `changes.rows` or `changes.bars` is set, and `push_lines(changes.lines)` when there are lines; then calls `push_event` for each of `changes.events`, for every `SessionChanges`, also when a snapshot was broadcast (it flushes what is pending first, so the order of transcript lines is kept); for `SessionEnded`, flushes. `hub.on_idle` is set to `manager.check_idle`. `record.py`: `class RecorderClient` that takes the place of a socket in `hub.clients` and appends one JSON line `{"at": <seconds since start>, "msg": <message>}` per message to the file. `serve` runs `uvicorn.run` with `host=cfg.server.host`, `port=cfg.server.port`, `ws_ping_interval=15` and `ws_ping_timeout=20`, plus `--record PATH` and `--config PATH`. On shutdown cancel the tasks in reverse order of start and close the coalescer.
- **Edge cases:** no session attached at connect time (the client gets `hello` only and the snapshot arrives with `SessionStarted`); an empty token (refuse with a clear message, exit code 2); the port in use; a manager that raises (logged, restarted after 1 s); shutdown while a window is open.
- **Acceptance:** a test builds the app with temporary folders, enters the lifespan, starts a stub run through a fixture transcript copied in line by line, connects a `TestClient` WebSocket and checks the order `hello`, `snapshot`, then `resid` and `bars`, and that a client connected before the run starts gets `hello` and then the snapshot when the session starts, and a `converged` event after the matching transcript line; the recorded file has valid JSON lines with increasing `at`; the empty-token case raises; cancelling the lifespan leaves no running tasks.
- **Out of scope:** discovery and notifications (B15), installer commands.

### Task B15: discovery, notifications and pairing

- **Files:** `fluentmon/discovery.py`, `fluentmon/notify.py`, `fluentmon/pairing.py`, changes to `fluentmon/app.py` (lifespan only), `tests/test_notify.py`, `tests/test_pairing.py`.
- **Do:** `discovery.py`: `class Advertiser` with `__init__(self, port: int, version: str) -> None`, `async start(self) -> None` and `async stop(self) -> None`, registering `_fluentmon._tcp.local.` through `zeroconf` with `socket.gethostname()` as the service name and the TXT record `proto=1`, `ver=<version>` and `name=<host name>`; a failure is logged and never raised. `notify.py`: `class Notifier` with `__init__(self, topic: str, client: httpx.AsyncClient | None = None, dedupe_s: float = 300.0, clock: Callable[[], float] = time.monotonic) -> None` and `async handle(self, ev: dict) -> None`, which posts to `https://ntfy.sh/<topic>` the title `FluentMon` and a body built from the event kind and text for the kinds `converged`, `problem`, `idle` and `session_end` only, with the `problem` kind not repeated within `dedupe_s`; with an empty topic nothing is sent; a network error is logged and swallowed. `pairing.py`: `pairing_url(host: str, port: int, token: str) -> str` returns `fluentmon://connect?host=<host>&port=<port>&token=<token>` (section 9), and `print_pairing(cfg: Config, out: IO[str] = sys.stdout) -> None` prints the URL and a QR code through `qrcode`'s ASCII output, using the first non-loopback IPv4 address of the machine. In the lifespan, start the `Advertiser`, feed each event dict of `changes.events` (keys `t`, `kind`, `iter`, `text`, `ts`) from the pump to `Notifier.handle` as a separate task, so the pump never waits for the network, and call `print_pairing` once at startup.
- **Edge cases:** no network (no IPv4 address: print the URL with `localhost`); an mDNS name conflict; a flood of `problem` events (one notification per window); `topic` containing characters that need escaping (reject anything outside letters, digits, `_` and `-` with `ConfigError`).
- **Acceptance:** an `httpx.MockTransport` records one POST for `converged`, none for `iter` or `con`, one for two `problem` events 10 s apart (fake clock) and two for events 301 s apart; an empty topic sends nothing; a 500 answer does not raise; `pairing_url` and the QR output are exact for known inputs; `Advertiser.start` with a patched `zeroconf` registers the service type once, with the TXT keys `proto`, `ver` and `name`.
- **Out of scope:** the installer, the Tailscale setup, push to iOS directly.

### Open questions for the refinement stage

The tasks above rely on the assumed values in the table at the start of this section. Each question below needs a decision before the tasks are handed to the implementation stage; until then the assumption in the task text stands.

1. **Pipe ownership.** Decided 2026-11-26: `fmon launch` starts Fluent as an interactive GUI without `-g` and owns no pipe. Fluent writes `console.trn` through the journal's `/file/start-transcript`, and the server tails it. `PipeSource` is built and tested for later batch use, and `launch` does not call it.
2. **Idle timer.** Section 5 calls the 5 s idle fallback the only timer, but idle detection (B5) and the 100 ms coalescing window (B9) also need delays. Confirm that these two are allowed, or find an event-driven replacement.
3. **`run_start` in transient runs.** B5 emits the event once per session, after a re-initialisation and after an idle resume, and not for every time step. Confirm that this is the intended behaviour for transient runs.
4. **Problem notification dedupe.** B15 allows one `problem` notification per 5 minutes (assumed). Confirm the window, or decide whether it should be per session.
5. **Species residuals.** B5 treats `uds-<n>` and the names in `extra_residual_names` as residuals, and everything else as report columns. Confirm against a real species-transport transcript.
6. **No-session `hello` and `snapshot` shape.** Decided 2026-11-26: with no session attached B12 sends `hello` only, and no snapshot, because the iPad client's `SnapshotMsg.session` is not optional. The snapshot follows with `SessionStarted`. Confirm that the client's idle screen handles a `hello` without a snapshot.
7. **Ping reply.** B12 ignores `ping` and relies on the WebSocket-level keep-alive of B14. Decide whether the client needs an explicit reply message, which would be a new wire message.
8. **Fluent host process name.** `find_fluent_pid` (B11) and `FluentTree` (B10) match `cx<nnn>.exe`, `fluent.exe` and related names. Confirm the names on the target machine during the recon step.
9. **Batch journal.** The `start.jou` text of section 1 is copied as printed (B13), with the run folder and case path filled in by the launcher. Confirm that it needs no changes for the installed Fluent version.
10. **History after a restart.** A restarted server rebuilds the series from the transcript only (B11); there are no CSV segments. Decide whether that is acceptable for runs that span a server restart.
11. **Config path for `PUT /api/config`.** B12 writes to `cfg.source`, or the default path when the config came from defaults. Confirm the default path (`%ProgramData%\\FluentMon\\config.toml`) and who may write it.
12. **`.log` files as transcripts.** B11 attaches any `.trn` or `.log` file in `watch_dir`. Confirm that `.log` is wanted, since some log files are not Fluent transcripts.
13. **Assumed files and keys.** The new files (`record.py`, `pairing.py`, `app.py`, `launcher.py`, `tools/replay.py`) and the four new `[server]` keys (`host`, `safety_read_s`, `allow_control`, `ntfy_topic`) are not yet in the Changelog. Once confirmed, record them there and add them to the project tree and the section 5 sample.

## 16. Implementation instructions: installer

These are the tasks for the implementation stage that builds section 14: `fmon setup`, `fmon doctor`, `fmon uninstall` and `installer/install.ps1`. There are ten tasks, I1 to I10, ordered so that each can be built and tested on its own, and every task up to I9 runs on any operating system because it talks to Windows only through the `Backend` of I1. Only I10 and the final manual test touch a real Windows PC. The server tasks B1 to B15 of section 15 come first: these tasks use `Config`, `load_config`, `save_config` and `find_config` (B1), `main` in `cli.py` (B13) and `print_pairing` (B15).

### Rules for every task

- Work on one task at a time, exactly as written, and do not start a task before the acceptance test of the one before it passes.
- Write no comments in any file, test files included.
- Do not change a fixed interface: the config keys of section 5 and the four `[server]` keys of section 15, the wire messages of section 6, the file layout of section 14, the command names and flags of section 14, and the signatures below. If a task needs one changed, stop and report it for the refinement stage.
- Python 3.12 with type hints on every function. Log with `logging`, never `print`, except in `confirm.py` and `cli.py`, which write to the `out` stream they are given.
- Every effect on the machine (processes, scheduled tasks, firewall, files outside a temporary folder, downloads, the registry, the clock) goes through the `Backend` of I1. No other module imports `subprocess`, `winreg`, `urllib` or `httpx` for this purpose, and no test touches the real machine.
- Tests use `pytest` and a `FakeBackend`. Any test that needs a real Windows PC is marked `@pytest.mark.windows` and skipped elsewhere.
- A task is done only when its acceptance test passes. Report any deviation from the task text, and any ambiguity, as a question instead of choosing.

### Assumed values these tasks introduce

| Item | Status | Value | How it is confirmed |
| --- | --- | --- | --- |
| Package layout | assumed | `fluentmon/setup/` holds `backend.py`, `fake.py`, `manifest.py`, `runner.py`, `detect.py`, `tdp.py`, `confirm.py`, `lhm.py`, `pins.py`, `configstep.py`, `windows_steps.py`, `tailscale.py`, `winbackend.py`; `fluentmon/doctor.py` is separate | refinement adds the files to the project tree |
| Module entry point | assumed | `fluentmon/__main__.py` calls `cli.main(["serve", *sys.argv[1:]])`, because section 14 starts the app with `pythonw.exe -m fluentmon` and no earlier task creates it | refinement confirms, then adds it to the tree |
| Health field `lhm` | decided | `GET /api/health` returns `ok`, `version` and `lhm` (section 5 and the Changelog); `session` is not returned | B12 task text is revised with the tasks |
| LHM pin | assumed | `pins.py` holds `LHM_VERSION`, `LHM_URL` and `LHM_SHA256`; the real values are set in the recon step and the tests use a generated zip | recon step |
| LHM settings keys | assumed | `lhm.py` writes the web-server keys through one table, `WEB_SERVER_KEYS`, so a wrong name is a one-line fix | recon step: toggle the server in the LHM window and diff the settings file |
| Manifest entry | assumed | `{"id": str, "done": bool, "at": ISO 8601 with offset, plus the fields of the section 14 table}`; the top level is `{"schema": 1, "steps": [...]}` | refinement |
| Bundled TDP table | assumed | `tdp.py` holds one entry, `AMD Ryzen 7 7800X3D` with `tdp_w` 120 and `tjmax_c` 89; any other CPU prompts | second PC |
| Exit codes | assumed | 0 success, 1 a step or check failed, 2 usage or refusal (not Windows, not elevated and declined, unreadable config) | refinement |
| Log line | assumed | `<ISO time> <step> <action> <result>` in `%ProgramData%\\FluentMon\\logs\\setup.log`, appended | refinement |

### Task I1: backend interface, fake and dry run

- **Files:** `fluentmon/setup/__init__.py`, `fluentmon/setup/backend.py`, `fluentmon/setup/fake.py`, `tests/test_backend.py`.
- **Do:** in `backend.py`, frozen dataclasses `CpuTopology(name: str, physical: int, logical: int)` and `FirewallRule(name: str, port: int, profile: str, remote: str, program: str, enabled: bool = True)`, and `class Backend(Protocol)` with these methods, grouped here only for reading: system: `is_windows() -> bool`, `is_admin() -> bool`, `relaunch_elevated(argv: Sequence[str]) -> int`, `now() -> datetime`, `cpu_topology() -> CpuTopology`, `lan_address() -> str | None`, `network_profile() -> str`, `which(name: str) -> Path | None`, `find_ansys() -> Path | None`, `program_path() -> Path` (the `pythonw.exe` of the running environment); network: `fetch(url: str, dest: Path) -> None`, `http_get_json(url: str, timeout_s: float = 1.0) -> dict | None`, `can_connect(host: str, port: int, timeout_s: float = 1.0) -> bool`, `port_owner(port: int) -> str | None` (the process name holding a listening port, `None` when free); files: `exists(path: Path) -> bool`, `read_bytes(path: Path) -> bytes | None`, `write_bytes(path: Path, data: bytes) -> None` (atomic: temporary file in the same folder, then replace), `append_text(path: Path, text: str) -> None`, `mkdir(path: Path) -> None`, `delete(path: Path) -> None`, `rmtree(path: Path) -> None`, `copy(src: Path, dst: Path) -> None`, `unzip(archive: Path, dest: Path) -> None`, `restrict_acl(path: Path) -> None` (current user and Administrators only), `is_writable(path: Path) -> bool`; processes and tasks: `stop_process(image: str) -> None`, `task_register(name: str, program: Path, args: str, highest: bool, restart_minutes: int | None) -> None`, `task_unregister(name: str) -> None`, `task_exists(name: str) -> bool`, `task_start(name: str) -> None`, `task_stop(name: str) -> None`, `task_last_result(name: str) -> int | None`, `task_program(name: str) -> Path | None` (the program the task runs, `None` when the task does not exist); firewall: `fw_get(name: str) -> FirewallRule | None`, `fw_set(rule: FirewallRule) -> None`, `fw_remove(name: str) -> None`. Also `MUTATING: frozenset[str]`, the names of every method that changes the machine (`relaunch_elevated`, `fetch`, `write_bytes`, `append_text`, `mkdir`, `delete`, `rmtree`, `copy`, `unzip`, `restrict_acl`, `stop_process`, every `task_` method except `task_exists`, `task_last_result` and `task_program`, `fw_set`, `fw_remove`). In `fake.py`, `class FakeBackend` implements every method over in-memory state (a dict of files, a dict of tasks, a dict of rules, settable `topology`, `lan`, `profile`, `ports`, `json_by_url`, `ansys`, `admin`, `windows`), records each call as `(name, args)` in `calls`, and can be told to raise on a named call with `fail_on(name, exc)`. `fetch` writes bytes taken from a settable `downloads: dict[str, bytes]`; `unzip` extracts a real zip held in the fake file store. Also `class DryRunBackend` that wraps any `Backend`, forwards every method not in `MUTATING`, and for every method in `MUTATING` writes one line `would <name> <first argument>` to an `out` stream and returns without acting (`write_bytes` and the like return `None`; `is_admin` is forwarded).
- **Edge cases:** a method called with a path that has spaces or Korean characters; a fake `read_bytes` of a missing file returns `None`, not an error; `fail_on` raises exactly once unless told otherwise.
- **Acceptance:** `FakeBackend` satisfies the protocol (a test lists the protocol's method names and checks each exists on the fake); a file written through the fake is read back byte for byte and `calls` shows both; `fail_on("fetch", OSError())` makes the next `fetch` raise; a `DryRunBackend` over a fake leaves the fake's state unchanged after a call to every `MUTATING` method and prints one line each, while a read method returns the inner fake's value; `MUTATING` holds no name that is not on the protocol.
- **Out of scope:** the real backend (I10), any step logic, and anything that imports `subprocess` or `winreg`.

### Task I2: manifest and step runner

- **Files:** `fluentmon/setup/manifest.py`, `fluentmon/setup/runner.py`, `tests/test_manifest.py`.
- **Do:** `manifest.py`: `class Manifest` with `__init__(self, path: Path, backend: Backend) -> None`, `load(self) -> None` (a missing file gives an empty manifest; an unreadable or wrong-schema file raises `ManifestError(ValueError)`), `get(self, step_id: str) -> dict | None`, `record(self, step_id: str, **fields: object) -> None` (sets `done` true and `at` from `backend.now()`, merges the fields, and saves), `remove(self, step_id: str) -> None`, `entries(self) -> list[dict]` in the order recorded, and the file format of section 14, `{"schema": 1, "steps": [...]}`, written with `orjson` through `backend.write_bytes`. `runner.py`: `class StepError(RuntimeError)`; `@dataclass class Context` with `backend`, `manifest`, `out: IO[str]`, `config_path: Path`, `yes: bool = False`, `rotate_token: bool = False`, `lhm_zip: Path | None = None`, `log_path: Path`, then dry\_run and purge, both bool and False by default, and the method `log(self, step: str, action: str, result: str) -> None` that appends one line in the assumed log format through `backend.append_text`; `class Step(Protocol)` with `id: str`, `undoable: bool`, `done(self, ctx: Context) -> bool`, `run(self, ctx: Context) -> dict`, `undo(self, ctx: Context, entry: dict) -> None`; `run_steps(steps: Sequence[Step], ctx: Context) -> int` and `undo_steps(steps: Sequence[Step], ctx: Context) -> int`. `run_steps` goes through the steps in order; a step whose `done` is true is logged as skipped; otherwise `run` is called and its returned dict is recorded with `manifest.record`; a `StepError` or any other exception is logged and printed as one line naming the step, stops the run, and makes the function return 1 with nothing recorded for that step. `undo_steps` takes the steps that are `undoable` and recorded as done, in reverse order, calls `undo`, and then `manifest.remove`; a failing undo is logged, printed, and the remaining steps still run, with exit code 1 at the end.
- **Edge cases:** a manifest written by an interrupted run (the entry of the failed step is absent, earlier ones are present); a step that returns fields with non-JSON values (a `Path` becomes its string); two `record` calls for one id (the second replaces the first and keeps its position); a `done` that raises (treated as not done and logged).
- **Acceptance:** with fake steps recording their own calls: a second `run_steps` runs no step again; a step failing at position 3 leaves entries for 1 and 2, and a rerun with the failure removed runs 3 onward only; `undo_steps` calls `undo` in reverse order and skips steps that are not undoable; a manifest file survives a reload with equal content; a wrong `schema` raises `ManifestError`; nothing outside the fake file store is touched.
- **Out of scope:** the real steps (I4 to I7), `detect`, the confirm screen, and command line parsing.

### Task I3: detection

- **Files:** `fluentmon/setup/detect.py`, `fluentmon/setup/tdp.py`, `tests/support.py`, `tests/test_detect.py`.
- **Do:** pure code, no `Backend`. `tdp.py`: `lookup(cpu_name: str) -> tuple[float, float] | None` over the bundled table of the assumed-values table, returning `(tdp_w, tjmax_c)`. The name is normalised first: a trailing ` <n>-Core Processor` and surrounding spaces are removed, so `AMD Ryzen 7 7800X3D 8-Core Processor` finds `AMD Ryzen 7 7800X3D`; the match is then exact on the normalised name. `detect.py`: `@dataclass class SensorNode` with `path: str`, `text: str`, `value: float | None`; `walk_sensors(tree: dict) -> list[SensorNode]` returns every leaf of an LHM `data.json` tree that has a `Value`, with `path` built exactly as `find_sensor` of B10 reads it (hardware name first, then the group, then the sensor), and `value` from `parse_value` of B10; `find_cpu_hardware(tree: dict, cpu_name: str) -> str | None` returns the hardware node name equal to `cpu_name`, else the first hardware node that has both a `Powers` and a `Clocks` group; `core_cpus(physical: int, logical: int) -> list[int]` with `ratio = logical // physical`; below 4 physical cores it returns `[k * ratio for k in range(physical)]`, otherwise `[k * ratio for k in (0, n // 4, n // 2, 3 * n // 4)]`; `@dataclass class Detected` with `cpu_name: str`, `cpus: list[int]`, `power_sensor: str | None`, `clock_sensors: list[str]`, `temp_sensor: str | None`, `tdp_w: float | None`, `tjmax_c: float | None`, `pkg_max_w: float | None`, `alternatives: dict[str, list[str]]`; `detect(tree: dict, topology: CpuTopology) -> Detected` applies the detection table of section 14 row by row: AMD when the name contains `AMD`, Intel when it contains `Intel`, any other name uses the fallback column only. `power_sensor` is the full path `<hardware>/Powers/Package` (AMD) or `<hardware>/Powers/CPU Package` (Intel), else the Powers sensor with the highest value, else `None`. `clock_sensors` is the bare names `Core #k (Effective)` for the cores of `cpus`, `k` counted from 1 as the core's index plus 1, and when any of the four is absent the same list as `Core #k`, and when any of those is absent an empty list. `temp_sensor` is the full path of `Temperatures/Core (Tctl/Tdie)` (AMD) or `Temperatures/CPU Package` (Intel), else `Core Max`, else the hottest Temperatures sensor, else `None`. `tdp_w` and `tjmax_c` come from `tdp.lookup`, `pkg_max_w` is `round(1.35 * tdp_w)` and `None` without a TDP. `alternatives[key]` for `power_sensor` and `temp_sensor` lists the other sensors of the same group, as full paths, strongest first; for `clock_sensors` it is empty. `to_config_fields(d: Detected) -> dict` returns only the keys that have a value, named as in section 5 (`cpus`, `power_sensor`, `clock_sensors`, `temp_sensor`, `tdp_w`, `tjmax_c`, `pkg_max_w`), leaving out `None` values and an empty `clock_sensors`. `tests/support.py`: `make_tree(hardware: str, sensors: dict[str, dict[str, str]]) -> dict`, which builds a `data.json` tree from `{group: {sensor: value text}}`.
- **Edge cases:** a tree with no CPU hardware node (all sensor fields empty and `cpus` still computed); a sensor `Value` of `-` or empty; two hardware nodes that both qualify (the first wins); a logical count not divisible by the physical count (use `logical // physical`, at least 1); a CPU name that is neither AMD nor Intel.
- **Acceptance:** `core_cpus` returns `[0, 2]` for 2 cores, `[0, 2, 6, 8]` for 6, `[0, 4, 8, 12]` for 8 and `[0, 8, 16, 24]` for 16, all with SMT (`logical = 2 * physical`), and `[0, 1, 2, 3]` for 4 cores without SMT; an inline AMD tree built with the names of the section 5 sample gives exactly the section 5 sample values (`cpus = [0, 4, 8, 12]`, the four `Core #k (Effective)` names for 1, 3, 5 and 7, the power and temperature paths, `tdp_w` 120, `pkg_max_w` 162, `tjmax_c` 89); an inline Intel tree gives the Intel paths; a tree without `(Effective)` clocks falls back to `Core #k`; a tree without clocks gives an empty list and `to_config_fields` has no `clock_sensors`; an unknown CPU gives `None` for TDP, Tjmax and `pkg_max_w`. When `tests/fixtures/lhm_amd.json` or `lhm_intel.json` exists the same checks run on it, otherwise those two tests are skipped.
- **Out of scope:** reading `data.json` over the network, the confirm screen (I4), and writing the config (I6).

### Task I4: confirm screen and the detect step

- **Files:** `fluentmon/setup/confirm.py`, `fluentmon/setup/pins.py` (only `LHM_DATA_URL` for now), `fluentmon/setup/detect.py` (only the two new `Detected` fields), `tests/test_confirm.py`.
- **Do:** `pins.py`: `LHM_DATA_URL = "http://127.0.0.1:8085/data.json"`. `confirm.py`: `confirm(d: Detected, logical: int, ask: Callable[[str], str], out: IO[str], yes: bool) -> Detected`. `Detected` gains `watch_dir: str` and `runs_dir: str`, defaulting to the `ServerCfg` defaults. `confirm` prints the nine numbered rows of the section 14 screen in its order: `cpus`, `power_sensor`, `clock_sensors`, `temp_sensor`, `tdp_w`, `pkg_max_w`, `tjmax_c`, `watch_dir`, `runs_dir`, each with its detected value, or `not found, key left out` for a missing sensor, or `unknown` for a missing number, followed by the footer line `[Enter] accept all   [1-9] change a value   [q] quit without changes`. An empty answer finishes, and `q` raises `StepError("setup cancelled")`; the steps that already ran keep their manifest entries and are undone with `fmon uninstall`. The `CPU` and `Fluent` lines of the screen are printed by `DetectStep` before it calls `confirm`, because `confirm` has no core count or backend. A row number shows that row's alternatives numbered from 1 plus `0 type a value`; choosing a number replaces the value, `0` asks `New value:` and parses it (`cpus` and `clock_sensors` as comma-separated lists, `tdp_w`, `tjmax_c`, `pkg_max_w` as positive numbers, `cpus` entries as integers from 0 to `logical - 1`, `watch_dir` and `runs_dir` as non-empty text); an empty answer to `New value:` keeps the current value; an invalid answer prints one line and asks again; after a change the screen is printed again. Before the first screen, a `tdp_w` or `tjmax_c` that is `None` is asked for directly (`TDP in watts:` and `Tjmax in degrees C:`), and `pkg_max_w` is then set to `round(1.35 * tdp_w)` when it was `None`. With `yes=True` nothing is asked and the detected values are returned unchanged, except that a `None` `tdp_w` or `tjmax_c` raises `StepError` with the message `tdp_w and tjmax_c are unknown for this CPU; run setup without --yes`. `class DetectStep` with `id = "detect"`, `undoable = False`, `__init__(self, ask: Callable[[str], str], sleep: Callable[[float], None] = time.sleep) -> None`, `done(ctx)` true when the manifest entry `detect` exists and holds `values`, and `run(ctx) -> dict`: poll `backend.http_get_json(LHM_DATA_URL)` up to 20 times with `sleep(1.0)` between tries; no answer raises `StepError("LHM data.json is not reachable on 127.0.0.1:8085")`; a tree where `walk_sensors` finds no sensor under the CPU hardware node raises `StepError("LHM is running but reports no CPU sensors; security software may be blocking its driver")`; otherwise `detect(tree, backend.cpu_topology())`, `confirm`, and return `{"cpu_name": ..., "values": to_config_fields(result)}`.
- **Edge cases:** an answer with spaces; a row number out of range (asked again); `0` for a `clock_sensors` row with an empty list (clears the key); `yes=True` with a complete detection (no prompt and no output beyond the screen); `ask` raising `EOFError` (treated as accepting all).
- **Acceptance:** with scripted answers: Enter alone returns the detected values; `4`, then `1`, then Enter picks the first alternative temperature sensor; `7`, `0`, `95`, Enter sets Tjmax; `q` raises `StepError`; an invalid number prints a message and the next valid answer is used; an unknown CPU asks for TDP and Tjmax first and derives `pkg_max_w`; `yes=True` returns the input unchanged and `yes=True` with an unknown CPU raises; `DetectStep` over a `FakeBackend` serving a tree on the 3rd poll succeeds with two recorded sleeps and returns the `values` dict, and with no answer after 20 polls raises with the message above; the screen text for the section 5 values matches a stored expected string.
- **Out of scope:** writing the config (I6), network calls other than `http_get_json`, and the manifest format.

### Task I5: LHM install and settings

- **Files:** `fluentmon/setup/pins.py` (the remaining constants), `fluentmon/setup/lhm.py`, `tests/test_lhm.py`.
- **Do:** `pins.py` adds `LHM_VERSION: str`, `LHM_URL: str`, `LHM_SHA256: str` (placeholders that the recon step replaces, each marked as such by its name only, not by a comment), `INSTALL_DIR = Path(r"C:\\Program Files\\FluentMon\\LibreHardwareMonitor")`, `LHM_EXE = "LibreHardwareMonitor.exe"` and `LHM_SETTINGS = INSTALL_DIR / "LibreHardwareMonitor.config"`. `lhm.py`: `WEB_SERVER_KEYS: dict[str, str]` with the initial entries `runWebServerMenuItem` to `true` and `listenerPort` to `8085` (unverified until the recon step); `apply_settings(xml: bytes | None, keys: Mapping[str, str]) -> bytes` parses an `<appSettings>` file (creating `<configuration><appSettings/></configuration>` when `xml` is `None`), sets `value` of the `<add key=...>` element for each key and appends the missing ones, and returns the XML as UTF-8 bytes. `class LhmStep` (`id = "lhm"`, `undoable = True`): `done` is true when the manifest entry exists and `INSTALL_DIR / LHM_EXE` exists; `run` takes `ctx.lhm_zip` when given, otherwise downloads `LHM_URL` with `backend.fetch` to `<config folder>/downloads/lhm.zip`; it computes SHA-256 of the bytes with `hashlib`, compares with `LHM_SHA256` and on a mismatch deletes the download (not a user-supplied zip) and raises `StepError` naming the expected and actual hashes; it unzips into `INSTALL_DIR` and raises `StepError` when `LHM_EXE` is not directly inside; it returns `{"folder": str(INSTALL_DIR), "sha256": ..., "version": LHM_VERSION}`. When `ctx.dry_run` is true the step makes the `mkdir`, `fetch` and `unzip` calls but does not read back, hash or look for what the fetch would have written, and returns the same dictionary with `sha256` set to `None`. `undo` calls `stop_process(LHM_EXE)` and `rmtree(folder)`. `class LhmSettingsStep` (`id = "lhm_settings"`, `undoable = True`): `done` is true when the entry exists and the settings file already contains every key of `WEB_SERVER_KEYS` with its value; `run` copies an existing settings file to `LHM_SETTINGS` plus `.fmon-backup`, writes `apply_settings` output through `backend.write_bytes`, and returns `{"settings": str(LHM_SETTINGS), "backup": <backup path or None>}`; `undo` restores the backup over the settings file and deletes the backup, or deletes the settings file when there was no backup.
- **Edge cases:** a corrupt user-supplied zip (`StepError`, nothing unpacked); a download failure (`StepError` with the URL); an existing settings file with other keys (kept); an existing key with another value (replaced); a second run when LHM is running (skipped by `done`); the zip holding its files inside one extra folder (error naming the layout, since the recon step decides the real layout).
- **Acceptance:** with a generated zip containing `LibreHardwareMonitor.exe` and the pinned hash patched in by the test: the files appear in the fake store, the manifest fields are as above, and a rerun skips; a wrong hash raises and removes the downloaded zip but not a `--lhm-zip` file; `apply_settings` on `None` yields a file with both keys, on a file with an unrelated key keeps it, and on a file holding `listenerPort` 8080 changes it to 8085; the settings step creates a backup next to an existing file, and `undo` restores the original bytes; the lhm undo calls `stop_process` before `rmtree`.
- **Out of scope:** starting LHM (I7), the web server's key names beyond the table, and any real download.

### Task I6: config step

- **Files:** `fluentmon/setup/configstep.py`, `tests/test_configstep.py`.
- **Do:** `build_config(values: Mapping[str, object], token: str, existing: Config | None) -> Config` starts from `existing` or `Config()`, copies the keys `watch_dir` and `runs_dir` of `values` into `Config.server` and every other key into `Config.meters`, sets `Config.server.token` to `token`, and returns the result; a `Config.meters` key absent from `values` keeps the value it has in `existing` (its default when there is none), except the three sensor keys, which become `None` or an empty list; a key of `values` that belongs to neither section raises `ValueError`; `Config.server` and `Config.convergence` also keep every value they have in `existing`, except `token`, which is set to the `token` argument, and `watch_dir` and `runs_dir`, which come from `values`. `config_text(cfg: Config) -> str` returns the TOML of the `[server]`, `[convergence]` and `[meters]` tables with `tomli_w`, leaving out `source` and every `None` value. `class ConfigStep` (`id = "config"`, `undoable = True`): `__init__(self, new_token: Callable[[], str] = lambda: secrets.token_urlsafe(24)) -> None`; `done` is true when the manifest entry exists, the file exists and `ctx.rotate_token` is false; `run` reads the confirmed values from `manifest.get("detect")["values"]` (a missing entry raises `StepError("detect has not run")`), reads an existing file through `backend.read_bytes` and `tomllib` and keeps its token unless `ctx.rotate_token` is set or it is shorter than 32 characters, creates `watch_dir` and `runs_dir` with `backend.mkdir`, backs an existing file up to `config.toml.bak-<YYYYMMDD-HHMMSS>` through `backend.copy`, writes the new file through `backend.write_bytes`, calls `backend.restrict_acl`, and returns `{"path": str, "mode": "created" | "updated", "backup": str | None}`. An existing file that cannot be parsed is backed up and replaced, and the log line says so. `undo` does nothing unless `ctx.purge` is true, and then deletes only the config file; the folder `%ProgramData%\\FluentMon` is removed by the uninstall command after every step has been undone (I9).
- **Edge cases:** a token already in the file (kept, same text in the new file); `--rotate-token` (new token, backup made); `values` with no sensors (the three keys absent in the file); an existing file with extra keys (dropped from the new file and visible in the backup); an existing file with a changed `lhm_url` (kept); a changed `port` or `allow_control` in `[server]` and a changed `target` in `[convergence]` (kept); `watch_dir` already present.
- **Acceptance:** `config_text` of the section 5 sample values, written to a temporary file, loads back through `load_config` to an equal `Config` (ignoring `source`); the step run twice keeps the token; `ctx.rotate_token` changes it; a first run records `created`, a later forced run records `updated` with a backup that holds the earlier bytes; the file is passed to `restrict_acl`; both folders are created; `watch_dir` and `runs_dir` are written under `[server]`; a forced rerun over a file with a changed `lhm_url` keeps it; a key of `values` that no section has raises `ValueError`; with `ctx.purge` false `undo` deletes nothing, with it true it does.
- **Out of scope:** detection, the confirm screen, `save_config` of B1 (it writes outside the backend, so this step does not use it).

### Task I7: tasks, firewall and Tailscale

- **Files:** `fluentmon/setup/windows_steps.py`, `fluentmon/setup/tailscale.py`, `tests/test_windows_steps.py`.
- **Do:** `windows_steps.py`: constants `LHM_TASK = "FluentMon LHM"`, `APP_TASK = "FluentMon"`, `RULE_NAME = "FluentMon"`. `class LhmTaskStep` (`id = "task_lhm"`, `undoable = True`): `done` is `backend.task_exists(LHM_TASK)` and backend.task\_last\_result(LHM\_TASK) is not None; `run` calls `task_register(LHM_TASK, INSTALL_DIR / LHM_EXE, "", highest=True, restart_minutes=1)` then `task_start`, and returns `{"task": LHM_TASK}`; `undo` calls `task_stop` then `task_unregister`. `class AppTaskStep` (`id = "task_app"`, `undoable = True`): the same with `APP_TASK`, `backend.program_path()`, the arguments `-m fluentmon`, `highest=False`, `restart_minutes=1`; it also returns `{"task": APP_TASK, "program": str(program)}`. `class FirewallStep` (`id = "firewall"`, `undoable = True`, `__init__(self, port: int = 8765) -> None`): the wanted rule is `FirewallRule(RULE_NAME, port, "Private", "LocalSubnet", str(backend.program_path()))`; `done` is true when `fw_get` returns a rule with the same name, port, profile and program, with enabled true, whose `remote` starts with `LocalSubnet`; `run` calls `fw_set` and returns `{"rule": RULE_NAME, "ranges": ["LocalSubnet"]}`; `undo` calls `fw_remove`. `tailscale.py`: `TAILSCALE_RANGE = "100.64.0.0/10"`, `SERVE_COMMAND = "tailscale serve --bg 8765"`, and `class TailscaleStep` (`id = "tailscale"`, `undoable = False`, `__init__(self, ask: Callable[[str], str]) -> None`): `done` is true when the manifest entry exists; `run` returns `{"skipped": "not installed"}` when `backend.which("tailscale.exe")` is `None`; otherwise it asks `Add the Tailscale range to the firewall rule? [y/N]`, treating `ctx.yes` as `y`, returns `{"skipped": "declined"}` on any other answer, and on `y` reads the rule with `fw_get`, writes it again with `remote = "LocalSubnet,100.64.0.0/10"`, prints `SERVE_COMMAND` to `ctx.out`, and returns `{"range": TAILSCALE_RANGE}`. A missing rule raises `StepError("firewall rule not found")`.
- **Edge cases:** a task that is registered but has never run, for instance because `task_start` failed (`done` is false, so a rerun registers it again and starts it); `task_start` raising (`StepError` naming the task, with the task left registered); the program path changing after an upgrade (the rule differs, so `run` replaces it); Tailscale accepted on an earlier run (the rule keeps the range, because `done` allows a longer `remote`).
- **Acceptance:** with a `FakeBackend`: `run_steps` over the LHM task step registers one task with `highest=True` and starts it; the app task uses `program_path()` and `-m fluentmon`; the firewall rule has the Private profile, `LocalSubnet` and the program path; changing the fake's program path makes `FirewallStep.done` false and a rerun updates the rule; the Tailscale step skips with no `tailscale.exe`, skips on `n`, and with `y` (or `yes`) adds the range and prints exactly `tailscale serve --bg 8765`; every undo reverses its step; all mutating calls appear in `calls` in the expected order.
- **Out of scope:** doctor checks (I8), the real Windows calls (I10), installing Tailscale.

### Task I8: doctor

- **Files:** `fluentmon/doctor.py`, `tests/test_doctor.py`.
- **Do:** `@dataclass(frozen=True) class Check` with `name: str`, `level: str` (`pass`, `warn` or `fail`), `detail: str` and `hint: str | None`. `run_checks(cfg: Config, backend: Backend, config_error: str | None = None) -> list[Check]` returns one `Check` per row of the section 14 doctor table, in table order, named `LHM`, `Sensors`, `Server`, `Port`, `Network profile`, `Firewall`, `Tasks`, `Folders`, `Config` and `Fluent`; it never raises and changes nothing, so a backend call that raises turns that one check into `fail` with the exception text as `detail`. `LHM`: `backend.http_get_json(cfg.meters.lhm_url.rstrip("/") + "/data.json")` returns a dict. `Sensors`: each configured `power_sensor` and `temp_sensor` path, and each `clock_sensors` name resolved as `<hardware>/Clocks/<name>` with `<hardware>` the first path segment of `power_sensor`, else of `temp_sensor`, is found with `find_sensor` of B10 and has a number; the detail lists `name=value` pairs; with no sensor configured the check is `warn` with the detail `none configured` and the hint to run `fmon setup`; with sensors configured it fails when `LHM` failed. `Server`: `http_get_json(f"http://127.0.0.1:{cfg.server.port}/api/health")` has `ok` true; `lhm` true passes, `lhm` false fails, and a missing `lhm` field is a `warn` with the detail `health has no lhm field`. `Port`: `backend.lan_address()` is not `None` and `backend.can_connect(address, cfg.server.port)` is true. `Network profile`: `backend.network_profile() == "Private"`, else `warn` with the hint to switch the connection to Private. `Firewall`: `fw_get("FluentMon")` exists, `enabled`, has `Private` in `profile`, has `cfg.server.port`, and its `program` equals `str(backend.program_path())`. `Tasks`: for `FluentMon` and `FluentMon LHM`, `task_exists` and `task_last_result == 0`; and `backend.task_program("FluentMon")` equals `backend.program_path()`, else `warn` with the hint to run `fmon setup` again after an upgrade. `Folders`: `cfg.server.watch_dir` and `cfg.server.runs_dir` exist and `is_writable`. `Config`: `config_error` is `None` and `len(cfg.server.token) >= 32`. `Fluent`: `backend.find_ansys()` is not `None`, else `warn`. Every `fail` and `warn` has a one-line `hint` saying what to do. `render(checks: Sequence[Check], out: IO[str], as_json: bool) -> None` prints `pass`, `warn` or `fail`, the name, the detail and, for non-pass lines, the hint, one check per line; with `as_json` it prints one JSON array of `{"name", "level", "detail", "hint"}` objects through `orjson`. `exit_code(checks: Sequence[Check]) -> int` is 1 when any check has level `fail`, otherwise 0.
- **Edge cases:** no sensors in the config (`LHM` passes, `Sensors` is `warn` with `none configured`); an app task whose program differs from `program_path()` (`warn`); a sensor that exists with the value `-`; an LHM that answers but whose tree lacks the configured hardware node; `network_profile` returning `DomainAuthenticated` (warn, same hint); a rule that is disabled; a task that was never run (`task_last_result` is `None`, so `fail` with the hint to start it); `lan_address` returning `None`.
- **Acceptance:** with a `FakeBackend` prepared like a finished setup (tree from `tests/support.py`, health JSON with `ok` and `lhm` true, rule, tasks with result 0, folders, `find_ansys` set) and a `Config` of the section 5 sample, every check passes and `exit_code` is 0; removing one condition at a time gives the expected level for that one check and no other (a missing `lhm` field gives `warn` and exit code 0, a disabled rule gives `fail` and exit code 1, a public network gives `warn`, a changed app task program gives `warn`, and a config without sensors gives `warn` for `Sensors`); `render` output for the all-pass case matches a stored expected string, and the JSON form parses to ten objects; a raising backend call yields `fail` for its own check only.
- **Out of scope:** starting or fixing anything (doctor is read-only), the command line (I9), and the `/api/health` handler itself.

### Task I9: commands, preflight, finish and uninstall

- **Files:** `fluentmon/setup/commands.py`, additions to `fluentmon/setup/windows_steps.py`, `fluentmon/__main__.py`, changes to `fluentmon/cli.py` (the installer subcommands only), `tests/test_cli.py` (only the test of the three installer stubs, replaced by a test that each installer subcommand reaches `commands.py`), `tests/test_commands.py`, `tests/test_main.py`.
- **Do:** build this task with the placeholder constants of `pins.py` as they are; the recon step pins the real values afterwards, and until then no real install can pass the hash check. `windows_steps.py` adds `class PreflightStep` (`id = "preflight"`, `undoable = False`, `done` always false): `run` raises `StepError` with a one-line message when `backend.is_windows()` is false, when `backend.port_owner(port)` for 8765 or 8085 names a process that is not `pythonw.exe`, `python.exe` or `LibreHardwareMonitor.exe`, or, unless `ctx.lhm_zip` is set, when `backend.can_connect("github.com", 443)` is false; it returns `{}`. It also adds `class FinishStep` (`id = "finish"`, `undoable = False`, `__init__(self, sleep: Callable[[float], None] = time.sleep) -> None`, `done` always false): `run` loads the config with `load_config(ctx.config_path)`, runs `run_checks` and `render` up to 10 times with `sleep(2.0)` between tries for as long as the `Server` check fails, prints the last result, raises `StepError("fmon doctor reports failures")` when `exit_code` is 1, and otherwise calls `print_pairing(cfg, ctx.out)` of B15 and returns `{}`. `build_steps(ask: Callable[[str], str], port: int = 8765) -> list[Step]` returns the ten steps in the section 14 order: `PreflightStep`, `LhmStep`, `LhmSettingsStep`, `LhmTaskStep`, `DetectStep(ask)`, `ConfigStep()`, `FirewallStep(port)`, `AppTaskStep`, `TailscaleStep(ask)`, `FinishStep()`. `commands.py`: `default_config_path() -> Path` (`%ProgramData%\\FluentMon\\config.toml`, with `C:\\ProgramData` when the variable is unset); `run_setup(argv: Sequence[str], backend: Backend, ask: Callable[[str], str], out: IO[str]) -> int`, `run_doctor(argv, backend, out) -> int` and `run_uninstall(argv, backend, ask, out) -> int`, each parsing only its own flags from section 14 (`setup`: `--dry-run`, `--yes`, `--rotate-token`, `--lhm-zip PATH`, `--config PATH`; `doctor`: `--json`, `--config PATH`; `uninstall`: `--yes`, `--purge`) with `argparse` and returning 2 on a usage error. `run_setup` builds the `Context` (config folder from the config path, manifest `setup-state.json` and log `logs/setup.log` next to it), wraps the backend in `DryRunBackend` for `--dry-run`, and when the backend is not elevated and this is not a dry run it calls `backend.relaunch_elevated(["setup", *argv])` and returns its result without running any step (the preflight checks then run in the elevated process, so the user sees one UAC prompt either way); it passes the port from `load_config` when the config file already exists, and runs `run_steps`. `run_doctor` loads the config (a `ConfigError` becomes `config_error` and the defaults are used for the other checks), runs `run_checks`, `render` and returns `exit_code`. `run_uninstall` loads the manifest, lists the undoable steps that are recorded as done, prints `Nothing to undo.` and returns 0 when there are none, asks `Undo these steps? [y/N]` unless `--yes`, runs `undo_steps` with `purge` set from `--purge`, and with `--purge` finally removes the config folder with `backend.rmtree` and prints `uv tool uninstall fluentmon`. `cli.py`: the subcommands `setup`, `doctor` and `uninstall` now construct `WindowsBackend()` (I10) and call these functions with `input` and `sys.stdout`; they replace the earlier `not implemented` answers of B13 and nothing else in `cli.py` changes. `__main__.py`: `main(["serve", *sys.argv[1:]])` under `if __name__ == "__main__"`, so that `pythonw.exe -m fluentmon` of section 14 starts the server.
- **Edge cases:** `--config` with a folder that does not exist yet (created by the config step); `--lhm-zip` with a missing file (usage error, exit 2); a rerun after success (every step but `preflight` and `finish` skipped, so the only mutating calls are manifest and log writes); a dry run on a clean fake (no state change, one `would` line per mutating call, the detect step prints `would detect sensors` and returns empty values, and a step that would read back what a mutating call produced skips the read-back, as the LHM step of I5 does); an uninstall with no manifest; an uninstall run twice.
- **Acceptance:** with a `FakeBackend` holding a generated LHM zip with the pinned hash patched in, an LHM tree served by `http_get_json`, a health JSON, and scripted `ask` answers: `run_setup(["--yes"], ...)` returns 0, the fake then holds the LHM files, the settings file, both tasks, the rule, the config and the manifest with all ten step ids, and the output ends with the doctor lines and a QR code; a second run returns 0 with no new task, rule or file; `--dry-run` leaves a snapshot of the fake unchanged and prints `would` lines; a non-elevated fake gets one `relaunch_elevated(["setup", "--yes"])` call and no step runs; a failure injected at the firewall step returns 1, leaves entries for the earlier steps, and a rerun finishes; `run_uninstall(["--yes"], ...)` removes the tasks, the rule and the LHM folder that setup installed, and restores the settings file when the LHM folder was not installed by setup (the undo of a folder that setup installed deletes the settings file with it), keeps the config and the `C:/FluentMon` folders, and a second call prints `Nothing to undo.`; `--purge` also deletes the config folder and prints `uv tool uninstall fluentmon`; declining the question changes nothing; `run_doctor` returns 0 on the finished fake and 1 after the rule is removed, and its `--json` output parses; `runpy.run_module("fluentmon", run_name="__main__")` with `cli.main` patched calls it with `["serve"]`.
- **Out of scope:** the real Windows calls (I10), the tests of section 14 that need a real PC, and any change to B13's `launch` and `serve`.

### Task I10: Windows backend and install.ps1

- **Files:** `fluentmon/setup/winbackend.py`, `installer/install.ps1`, `pyproject.toml` (only the `markers` entry that registers `windows`), `tests/test_winbackend.py`.
- **Do:** `class WindowsBackend` implements every method of the `Backend` of I1. Rules for the whole class: every script that returns text writes it as the base64 of its UTF-8 bytes and Python decodes it, so that non-ASCII text such as a Korean program path survives whatever console encoding is active; no value is ever pasted into a PowerShell or command line string; the script text is fixed and its values arrive as environment variables (`FMON_NAME`, `FMON_PROGRAM`, `FMON_ARGS`, `FMON_PORT` and so on) through one helper `_ps(script: str, **env: str) -> str` that runs `powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -Command`, returns standard output, and raises `OSError` with the error text on a non-zero exit. Methods: `is_windows` is `sys.platform == "win32"`; `is_admin` calls `ctypes.windll.shell32.IsUserAnAdmin()`; `relaunch_elevated(argv)` runs `Start-Process` with `-Verb RunAs -Wait -PassThru` on the `fmon.exe` next to `sys.executable` and returns the elevated process's exit code; `cpu_topology` uses `psutil.cpu_count` for both counts and the processor name from the registry value `ProcessorNameString` of `HKLM\\HARDWARE\\DESCRIPTION\\System\\CentralProcessor\\0`; `lan_address` opens a UDP socket to a private address without sending anything and returns the local address, or `None`; `network_profile` returns `NetworkCategory` of the connection profile of the interface that holds `lan_address()`, or `Unknown`; `which` is `shutil.which`; `find_ansys` returns the folder named by `ANSYS_INC`, else `C:\\Program Files\\ANSYS Inc`, when it exists; `program_path` is `Path(sys.executable).with_name("pythonw.exe")`; `fetch` streams the URL over HTTPS only with `httpx` and a 60 s timeout to a temporary name and renames on success; `http_get_json` and `can_connect` use `httpx` and `socket.create_connection` and return `None` and `False` on any error; `port_owner` uses `psutil.net_connections(kind="tcp")` for a listening socket on the port and returns the process name, `"unknown"` when the process cannot be read, `None` when free; `exists`, `read_bytes`, `mkdir`, `delete`, `rmtree`, `copy`, `append_text` and `is_writable` use `pathlib` and `shutil`; `write_bytes` writes a temporary file in the target folder and `os.replace`s it; `unzip` refuses any member whose resolved path leaves `dest` (raising `OSError`); `restrict_acl` runs `icacls` with `/inheritance:r` and full control for the current user and for the SID `*S-1-5-32-544`; `stop_process` runs `taskkill /F /IM` and accepts exit code 128 (not running); the `task_` methods use `Register-ScheduledTask`, `Unregister-ScheduledTask`, `Start-ScheduledTask`, `Stop-ScheduledTask` and `Get-ScheduledTaskInfo` with an at-log-on trigger for the current user, the run level `Highest` or `Limited`, `RestartCount` 999 and `RestartInterval` of the given minutes, no execution time limit, and `task_last_result` returns `None` for the value 267011 (never run); `task_program` returns the `Execute` of the task's first action as a `Path`, or `None` when the task does not exist; the `fw_` methods use `New-NetFirewallRule` (replacing an existing rule of that name), `Get-NetFirewallRule` with its port and application filters, `Set-NetFirewallRule` and `Remove-NetFirewallRule`. `installer/install.ps1` has a `param` block with `-Source` (default `git+https://github.com/scerial/Fluent-Monitor`), `-Ref` (default `main`) and the remaining arguments passed through to `fmon setup`; it sets `$ErrorActionPreference = 'Stop'`, stops with a message unless the Windows build is 10 or newer, installs `uv` with its official installer when `uv` is not found and adds its folder to the session `PATH`, runs `uv tool install --python 3.12 --force` on the source at the ref, runs `uv tool update-shell`, and finally calls `fmon.exe` from `uv tool dir --bin` with `setup` and the passed-through arguments, returning its exit code. The script contains no comments and does not elevate itself.
- **Edge cases:** a path with spaces or Korean characters in every file and task method; a task or rule that already exists (replaced, not duplicated); `taskkill` of a process that is not running; a zip with `..` in a member name; PowerShell writing warnings to standard error with exit code 0 (not an error); no network (`fetch` raises `OSError` naming the URL).
- **Acceptance:** tests that run anywhere: `unzip` rejects a zip-slip archive and extracts a normal one; `write_bytes` leaves no temporary file behind and never exposes a half-written target (checked by an interruption in a patched `os.replace`); `_ps` is exercised through a patched `subprocess.run` and puts every value in the environment, never in the script text (an assertion over a path containing `'`, `"`, `$` and spaces); the `Backend` protocol check of I1 passes for the class. Tests marked `windows`: read-only methods return sane values (`cpu_topology` has a name and positive counts, `program_path` exists, `port_owner(1)` is `None`, `is_writable` on a temporary folder is true), and, when elevated, a task named `FluentMonTest` and a rule named `FluentMonTest` can be created, read back with the values given, replaced, and removed. `install.ps1` parses without errors (`[System.Management.Automation.Language.Parser]::ParseFile`) in a `windows` test. The exit of the installer build step is the manual run of section 14: on a clean Windows 11 VM run `install.ps1`, then `fmon setup --yes`, expect a green `fmon doctor`, reboot, expect a green `fmon doctor` again, then pair the iPad by the QR code.
- **Out of scope:** a wrapper `.exe`, code signing, updating LHM, installing Tailscale, and any change to the steps of I4 to I9.

### Open questions for the refinement stage (installer)

The assumed values at the start of this section stand until these are decided. Questions 1 and 2 change what I8 and I10 build, so settle them first.

1. **Repository access.** `install.ps1` installs from `git+https://github.com/scerial/Fluent-Monitor` (I10). If that repository is private, `uv` needs credentials and the one-command install does not work on a clean PC. Decide: make it public, publish a wheel or release asset, or accept a token argument.
2. **Health field `lhm`.** Decided 2026-11-25: `GET /api/health` returns `ok`, `version` and `lhm` (section 5 and the Changelog). `session` is not returned. The B12 task text is revised with the tasks.
3. **Where the installer commands live.** B13 fixes `main(argv)`, so the installer logic is in `setup/commands.py` and `cli.py` only constructs the real backend and delegates (I9). Confirm, or allow `main` to take injected arguments.
4. **UAC relaunch order.** Section 14 lists the relaunch after preflight; I9 relaunches first and runs the preflight checks in the elevated process. Confirm, since the user sees one prompt either way.
5. **`--yes` on an unknown CPU.** I4 stops with an error when TDP and Tjmax cannot be determined, because the config defaults are the values of this PC and would be wrong silently. Alternative: leave the keys out and warn.
6. **No CPU sensors in `data.json`.** I4 stops with a message naming security software and the LHM driver. Decide whether setup should also offer a retry or print more, which closes the antivirus question of section 14.
7. **Detected values in the manifest.** I4 records the confirmed values in the `detect` entry so that the config step can be rerun after a failure; section 14's manifest table lists no such field. Confirm and add it to the table.
8. **Clock sensor fallback.** I3 treats the four `Core #k` names as all or nothing. Confirm that a partial match should leave the key out.
9. **LHM facts.** The release asset name and layout, the settings file name and format, the web-server key names and whether it can be bound to 127.0.0.1 are unverified (I5). The recon step settles them; until then `WEB_SERVER_KEYS`, `LHM_URL` and `LHM_SHA256` are placeholders.
10. **Task restart policy.** Both tasks restart after 1 minute with a large retry count (I7, I10). Section 5 says every 1 minute for the app and says nothing for LHM. Confirm the LHM value.
11. **uv itself.** `install.ps1` installs `uv` with its official installer and does not pin a version (I10). Decide whether to pin it, and whether `uv tool update-shell` may change the user's `PATH`.
12. **New files and the Changelog.** `setup/commands.py`, `doctor.py`, `__main__.py`, the other `setup/` modules and `installer/install.ps1` are not all in the project tree, and the added `enabled` field of the firewall rule, the `dry_run` and `purge` flags of the step context, and the `-Source` and `-Ref` arguments of the script are not in the Changelog. Once confirmed, record them there.
