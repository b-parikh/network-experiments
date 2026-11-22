# satlink: satellite link emulator for file-transfer testing

## Goal

Test file-transfer applications and protocols (TCP, custom UDP, QUIC, DTN/CFDP) over an emulated satellite link with:

- Asymmetric bandwidth, downlink from a few hundred kbps up to 1 Gbps, uplink with its own profile
- A link that narrows in steps over time
- Random and bursty packet loss
- Abrupt outages that reacquire minutes later (silent blackhole, both ways or one way)
- Topology: one satellite and one ground station, nothing else. No user terminal, no inter-satellite links.

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
| Roles | `satellite` (source of the file in the downlink case) and `ground` (ground station). Downlink = satellite -> ground |
| Outages | Silent blackhole, `up`, `down` or `both`. Queued packets are dropped |
| Return path | Same path as forward, independent up/down shaping |
| Queueing | FIFO only, sized in ms. Presets use about one base RTT (1 BDP); deep buffers live in a dedicated scenario |
| Loss/jitter | Raw netem options passed through (e.g. Gilbert-Elliott) |
| Scenarios | TOML timelines with step changes, plus 3 presets |
| Run end | Ends when the transfer exits or times out. Timeline holds its last state if it runs out |
| Modes | Automated (runs the apps) and manual (only applies the link) |

## Architecture

```
 [satellite container] --veth-- docker bridge --veth-- [ground container]
          ^                                                ^
  host-side veth egress = UPLINK chain        host-side veth egress = DOWNLINK chain
```

### Shaping placement

Each direction is shaped on the host side of the destination container's veth. Nothing is shaped inside the containers.

Reason: shaping on the sender's own interface lets TCP Small Queues and local qdisc backpressure throttle the sender without loss, which gives misleading results. The bottleneck must be a non-local hop.

### Per-direction qdisc chain

```
root:  netem  delay <one-way delay> [raw netem options: jitter, loss, ...]
child: tbf    rate <rate> latency <queue ms>   (FIFO bottleneck)
```

- `tbf` is the bottleneck and its `latency` sets the buffer depth in ms.
- netem orders packets by send time, so a drop in delay lets new packets overtake old ones. So a `set` that lowers the delay produces realistic reordering with no custom code.
- netem's built-in `rate` is not used: it serializes packets behind the queue tail (no reordering) and sizes buffers in packets.

### Offloads

Disable TSO, GSO and GRO on both ends of each veth (`ethtool -K`, using `nsenter` for the container side). Otherwise netem treats a 64KB GSO super-packet as one packet, and a single drop removes dozens of real packets.

### Interface discovery

For each container, read `/sys/class/net/eth0/iflink` inside its namespace (`nsenter -t <pid> -n`) and match it to the host interface index. Rediscover on every command, since container restarts change veth names.

### Link operations

| Operation | Implementation |
|---|---|
| Parameter change (rate, delay, loss) | `tc qdisc change` on the affected stage. Queued packets are kept |
| Outage start | `tc qdisc replace` root with `netem loss 100%`. Flushes queued and in-flight packets |
| One-way outage | Same, on only one direction's veth |
| Outage end | Rebuild the chain from current state |

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

[[event]]
t = 60
set = { "downlink.rate" = "20mbit" }

[[event]]
t = 120
outage = { duration = 180, dir = "both" }   # up | down | both

[app]
sender = "satellite"                    # or "ground"
file_size = "500M"
recv_cmd = "..."
send_cmd = "..."
timeout = 3600
```

Parsed with `tomllib` (Python 3.11+ stdlib). Two event types: `set`, `outage`.

## Presets

| Preset | Down / Up | One-way delay | Queue |
|---|---|---|---|
| `leo` | 200M / 15M | 25ms | 50ms |
| `narrowband` | 300k / 64k | 30ms | 60ms |
| `gigabit` | 1G / 50M | 25ms | 50ms |

## Results

Each run writes `results/<timestamp>-<scenario>/`:

| File | Contents |
|---|---|
| `summary.json` | Exit code, duration, goodput, sha256 match |
| `timeseries.csv` | 1s samples from `tc -s`: delivered bytes, drops, backlog, configured rate and delay, per direction |
| `plot.png` | Delivered throughput vs configured rate per direction, with markers for `set` and `outage` events |
| `tc.log` | Every tc command applied, timestamped |

Outage recovery time is read off the plot and time series.

## Containers

- `satlink-endpoint`: Debian-based image with iperf3, UFTP, sha256sum, coreutils.
- `docker/compose.yml`: `satellite` and `ground` on one user-defined bridge.
- Your own apps replace the endpoint image with `satlink up --image <image>`; commands come from the scenario.

## Repository layout

```
.gitignore
satlink.py                 # entire tool: CLI, tc builder, discovery, timeline, harness, metrics, plot
presets/{leo,narrowband,gigabit}.toml
scenarios/
  iperf3-baseline.toml
  bufferbloat.toml
  uftp-baseline.toml
  narrowing-link.toml
  long-outage.toml
  cfdp-class2-leo.toml, cfdp-class1-leo.toml, cfdp-narrowband.toml, cfdp-long-outage.toml
