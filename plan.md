# satlink: satellite link emulator for file-transfer testing

## Goal

Test file-transfer applications and protocols (TCP, custom UDP, QUIC, DTN/CFDP) over an emulated satellite link with:

- Asymmetric bandwidth, downlink from a few hundred kbps up to 1 Gbps, uplink with its own independent profile
- A link that narrows in steps over time
- Random and bursty packet loss
- Abrupt outages that reacquire minutes later (silent blackhole, both ways or one way)
- LEO (Starlink-like) behavior: 15s handover latency jumps, jitter, reordering
- ISL relay: file originates on a source satellite, crosses inter-satellite links to the main satellite, then downlinks. Path changes alter latency.
- Cross traffic competing for the same bottleneck

Use standard Linux tools (`tc`/netem, `ip`, `ethtool`, iperf3, UFTP, Docker). Glue code in Python (stdlib) with matplotlib for plots.

## Requirements summary

| Area | Decision |
|---|---|
| Orbit regimes | LEO (Starlink-like) and generic parameterized. GEO preset included as a bonus |
| Protocols | TCP, custom UDP, QUIC, DTN/CFDP |
| Peak rate | Up to ~1 Gbps on a single host |
| Low end | ~100s of kbps, standard MTU 1500 |
| Host | Native Linux with root, Docker Engine |
| Emulator location | On the Docker host, shaping host-side veth interfaces. No `NET_ADMIN` in app containers |
| Endpoints | One sender, one receiver. Files move in both directions |
| Roles | `ground` (gateway side) and `terminal` (user dish side). Downlink = ground -> terminal |
| Outages | Silent blackhole, both ways or one way. Queued packets are dropped |
| Return path | Same path as forward, with independent up/down shaping |
| Queueing | Knob per scenario: deep FIFO (sized in ms) or AQM (CAKE) |
| LEO effects | 15s handover latency jumps with loss burst, jitter and reordering |
| Extra impairments | Gilbert-Elliott bursty loss, cross traffic |
| Scenarios | Scripted timelines (step changes) and named presets, in TOML |
| Run end | Ends when the transfer exits or times out. Timeline holds its last state if it runs out |
| Harness | Automated mode (runs the apps) and manual mode (only applies the link) |
| Live control | CLI commands that change the link mid-run |
| Metrics | Transfer outcome and time-series plots |
| Baselines | iperf3 and UFTP |
| TCP tuning | Not a knob. Apps use container defaults |
| VMs | Later. Document pointing the tool at libvirt `vnetX` interfaces |
| ISL | Aggregate delay-only segment (optical ISLs, never the bottleneck), same delay in both directions, changed through main-satellite switch events |

## Architecture

```
 [ground container] --veth-- docker bridge --veth-- [terminal container]
          ^                                                ^
  host-side veth egress = UPLINK chain        host-side veth egress = DOWNLINK chain
```

### Shaping placement

Each direction is shaped on the host side of the destination container's veth (egress into the receiving container). Nothing is shaped inside the containers.

Reason: shaping on the sender's own interface lets TCP Small Queues and local qdisc backpressure throttle the sender without loss. That gives misleading results. The bottleneck must be a non-local hop.

### Per-direction qdisc chain

```
root: netem (delay, jitter, Gilbert-Elliott loss, reorder)
  child: tbf + bfifo sized in ms   (deep FIFO, bufferbloat)
     or: cake bandwidth X          (AQM)
```

- The child queue is the bottleneck and determines queueing behavior.
- netem's tfifo orders packets by send time, so a drop in delay naturally lets new packets overtake old ones (used for handover and ISL reordering).

### Offloads

Disable TSO, GSO and GRO on both ends of each veth (`ethtool -K`, using `nsenter` for the container side). Otherwise netem treats a 64KB GSO super-packet as one packet, and a single drop removes dozens of real packets.

### Interface discovery

For each container, read `/sys/class/net/eth0/iflink` inside its network namespace (`nsenter -t <pid> -n`) and match it to the host interface index. Rediscover after any container restart.

### Link operations

| Operation | Implementation |
|---|---|
| Parameter change (rate, delay, loss, ISL delay, handover offset) | `tc qdisc change` on the affected stage. Queued packets are kept |
| Outage start | `tc qdisc replace` root with `netem loss 100%`. Flushes queued and in-flight packets (loss of signal) |
| One-way outage | Same, on only one direction's veth |
| Outage end | Rebuild the full chain from current state |
| Cross traffic | iperf3 sidecars with `network_mode: container:ground` / `container:terminal`, so they share the same veths and bottleneck |

