# satlink

Emulate an asymmetric satellite link between two Docker containers with Linux `tc` (netem + tbf), to test file-transfer applications and protocols. One Python script (`satlink.py`, stdlib only; matplotlib optional for plots). Design and rationale: [plan.md](plan.md).

```
[ground] --veth-- bridge satlink0 --veth-- [terminal]
   downlink (ground -> terminal): shaped on terminal's host-side veth
   uplink   (terminal -> ground): shaped on ground's host-side veth
```

Each direction is `netem` (delay, jitter, loss) feeding a `tbf` FIFO (rate, buffer depth in ms). Nothing is shaped inside the containers, which keeps TCP results honest.

## Requirements

Linux host with root, Docker Engine with the compose plugin, `iproute2`, `ethtool`, `util-linux` (nsenter), Python 3.11+. The kernel needs `sch_netem` and `sch_tbf`. Optional: `matplotlib` for `plot.png`.

## Quick start

```bash
sudo ./satlink.py up                      # builds docker/endpoint, starts containers, disables offloads
sudo ./satlink.py validate                # checks RTT and rate per direction for all presets
sudo ./satlink.py run scenarios/narrowing-link.toml
sudo ./satlink.py down
```

Your own application: `sudo ./satlink.py up --image my-image` (the image needs `sh`, `timeout`, `sha256sum`, `head`; containers run `sleep infinity` and you start your tools through the scenario's `send_cmd` / `recv_cmd`). Addresses: ground `172.30.0.2`, terminal `172.30.0.3`.

## Commands

| Command | Purpose |
|---|---|
| `up [--image IMG]` / `down` | Start/stop containers; `up` rediscovers veths and disables TSO/GSO/GRO |
| `apply <preset or scenario.toml>` | Manual mode: apply the static profile and exit. Timeline events are ignored |
| `outage <seconds> [--dir up\|down\|both]` | Manual blackhole of the profile last applied, then restore |
| `run <scenario.toml>` | Apply, run timeline and transfer, write results |
| `validate [preset ...]` | Ping RTT and iperf3 UDP rate per direction against the profile |
| `clear` | Remove all qdiscs |
| `--dry-run` (before the command) | Print every command instead of running it |

For manual testing: `apply leo`, run your tools with `docker exec`, and use `outage 120 --dir down` whenever you want a link break.

## Scenario file (TOML)

```toml
preset = "leo"                          # presets/*.toml: leo, narrowband, gigabit

[downlink]                              # ground -> terminal. [uplink] has the same keys
rate = "200mbit"                        # tc units: bit, kbit, mbit, gbit
delay = "25ms"                          # one-way
jitter = "4ms"                          # note: jitter also causes reordering
queue_ms = 300                          # modem buffer depth (FIFO)
netem = "loss gemodel 0.5% 40%"         # raw netem options appended as-is (loss, duplicate, corrupt, ...)

[isl]
delay = "25ms"                          # extra one-way delay from ISL hops, applied to both directions

[[event]]
t = 60                                  # seconds after the transfer starts
set = { "downlink.rate" = "20mbit" }    # any [downlink]/[uplink] key, or "isl.delay"

[[event]]
t = 120
outage = { duration = 180, dir = "both" }   # silent blackhole; queued and in-flight packets are lost

[[event]]
t = 300                                 # main-satellite switch: ISL path and downlink change together
switch = { isl_delay = "12ms", downlink = { delay = "22ms", rate = "150mbit" }, gap_ms = 0 }

[app]                                   # optional; without it the timeline just runs against whatever you start
sender = "ground"                       # or "terminal"
file_size = "500M"                      # optional: random file generated on the sender; sha256 verified after
send_cmd = "..."                        # run in the sender container
recv_cmd = "..."                        # run in the receiver container, started first
timeout = 3600
# optional: send_file, recv_file, verify, recv_settle (s), recv_grace (s), [run] duration for app-less runs
```

In `send_cmd`/`recv_cmd`, `{peer}` is the other container's IP, `{self}` its own, `{file}` the sender's file, `{out}` the receiver's file, `{size}` the byte size.

The timeline starts when the sender starts. The run ends when `send_cmd` exits or `timeout` hits (exit 137). If the timeline runs out first, the link keeps its last state. After the run the link is cleared, the receiver is stopped and the file is verified.

### Path switches and reordering

`switch` and `set` use `tc qdisc change`, so packets already queued keep their old delay. A shorter path lets new packets overtake old ones (reordering); a longer path gives a delivery gap. `gap_ms` adds a blackout during which new arrivals are dropped.

## Results

`results/<timestamp>-<scenario>/`

| File | Contents |
|---|---|
| `summary.json` | Exit codes, duration, goodput, sha256 match, executed events |
| `timeseries.csv` | 1 s samples per direction: delivered rate, drops, backlog, configured rate and delay, link mode |
| `plot.png` | Delivered vs configured rate per direction; outages shaded; set/switch events dotted |
| `tc.log` | Every tc command with timestamps |
| `sender.log`, `receiver.log`, `scenario.effective.json` | Tool output and the resolved scenario |

Outage recovery time is the gap between the end of the shaded region and delivered rate coming back in the plot or CSV.

## Behavior to know about

- Offloads (TSO/GSO/GRO) are disabled on both ends of each veth. With them on, netem treats a 64 KB super-packet as one packet and loss is wrong. Rerun `up` after restarting containers.
- netem sits before tbf, so it holds everything in flight for the delay. Its `limit` is sized for a 10 Gbit/s offered load; a sender exceeding that sees drops in netem rather than in the tbf queue.
- Outages delete the root qdisc, which flushes everything, then rebuild it on restore. TCP counters in the containers are not touched.
- netem's per-packet randomness is not seeded. Scenario timing is deterministic; loss patterns are not.
- Handover timing is on the wall clock of the host only because events are relative to transfer start.

## Status

Verified in a sandbox without netem or Docker: command generation and timeline ordering (`--dry-run`), tbf rate accuracy from 64 kbit to 1 Gbit on a veth pair, the iperf3 JSON field used by `validate`, and plot generation.

Not yet run on a real host: `up`/veth discovery, netem delay/jitter/loss, `validate` end to end, the `uftp` baseline (option syntax taken from the Debian usage text), and the 1 Gbit + 600 ms case. Run `validate` first on your host; if the netem + tbf chain does not hold at 1 Gbit, see the fallback in plan.md.

Not in v1 (see "Later" in plan.md): live control, periodic LEO handovers, CAKE, cross traffic, VM support.