apps/cfdp/cfdp_entity.py   # CFDP sender/receiver, installed in the image as cfdp-entity
docker/
  compose.yml
  endpoint/Dockerfile
README.md
```

## CFDP entities

`apps/cfdp/cfdp_entity.py` is a CFDP file sender and receiver (Class 1 and 2) over UDP, built on cfdp-py 0.7.0 (`cfdppy`, Apache-2.0), which provides the state machines and PDU encoding but no transport. Our code is a single-threaded `select` loop, a token-bucket pacer and a CLI.

| Choice | Decision |
|---|---|
| Library | cfdp-py. LibreCube `cfdp` 0.2.0 was the alternative: less glue, but older and no pacing |
| Classes | Both, `--class 1\|2` per run |
| Pacing | `--rate`, counting wire bytes (PDU + 42). CFDP has no congestion control |
| Outages | Entities are blind to them. Timer limits are flags set per scenario (`--ack-*`, `--nak-*`, `--check-*`) |
| Entity IDs | satellite = 1, ground = 2, UDP port 4444 |
| Reports | One JSON line per side, captured into `summary.json` |
| Faults | Checksum failure cancels (the library default ignores it) so Class 1 loss is visible |

The endpoint image installs cfdp-py in `/opt/cfdp` and the script as `cfdp-entity`. The compose build context is the repository root so the Dockerfile can copy it.

Scenarios: `cfdp-class2-leo` (bursty loss), `cfdp-class1-leo`, `cfdp-narrowband` (NAKs over a 64 kbit/s uplink), `cfdp-long-outage` (3 minute blackhole, long timers).

## Phases

### Phase 1: core link

- `up`, `down`, interface discovery, offload disabling
- netem + tbf chain builder, `apply`, `clear`, `outage`
- `validate`: ping RTT and iperf3 throughput per direction, including 1 Gbps and 300 kbps
- Go/no-go on netem + tbf at 1 Gbps (fallback below)

### Phase 2: timeline

- `run` with `set` and `outage` events
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

## Later

- Link-aware CFDP freeze/thaw: satlink tells the entities when an outage starts and ends (a known contact schedule, e.g. via a state file or UDP control message) so they suspend their timers instead of relying on long blind timers
- Live control over a Unix socket during automated runs
- Pass/contact schedules: AOS/LOS windows, elevation-dependent delay and rate
- CAKE/AQM queue option
- Cross-traffic sidecars and timeline events
- Richer metrics: per-outage recovery time, finer sampling, delay on a second plot axis
- Fuller `validate` (loss-model tolerance, reordering checks)
- More presets (`leo-degraded`, `leo-congested`, `generic-geo`, `stress`)
- Scenario labels for roles
- Orbit-based pass generator emitting `set` and `outage` events (delay and rate over a pass, LOS)
- Explicit per-satellite nodes and DTN store-and-forward (ION, µD3TN, HDTN)
- Separate terrestrial return path with policy routing
- VM support (libvirt `vnetX` / tap interfaces)
- MTU knob, IPv6, TCP congestion control knobs

## Implementation status

The CFDP entity is implemented and tested on loopback behind a lossy/blackholing UDP proxy (Python 3.11 and 3.12). The image build and the CFDP scenarios over the real emulated link are not yet run.

Phases 1 to 4 are implemented in `satlink.py`. Checked without root-level netem or Docker: dry-run command generation, timeline ordering, tbf rate accuracy (64 kbit to 1 Gbit, on a veth pair), `validate`'s iperf3 parsing, plots. Still to run on a real host: veth discovery, netem behavior, `validate`, UFTP baseline, the 1 Gbit + 600 ms go/no-go. See README.md "Status".