### State model

The runner keeps a single link state per direction and recomputes netem whenever any input changes:

```
one-way delay = base delay + isl_delay + current handover offset
```

Timeline events and live control commands go through one event queue, so they never conflict.

### Fallback design

If `netem -> child qdisc` does not hold up at 1 Gbps, switch to a small router network namespace on the host (two bridges, one per side). The router's egress holds the modem queue (rate + AQM/FIFO) and the destination veth holds netem (propagation delay, loss). Phase 1 decides this.

## LEO handovers

- At seconds :12, :27, :42 and :57 of each minute (configurable interval and offset), the base delay steps to a new value drawn from `delay_range`.
- A short loss burst (`burst_ms`) is applied at each handover.
- Draws use a seeded Python RNG so schedules repeat across runs.
- Periodic handovers change only the downlink delay, not the ISL delay.

## ISL relay scenario

```
source sat --[ISL path: isl_delay]--> main sat --[downlink: rate/delay/loss/queue]--> ground
 ("ground" container)                                                      ("terminal" container)
```

- With the ground/terminal naming, the `ground` container plays the source satellite and `terminal` plays the receiving ground station. Downlink stays ground -> terminal.
- A scenario `label` lets plots show "source sat" and "ground station".
- `isl_delay` is added to both directions at the same moment. `isl_delay = 0` is the plain bent-pipe case.

### Main-satellite switch event

One event switches the main satellite, changing the ISL path and downlink together:

```toml
[[event]]
t = 300
switch = { isl_delay = "12ms", downlink = { delay = "22ms", rate = "150mbit" }, gap_ms = 0 }
```

- Applied with `tc qdisc change`, so packets already queued keep their old delay.
- Shorter path: new packets arrive before old ones (physical reordering).
- Longer path: a delivery gap.
- `gap_ms` (default 0): brief blackout during the switch. New arrivals are dropped for its duration.
- `gap_flush` (default false): also drop packets in flight on the old path. This removes the reordering.
- A future orbit-based generator (Walker constellation or TLEs, e.g. skyfield) would emit `switch` events in this same format.

## CLI

| Command | Purpose |
|---|---|
| `satlink up` / `satlink down` | Start/stop the compose stack, discover veths, disable offloads |
| `satlink apply <preset or scenario>` | Manual mode: apply a static profile and hold it |
| `satlink run <scenario.toml>` | Automated mode: run the timeline, launch the transfer, collect results, plot |
| `satlink ctl outage <seconds> --dir up\|down\|both` | Live outage |
| `satlink ctl set downlink.rate=10mbit` | Live parameter change |
| `satlink ctl switch isl_delay=12ms ...` | Live main-satellite switch |
| `satlink ctl status` | Show current link state and tc counters |
| `satlink validate` | Self-test that configured behavior is actually delivered |
| `satlink clear` | Remove all qdiscs |

`ctl` talks to the running `apply` or `run` process over a Unix socket.

## Scenario format (TOML)

```toml
preset = "leo-good"                    # base profile, then overrides
label = { ground = "source sat", terminal = "ground station" }

[downlink]
rate = "200mbit"
delay = "20ms"
jitter = "4ms"
queue = { type = "fifo", ms = 300 }    # or { type = "cake" }
loss = { model = "gemodel", p = 0.5, r = 40, h = 100, k = 0 }  # percent

[uplink]
rate = "15mbit"
delay = "20ms"
queue = { type = "fifo", ms = 200 }

[isl]
delay = "25ms"

[handover]
enabled = true
interval = 15
offset = 12
delay_range = ["20ms", "55ms"]
burst_ms = 80
seed = 42

[[event]]
t = 60
set = { "downlink.rate" = "20mbit" }

[[event]]
t = 120
outage = { duration = 180, dir = "both" }   # dir: up | down | both

[[event]]
t = 300
switch = { isl_delay = "12ms", downlink = { delay = "22ms" }, gap_ms = 0 }

[[event]]
t = 400
cross = { dir = "down", proto = "udp", rate = "50mbit", duration = 60 }

[app]
sender = "ground"                      # or "terminal"
image = "satlink-endpoint"             # replace with your own images
file_size = "500M"
recv_cmd = "..."
send_cmd = "..."
timeout = 3600
```

Parsed with `tomllib` (Python 3.11+ stdlib).

## Presets

