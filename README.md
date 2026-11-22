# satlink

Emulate an asymmetric satellite link between two Docker containers with Linux `tc` (netem + tbf), to test file-transfer applications and protocols. One Python script (`satlink.py`, stdlib only; matplotlib optional for plots). Design and rationale: [plan.md](plan.md).

```
[satellite] --veth-- bridge satlink0 --veth-- [ground]   (ground = ground station)
   downlink (satellite -> ground): shaped on ground's host-side veth
   uplink   (ground -> satellite): shaped on satellite's host-side veth
```

Each direction is `netem` (delay, jitter, loss) feeding a `tbf` FIFO (rate, buffer depth in ms). Nothing is shaped inside the containers, which keeps TCP results honest.

New to tc and netem? Start with [docs/tc-tutorial.md](docs/tc-tutorial.md).

## Requirements

Linux host with root, Docker Engine with the compose plugin, `iproute2`, `ethtool`, `util-linux` (nsenter), Python 3.11+. The kernel needs `sch_netem` and `sch_tbf`. Optional: `matplotlib` for `plot.png`.

## Quick start

```bash
sudo ./satlink.py up                      # builds docker/endpoint, starts containers, disables offloads
sudo ./satlink.py validate                # checks RTT and rate per direction for all presets
sudo ./satlink.py run scenarios/narrowing-link.toml
sudo ./satlink.py down
```

Your own application: `sudo ./satlink.py up --image my-image` (the image needs `sh`, `timeout`, `sha256sum`, `head`; containers run `sleep infinity` and you start your tools through the scenario's `send_cmd` / `recv_cmd`). Addresses: satellite `172.30.0.2`, ground `172.30.0.3`.

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

[downlink]                              # satellite -> ground station. [uplink] has the same keys
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
sender = "satellite"                    # or "ground"
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

## CFDP sender and receiver

`apps/cfdp/cfdp_entity.py` is a CFDP (CCSDS 727.0-B) file sender and receiver over UDP, built on [cfdp-py](https://github.com/us-irs/cfdp-py) 0.7.0. The endpoint image has it installed as `cfdp`.

```
cfdp send FILE DEST --local-id 1 --remote-id 2 --remote HOST:4444 --class 2 --rate 150mbit
cfdp recv --local-id 2 --remote-id 1 --out-dir /data --once
```

- `--class 1`: unacknowledged, no retransmission; the sender cannot know the outcome. `--class 2`: acknowledged, NAK-based retransmission, success means the receiver's Finished PDU arrived.
- `--rate` paces every outgoing PDU, retransmissions included (`0` = unpaced). CFDP has no congestion control, so size this to the link. Unpaced, the sender overruns the receiver's socket buffer and triggers a NAK storm (measured: 8 MiB took 101 s instead of 2 s).
- The Python sender tops out near 220 Mbit/s of file data (about 20k PDUs/s) on a modern core, so the top of a gigabit downlink is out of reach for it; use `--rate` of about 150mbit or less. `--segment` (default 1400) keeps PDUs under the 1500-byte MTU.
- Timers: `--ack-interval/--ack-limit`, `--nak-interval/--nak-limit`, `--check-interval/--check-limit` (library defaults 10 s x 2). The entities are blind to outages. If the link is dark longer than interval x limit, the transaction is cancelled (`POSITIVE_ACK_LIMIT_REACHED`, `NAK_LIMIT_REACHED`), so outage scenarios set long timers. cfdp-py has no inactivity timer; `--timeout` is a local wall-clock limit.
- The sender's UDP and the receiver's socket buffers are requested at 8 MiB; the kernel caps them at `net.core.rmem_max`/`wmem_max`. Raise those sysctls on the host and in containers for fast links.
- Exit codes: 0 success, 2 CFDP fault or incomplete file, 3 local timeout or terminated early. The last output line is a JSON report (condition, durations, goodput, retransmitted bytes, NAKs, PDU counts, Finished PDU contents). satlink copies it into `summary.json` as `sender_report` / `receiver_report`.
- Ports: both entities use UDP 4444. IDs: satellite 1, ground 2.

Scenarios: `cfdp-class2-leo`, `cfdp-class1-leo` (fails the sha256 check by design under loss), `cfdp-narrowband`, `cfdp-long-outage`.

Local runs and development use uv: `uv venv && uv pip install -e .[plots]` or `uv pip install cfdp-py==0.7.0`, then `.venv/bin/python apps/cfdp/cfdp_entity.py ...`.

## Behavior to know about

- Offloads (TSO/GSO/GRO) are disabled on both ends of each veth. With them on, netem treats a 64 KB super-packet as one packet and loss is wrong. Rerun `up` after restarting containers.
- netem sits before tbf, so it holds everything in flight for the delay. Its `limit` is sized for a 10 Gbit/s offered load; a sender exceeding that sees drops in netem rather than in the tbf queue.
- Outages delete the root qdisc, which flushes everything, then rebuild it on restore. TCP counters in the containers are not touched.
- netem's per-packet randomness is not seeded. Scenario timing is deterministic; loss patterns are not.

## Status

Verified in a sandbox without netem or Docker: command generation and timeline ordering (`--dry-run`), tbf rate accuracy from 64 kbit to 1 Gbit on a veth pair, the iperf3 JSON field used by `validate`, and plot generation.

Not yet run on a real host: `up`/veth discovery, netem delay/jitter/loss, `validate` end to end, the `uftp` baseline (option syntax taken from the Debian usage text), and the 1 Gbit + 600 ms case. Run `validate` first on your host; if the netem + tbf chain does not hold at 1 Gbit, see the fallback in plan.md.

Not in v1 (see "Later" in plan.md): live control, pass/contact schedules, CAKE, cross traffic, VM support.

CFDP entity, verified on loopback through a lossy UDP relay (not through satlink or real netem): Class 2 delivers an identical file under 5% loss in both directions (271 NAKs, 456 KB retransmitted of 8 MiB); Class 1 reports `DATA_INCOMPLETE`; a 10 s outage over EOF cancels the transfer with default-style short timers and completes with long timers; pacing is accurate from 2 to 40 Mbit/s; `SIGTERM` still yields the report. Not yet run: the image build, the CFDP scenarios through satlink, gemodel loss.
