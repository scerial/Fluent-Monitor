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
3. The server polls `data.json` with `httpx` and walks the sensor tree to the CPU node > `Powers` > `Package`. Store the path once in `config.toml` (`power_sensor = "AMD Ryzen 7 7800X3D/Powers/Package"`), and parse `"87.3 W"` to a float.
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
  tools/
    replay.py               # writes a recorded transcript out at N x speed
  tests/
    fixtures/steady_kw.trn, transient_les.trn, diverged.trn, reports.trn
    test_parser.py, test_progress.py
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

- Development: `py -3.12 -m venv .venv`, `pip install -e .`, `python -m fluentmon`.
- Always-on: Task Scheduler task "At log on of Youngmin", action `pythonw.exe -m fluentmon` (no console window), restart on failure every 1 minute. LHM gets its own elevated task; FluentMon itself does not need admin.
- Firewall, once, in an elevated PowerShell:

  ```powershell
  New-NetFirewallRule -DisplayName "FluentMon" -Direction Inbound -Protocol TCP `
    -LocalPort 8765 -Action Allow -Profile Private `
    -RemoteAddress LocalSubnet,100.64.0.0/10
  ```

  The second address range is Tailscale's. Keep LHM's port 8085 closed to the network; only FluentMon reads it, locally.

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

1. **Recon (half a day).** Record the fixtures, count change notifications against appended lines, check pipe buffering, confirm the residual-line format and Fluent's process names in Task Manager, and confirm LHM shows a CPU package power sensor. Exit: four fixture files and notes.
2. **Parser and replay (1–2 days).** Exit: pytest green on all fixtures, including the chunking property test.
3. **Server stream (1 day).** PC poll after every broadcast, WebSocket, snapshot, coalescing. Exit: a 20-line Python test client prints sensible `bars` and `sys` messages during a replay.
4. **iPad dashboard (2–3 days).** Layout split, residual chart, bars, arc meters, first on `MockClient`, then live. Exit: a real Fluent run shown end to end.
5. **Console drawer (1 day).** Exit: follow mode, scroll-back and colouring work during a replay at 30 lines/s.
6. **Hardening (1 day).** Bonjour, token and QR pairing, `tailscale serve`, Task Scheduler autostart, ntfy alerts.
7. **Optional.** PyFluent channel, checkpoint buttons, rank pinning, CSV export button.

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
