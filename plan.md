# satlink: satellite link emulator for file-transfer testing

## Goal

Test file-transfer applications and protocols (TCP, custom UDP, QUIC, DTN/CFDP) over an emulated satellite link with:

- Asymmetric bandwidth, downlink from a few hundred kbps up to 1 Gbps, uplink with its own profile
- A link that narrows in steps over time
- Random and bursty packet loss
- Abrupt outages that reacquire minutes later (silent blackhole, both ways or one way)
- ISL relay: file originates on a source satellite, crosses inter-satellite links to the main satellite, then downlinks. Path changes alter latency.

Use standard Linux tools (`tc`/netem, `tbf`, `ip`, `ethtool`, iperf3, UFTP, Docker). Glue is a single Python stdlib script plus matplotlib for plots.

v1 is deliberately minimal. Deferred items are listed under [Later](#later).

## Requirements summary

| Area | v1 decision |
|---|---|
| Peak rate | Up to ~1 Gbps on a single host |
| Low end | ~100s of kbps, standard MTU 1500 |
| Host | Native Linux with root, Docker Engine |
| Emulator location | On the Docker host, shaping host-side veth interfaces. No `NET_ADMIN` in app containers |
| Endpoints | One sender, one receiver. Files move in both directions |
| Roles | `ground` (gateway side) and `terminal` (user side). Downlink = ground -> terminal |
| Outages | Silent blackhole, `up`, `down` or `both`. Queued packets are dropped |
| Return path | Same path as forward, independent up/down shaping |
| Queueing | FIFO only, sized in ms |
| Loss/jitter | Raw netem options passed through (e.g. Gilbert-Elliott) |
| Scenarios | TOML timelines with step changes, plus 3 presets |
| Run end | Ends when the transfer exits or times out. Timeline holds its last state if it runs out |
| Modes | Automated (runs the apps) and manual (only applies the link) |
| ISL | Delay-only segment, same delay in both directions, changed by `set` or `switch` events |

## Architecture

```
 [ground container] --veth-- docker bridge --veth-- [terminal container]
          ^                                                ^
  host-side veth egress = UPLINK chain        host-side veth egress = DOWNLINK chain
```

### Shaping placement

Each direction is shaped on the host side of the destination container's veth. Nothing is shaped inside the containers.

Reason: shaping on the sender's own interface lets TCP Small Queues and local qdisc backpressure throttle the sender without loss, which gives misleading results. The bottleneck must be a non-local hop.

### Per-direction qdisc chain

```
root:  netem  delay <base + isl_delay> [raw netem options: jitter, loss, ...]
child: tbf    rate <rate> latency <queue ms>   (FIFO bottleneck)
```

- `tbf` is the bottleneck and its `latency` sets the buffer depth in ms.
- netem orders packets by send time, so a drop in delay lets new packets overtake old ones. This produces realistic reordering on a path switch with no custom code.
- netem's built-in `rate` is not used: it serializes packets behind the queue tail (no reordering) and sizes buffers in packets.

### Offloads

Disable TSO, GSO and GRO on both ends of each veth (`ethtool -K`, using `nsenter` for the container side). Otherwise netem treats a 64KB GSO super-packet as one packet, and a single drop removes dozens of real packets.

### Interface discovery

For each container, read `/sys/class/net/eth0/iflink` inside its namespace (`nsenter -t <pid> -n`) and match it to the host interface index. Rediscover on every command, since container restarts change veth names.

### Link operations

| Operation | Implementation |
|---|---|
| Parameter change (rate, delay, loss, ISL delay) | `tc qdisc change` on the affected stage. Queued packets are kept |
| Outage start | `tc qdisc replace` root with `netem loss 100%`. Flushes queued and in-flight packets |
| One-way outage | Same, on only one direction's veth |
| Outage end | Rebuild the chain from current state |

## ISL relay scenario

```
source sat --[ISL path: isl_delay]--> main sat --[downlink: rate/delay/loss/queue]--> ground
 ("ground" container)                                                      ("terminal" container)
```

- The `ground` container plays the source satellite and `terminal` plays the receiving ground station. Downlink stays ground -> terminal.
- One-way delay per direction = `delay + isl_delay`. `isl_delay` applies to both directions at the same moment. `isl_delay = 0` is the plain bent-pipe case.

### Main-satellite switch event

Changes the ISL path and downlink together:

```toml
[[event]]
t = 300
switch = { isl_delay = "12ms", downlink = { delay = "22ms", rate = "150mbit" }, gap_ms = 0 }
```

- Applied with `tc qdisc change`, so queued packets keep their old delay.
- Shorter path: new packets arrive before old ones (reordering). Longer path: a delivery gap.
- `gap_ms` (default 0): brief blackout during the switch; new arrivals are dropped for its duration.

## CLI

| Command | Purpose |
|---|---|
| `satlink up [--image IMG]` / `satlink down` | Start/stop the compose stack, discover veths, disable offloads |
| `satlink apply <preset or scenario>` | Manual mode: apply the static profile (no timeline), save it to `.satlink-state.json`, exit |
| `satlink outage <seconds> [--dir up\|down\|both]` | Manual outage on the profile last applied: blackhole, wait, restore |
| `satlink run <scenario.toml>` | Automated mode: apply, run timeline and transfer, collect results, plot |
| `satlink validate` | Self-test: ping RTT and iperf3 throughput in each direction match the profile |
| `satlink clear` | Remove all qdiscs |

No live control during automated runs in v1.

## Scenario format (TOML)

```toml
preset = "leo"                          # base profile, then overrides

[downlink]
rate = "200mbit"
delay = "20ms"
jitter = "4ms"
queue_ms = 300
netem = "loss gemodel 0.5% 40%"        # raw netem options appended as-is, optional

[uplink]
rate = "15mbit"
delay = "20ms"
queue_ms = 200

[isl]
delay = "25ms"

[[event]]
t = 60
set = { "downlink.rate" = "20mbit" }

[[event]]
t = 120
outage = { duration = 180, dir = "both" }   # up | down | both

[[event]]
t = 300
switch = { isl_delay = "12ms", downlink = { delay = "22ms" }, gap_ms = 0 }

[app]
sender = "ground"                       # or "terminal"
file_size = "500M"
recv_cmd = "..."
send_cmd = "..."
timeout = 3600
```

Parsed with `tomllib` (Python 3.11+ stdlib). Three event types: `set`, `outage`, `switch`.

## Presets

| Preset | Down / Up | One-way delay | Queue |
|---|---|---|---|
| `leo` | 200M / 15M | 25ms | 300ms |
| `narrowband` | 300k / 64k | 30ms | 1000ms |
| `gigabit` | 1G / 50M | 25ms | 100ms |

## Results

Each run writes `results/<timestamp>-<scenario>/`:

| File | Contents |
|---|---|
| `summary.json` | Exit code, duration, goodput, sha256 match |
| `timeseries.csv` | 1s samples from `tc -s`: delivered bytes, drops, backlog, configured rate and delay, per direction |
| `plot.png` | Delivered throughput vs configured rate per direction, with markers for `set`, `outage` and `switch` events |
| `tc.log` | Every tc command applied, timestamped |

Outage recovery time and switch stalls are read off the plot and time series.

## Containers

- `satlink-endpoint`: Debian-based image with iperf3, UFTP, sha256sum, coreutils.
- `docker/compose.yml`: `ground` and `terminal` on one user-defined bridge.
- Your own apps replace the endpoint image with `satlink up --image <image>`; commands come from the scenario.

## Repository layout

```
.gitignore
satlink.py                 # entire tool: CLI, tc builder, discovery, timeline, harness, metrics, plot
presets/{leo,narrowband,gigabit}.toml
scenarios/
  iperf3-baseline.toml
  uftp-baseline.toml
  narrowing-link.toml
  long-outage.toml
  isl-path-changes.toml
docker/
  compose.yml
  endpoint/Dockerfile
README.md
```

## Phases

### Phase 1: core link

- `up`, `down`, interface discovery, offload disabling
- netem + tbf chain builder, `apply`, `clear`, `outage`
- `validate`: ping RTT and iperf3 throughput per direction, including 1 Gbps and 300 kbps
- Go/no-go on netem + tbf at 1 Gbps (fallback below)

### Phase 2: timeline

- `run` with `set`, `outage` and `switch` events
- Check that a delay drop produces out-of-order datagrams in iperf3 UDP

### Phase 3: harness and output

- Run sender/receiver commands, timeout, exit code
- sha256 verification
- 1s tc sampling, `summary.json`, `timeseries.csv`, `plot.png`

### Phase 4: finish

- Presets and example scenarios
- README with usage and pitfalls

## Testing

`satlink validate` plus a short run of each example scenario. Requires root and Docker.

## Risks

| Item | Mitigation |
|---|---|
| netem + tbf at 1 Gbps | Tested in Phase 1. Fallback: a router netns on the host with the queue on one interface and netem on another |
| Development environment | Needs `iproute2`, `ethtool` and a running Docker daemon for testing |

## CFDP sender and receiver (implemented)

Built on `cfdp-py` 0.7.0 (`cfdppy`, Apache-2.0). It supplies the Class 1/2 state machines and PDU encoding; the glue is ours: one script, `cfdp_entity.py`, with `send` and `recv` modes.

- UDP port 4444, one socket per entity. Data PDUs use the downlink, NAK/ACK/Finished PDUs the uplink. Entity IDs: ground 1, terminal 2.
- `--class 1|2` per run. Class 1: no retransmission, success means EOF sent. Class 2: NAK-based retransmission, success means Finished received.
- `--rate <tc units>`: token-bucket pacing of every outgoing PDU, retransmissions included. CFDP has no congestion control, so this is how the sender is sized to the link. `0` = unpaced. Default segment 1400 bytes (no IP fragmentation at MTU 1500).
- Timer flags map one-to-one to cfdp-py (`--ack-interval/--ack-limit`, `--nak-interval/--nak-limit`, `--check-interval/--check-limit`). cfdp-py has no inactivity timer; `--timeout` is a local wall-clock limit. Entities are blind to outages, so outage scenarios set long timers. Library defaults (10 s intervals, limit 2) end a transaction in roughly 20 s of silence.
- Exit codes: 0 success, 2 CFDP fault (condition code printed), 3 local timeout. The last log line is a JSON report (condition code, bytes, duration, PDUs, retransmitted bytes, NAK counts); satlink copies it into `summary.json` as `sender_report` / `receiver_report`.
- One file per run. Receiver writes under `--out-dir` (default `/data`); the sender's DEST path is mapped into it.
- Python ceiling: about 220 Mbit/s of file data per sender on a modern core, so the 1 Gbit/s end of the range needs a non-Python CFDP implementation (see Later).
- Image: `python3` plus `cfdp-py==0.7.0` in `/opt/cfdp`, `cfdp` on the PATH. The compose build context is now the repository root.
- Local development uses uv (`pyproject.toml`, `.venv`).
- Example scenarios: Class 2 and Class 1 over LEO with bursty loss, narrowband (NAKs over a 64 kbit/s uplink), long outage with long timers.

Testing plan: loopback transfer per class with sha256 check; paced transfer over a rate-limited veth pair; loss and blackhole injection (Class 2 must deliver an identical file, Class 1 must report missing data; default timers must fault in an outage, long timers must complete). Real link tests run through satlink on a host with netem and Docker.

## Later

- Live control over a Unix socket during automated runs
- Faster CFDP entity for gigabit rates (C/Rust implementation, or multi-process sender)
- Link-aware CFDP freeze/thaw: satlink tells the CFDP entities when the link goes down or up (like a known contact schedule) so they suspend timers during outages, instead of relying on long blind timers
- Periodic LEO handovers (15s, wall-clock aligned, seeded delay draws, loss bursts)
- CAKE/AQM queue option
- Cross-traffic sidecars and timeline events
- `gap_flush` (drop in-flight packets on a switch)
- Richer metrics: per-outage recovery time, switch stall, finer sampling, delay on a second plot axis
- Fuller `validate` (loss-model tolerance, reordering checks)
- More presets (`leo-degraded`, `leo-congested`, `generic-geo`, `stress`, `isl-relay`)
- Scenario labels for roles
- Orbit-based path generator emitting `switch` events
- Explicit per-satellite nodes and DTN store-and-forward (ION, µD3TN, HDTN)
- Separate terrestrial return path with policy routing
- VM support (libvirt `vnetX` / tap interfaces)
- MTU knob, IPv6, TCP congestion control knobs

## Implementation status

Phases 1 to 4 are implemented in `satlink.py`. Checked without root-level netem or Docker: dry-run command generation, timeline ordering, tbf rate accuracy (64 kbit to 1 Gbit, on a veth pair), `validate`'s iperf3 parsing, plots. Still to run on a real host: veth discovery, netem behavior, `validate`, UFTP baseline, the 1 Gbit + 600 ms go/no-go. See README.md "Status".
