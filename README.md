# satlink

Emulate an asymmetric satellite link between two Docker containers with Linux `tc` (netem + tbf), to test file-transfer applications and protocols. One Python script (`satlink.py`, stdlib only; matplotlib optional for plots). Design and rationale: [plan.md](plan.md).

```
[satellite] --veth-- bridge satlink0 --veth-- [ground]
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

Your own application: `sudo ./satlink.py up --image my-image` (the image needs `sh`, `timeout`, `sha256sum`, `head`; containers run `sleep infinity` and you start your tools through the scenario's `send_cmd` / `recv_cmd`). Addresses: satellite `172.30.0.2`, ground station `172.30.0.3`.

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

[downlink]                              # satellite -> ground. [uplink] has the same keys
rate = "200mbit"                        # tc units: bit, kbit, mbit, gbit
delay = "25ms"                          # one-way
jitter = "4ms"                          # note: jitter also causes reordering
queue_ms = 300                          # modem buffer depth (FIFO)
netem = "loss gemodel 0.5% 40%"         # raw netem options appended as-is (loss, duplicate, corrupt, ...)

[[event]]
t = 60                                  # seconds after the transfer starts
set = { "downlink.rate" = "20mbit" }    # any [downlink]/[uplink] key

[[event]]
t = 120
outage = { duration = 180, dir = "both" }   # silent blackhole; queued and in-flight packets are lost

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

### Delay changes and reordering

`set` uses `tc qdisc change`, so packets already queued keep their old delay. Lowering `delay` lets new packets overtake old ones (reordering); raising it gives a delivery gap.

## CFDP sender and receiver

`apps/cfdp/cfdp_entity.py` (installed in the endpoint image as `cfdp-entity`) sends and receives files with CFDP (CCSDS 727.0-B) over UDP, using [cfdp-py](https://github.com/us-irs/cfdp-py) 0.7.0. Class 1 and Class 2, one file per run.

```
cfdp-entity recv --local-id 2 --remote-id 1 --out-dir /data/recv --once
cfdp-entity send /data/payload.bin payload.bin --local-id 1 --remote-id 2 --remote 172.30.0.3:4444 --class 2 --rate 150mbit
```

The scenarios use satellite = entity 1, ground station = entity 2, UDP port 4444. The destination path is relative to the receiver's `--out-dir`. The receiver replies to whichever address sent to it.

- **Class 1** is unacknowledged: data is sent once. The sender reports success when EOF is sent. The receiver reports `FILE_CHECKSUM_FAILURE` and `DATA_INCOMPLETE` if loss hit the file (the library default would ignore a checksum failure; this tool cancels on it).
- **Class 2** is acknowledged: the receiver NAKs missing data, and the sender succeeds only when the receiver's Finished PDU says the file is complete.
- **`--rate`** paces the sender. CFDP has no congestion control, so set it at or below the link rate. It counts wire bytes (PDU + 42 bytes of Ethernet/IP/UDP), the same way the link's `tbf` does. Measured pacing error is under 1% from 2 to 100 Mbit/s. The Python sender tops out around 200 Mbit/s unpaced.
- **`--segment`** is file bytes per PDU (default 1400, so a PDU fits one unfragmented datagram at MTU 1500).
- **Timers.** The entities cannot see link outages, so their timers must outlast them. Defaults are the library's:

| Flag | Default | Meaning |
|---|---|---|
| `--ack-interval` / `--ack-limit` | 10 s / 2 | Wait for an ACK (of EOF at the sender, of Finished at the receiver) |
| `--nak-interval` / `--nak-limit` | 10 s / 2 | Receiver repeats NAKs for missing data |
| `--check-interval` / `--check-limit` | 5 s / 2 | Class 1 receiver waits for late data after EOF |

With the defaults, a 15 s blackhole that started 1 s into a 4 s transfer abandoned it (`POSITIVE_ACK_LIMIT_REACHED` on the sender, `DATA_INCOMPLETE` on the receiver). The same outage succeeded with `--ack-interval 5 --ack-limit 20 --nak-interval 5 --nak-limit 20`. Scenarios with long outages set longer timers.

Each side prints one JSON report as its last line (`ok`, `condition`, `finished_condition`, `delivery`, duration, goodput, PDU counts by type, `retransmitted_bytes`, faults). `satlink run` stores both lines in `summary.json` as `sender_report` and `receiver_report`. Exit codes: 0 success, 2 fault or incomplete delivery, 3 timeout.

Scenarios: `cfdp-class2-leo`, `cfdp-class1-leo`, `cfdp-narrowband`, `cfdp-long-outage`.

## Results

`results/<timestamp>-<scenario>/`

| File | Contents |
|---|---|
| `summary.json` | Exit codes, duration, goodput, sha256 match, executed events |
| `timeseries.csv` | 1 s samples per direction: delivered rate, drops, backlog, configured rate and delay, link mode |
| `plot.png` | Delivered vs configured rate per direction; outages shaded; set events dotted |
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

The CFDP entity was tested on loopback against a UDP proxy that drops packets and blackholes windows (Class 2 recovers from 2% loss each way, Class 1 reports the loss, a 15 s blackhole fails with default timers and succeeds with long ones), on Python 3.11 and 3.12.

Not yet run on a real host: `up`/veth discovery, netem delay/jitter/loss, `validate` end to end, the endpoint image build (`cfdp-py` install) and the CFDP scenarios over the emulated link, the `uftp` baseline (option syntax taken from the Debian usage text), and the 1 Gbit + 600 ms case. Run `validate` first on your host; if the netem + tbf chain does not hold at 1 Gbit, see the fallback in plan.md.

Not in v1 (see "Later" in plan.md): live control, periodic LEO handovers, CAKE, cross traffic, VM support.