| Preset | Down / Up | One-way delay | Notes |
|---|---|---|---|
| `leo-good` | 200M / 15M | ~25ms | Handovers on, light random loss |
| `leo-degraded` | 50M / 5M | ~30ms | Gilbert-Elliott bursty loss |
| `leo-congested` | 100M / 10M | ~30ms | Background cross traffic |
| `narrowband` | 300k / 64k | ~30ms | Deep FIFO |
| `gigabit` | 1G / 50M | ~25ms | Large buffers |
| `generic-geo` | 50M / 5M | ~300ms | No handovers |
| `stress` | 200M stepping down | ~30ms | Multi-minute outages |
| `isl-relay` | 200M / 15M | ~25ms + ISL | ISL delay steps between 5 and 40ms |

## Results

Each run writes `results/<timestamp>-<scenario>/`:

| File | Contents |
|---|---|
| `summary.json` | Exit code, completion time, goodput, sha256 match, outage count, per-outage recovery time (link restored to first new bytes delivered), switch count, delivery stall around each switch |
| `timeseries.csv` | Every 250ms from `tc -s`: delivered bytes/rate, backlog, drops per direction, current configured rate and one-way delay |
| `plot.png` | Delivered throughput vs configured capacity for both directions, one-way delay on a second axis, markers for outages, handovers and switches |
| `scenario.effective.toml` | Fully resolved scenario |
| `tc.log` | Every tc command applied, timestamped |

## Containers

- `satlink-endpoint`: Debian-based image with iperf3, UFTP, sha256sum, tcpdump, coreutils.
- `docker/compose.yml`: `ground`, `terminal`, and iperf3 cross-traffic sidecars sharing their network namespaces.
- Your own apps replace the endpoint image by setting `image` and commands in the scenario.

## Repository layout

```
satlink/
  satlink                  # CLI entry point
  satlink/
    tc.py                  # builds tc/ethtool commands from link state
    topology.py            # container and veth discovery, offloads
    scenario.py            # TOML parsing, presets, validation
    runner.py              # timeline, handovers, event queue
    control.py             # Unix socket for live control
    harness.py             # app execution, checksums, timeouts
    metrics.py             # tc counter sampling
    plot.py                # matplotlib plots
  presets/*.toml
  scenarios/examples/*.toml
  docker/
    compose.yml
    endpoint/Dockerfile
  tests/
    unit/                  # no root needed
    integration/           # root + Docker
  README.md
```

## Phases

### Phase 1: core link

- Topology, veth discovery, offload disabling
- tc chain builder, `apply`, `clear`
- `validate`:
  - ping RTT matches configured delay
  - iperf3 TCP/UDP throughput matches configured rate in each direction
  - 1 Gbps with 600ms RTT and 300 kbps both hold
  - Gilbert-Elliott loss rate within tolerance
  - Delay drop during iperf3 UDP produces non-zero out-of-order count; with `gap_flush` it is zero
- Go/no-go on the netem + child qdisc chain; fall back to the router netns design if needed

### Phase 2: dynamics

- Timeline runner with step changes
- Outages (both, up, down) with queue flush
- Periodic handovers with seeded delay draws and loss bursts
- ISL delay and main-satellite switch events
- Unix-socket live control

### Phase 3: harness and metrics

- App execution in automated mode, timeouts, exit codes
- sha256 checksum verification
- tc counter sampling, recovery-time and stall metrics
- Plots
- Cross-traffic events

### Phase 4: finish

- All presets
- Example scenarios: iperf3 baseline, UFTP baseline, narrowing link, long outage, `isl-path-changes.toml`
- README with usage and pitfalls
- Notes for VMs (point at libvirt `vnetX` / tap interfaces)

## Testing

- Unit tests (no root): TOML parsing, preset merging, delay arithmetic, tc command generation, event ordering.
- Integration tests (root + Docker): `satlink validate` plus short end-to-end runs of each example scenario.

## Risks and open items

| Item | Mitigation |
|---|---|
| netem + child qdisc at 1 Gbps | Tested in Phase 1, router netns fallback |
| netem per-packet random seed depends on kernel/iproute2 version | Timeline and handover randomness is seeded in Python regardless; document that per-packet loss may not repeat on older kernels |
| Container restarts change veth names | Rediscover interfaces on each command and before each run |
| Handover alignment to wall clock | Requires host clock synced (NTP) |
| Development environment | Needs `iproute2`, `ethtool` and a running Docker daemon in the dev container for integration tests |

## Future work

- Orbit-based path generator emitting `switch` events
- Explicit per-satellite nodes and DTN store-and-forward (ION, µD3TN, HDTN)
- Separate terrestrial return path with policy routing
- VM support with automatic tap discovery
- MTU knob, IPv6, TCP congestion control knobs
