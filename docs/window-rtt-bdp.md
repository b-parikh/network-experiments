# Throughput, window, RTT and the bandwidth-delay product

Why a TCP transfer over a satellite link can run far below the link rate, even
with no loss. Companion to [tc-tutorial.md](tc-tutorial.md).

## The rule

**throughput ≤ window ÷ RTT**

A TCP sender may have at most one **window** of unacknowledged data
outstanding, and it takes one **RTT** for any of that data to be acknowledged.
So it can send at most one window per round trip. One window per RTT *is* the
rate: window ÷ RTT.

### Walking through it

Say the window is 1 MB and the RTT is 100 ms, on a link much faster than that.

```
t = 0 ms     sender sends 1 MB, then must stop: the window is full
t = 0-100    data travels to the receiver; ACKs travel back
t = 100 ms   ACKs for that 1 MB arrive, the window frees up, the next 1 MB can go
```

That's 1 MB every 100 ms, or 10 MB/s (80 Mbit/s), however fast the link is. If
you made the link 10 times faster, the 1 MB would leave sooner, but the sender
would still wait out the rest of the 100 ms for ACKs, so throughput wouldn't
change.

In a steady stream, the waiting is spread out: each ACK that comes back frees
room to send a little more. The average is the same, one window per RTT.

### What "window" means here

The sender may have at most this much data outstanding. It is the smaller of
two limits:

| Limit | Set by | Purpose |
|---|---|---|
| **Congestion window (cwnd)** | The sender | Don't overload the *network*. It grows while there's no loss and shrinks on loss |
| **Receive window (rwnd)** | The receiver | Don't overflow the *receiver's* buffer. Bounded by its socket buffer size |

**window = min(cwnd, rwnd)**

iperf3's `Cwnd` column shows only the congestion window. If it stops growing
with no retransmits, the connection is usually blocked by the receive window
instead, which has hit the receive buffer ceiling (on Linux, the third number
of `net.ipv4.tcp_rmem`).

### Why it's "≤" and not "="

window ÷ RTT is a **ceiling**. Three things can hold throughput below it:

1. **The link is slower.** Throughput can't exceed the link rate, so in full:
   **throughput ≤ min(link rate, window ÷ RTT)**. Whichever is smaller is the
   bottleneck.
2. **The window isn't full yet.** After a connection starts or after a loss,
   cwnd is small and grows over several RTTs. That's the slower first second in
   most iperf3 runs.
3. **The sender has nothing to send:** an app that writes slowly, or small
   requests.

### The RTT here is the real one, queueing included

When a link is congested, packets wait in its buffer. That waiting is part of
the RTT TCP experiences:

**RTT = propagation delay + time spent in queues**

That's the bufferbloat effect from Step 5 of the tutorial: tbf's queue filled,
ping went from 25 ms to about 325 ms, and every window-limited flow on that
link slowed down accordingly.

## The bandwidth-delay product

Rearrange the rule for the window needed to reach a target rate:

**BDP = link rate × RTT**

The BDP is the amount of data in flight on a full link: sent, but not yet
acknowledged. Think of the link as a pipe: the rate is its width and the RTT
its length, so the BDP is its volume. To keep the pipe full, the window must be
at least one BDP.

- **Window ≥ BDP:** the link is the bottleneck, and you get full rate.
- **Window < BDP:** the window is the bottleneck, and throughput = window ÷ RTT.

It's the same equation seen from two sides:

| You know | You get |
|---|---|
| window and RTT | the best possible throughput: window ÷ RTT |
| rate and RTT | the window you need: rate × RTT (BDP) |

### BDP for satlink's presets

| Link | Rate | RTT | BDP |
|---|---|---|---|
| `leo` downlink | 200 Mbit/s | ~50 ms | 1.25 MB |
| `gigabit` downlink | 1 Gbit/s | ~50 ms | 6.25 MB |
| `narrowband` downlink | 300 kbit/s | ~60 ms | 2.25 KB (under 2 packets) |
| GEO, for comparison | 1 Gbit/s | ~600 ms | 75 MB |

A 7.65 MB window fills the `leo` and `gigabit` links, but caps a GEO gigabit
link at about 100 Mbit/s.

## Worked example: the tutorial's "slow" iperf3 run

Running Step 4's unshaped test with Step 2's `netem delay 100ms` still on `va` gave:

```
[ ID] Interval           Transfer     Bitrate         Retr  Cwnd
[  5]   0.00-1.00   sec  48.0 MBytes   403 Mbits/sec    0   7.65 MBytes
[  5]   1.00-2.00   sec  75.0 MBytes   629 Mbits/sec    0   7.65 MBytes
...
```

- Cwnd flat at 7.65 MB with 0 retransmits: window-limited, not loss- or
  rate-limited.
- 7.65 MB × 8 = 61.2 Mbit of window, divided by 0.1 s RTT = 612 Mbit/s. iperf3
  reported 629 Mbit/s. The small gap comes from rounding in iperf3's display
  and from the RTT being slightly under 100 ms: the delay is in one direction
  only, and the return path adds almost nothing.
- On an unshaped veth (RTT about 0.05 ms) the BDP is a few hundred KB, the same
  window covers it many times over, and the run reaches tens of Gbit/s.

**Try it:** in the lab container, set `netem delay 100ms` on `va`, then run
iperf3 with `-w 1M`, `-w 4M` and `-w 16M`. Throughput rises roughly in step
with the window until the window reaches the BDP.

## Why this matters for satellites and satlink

- **Delay sets the ceiling.** With the same window, doubling the RTT halves the
  throughput. Satellite RTTs are 10 to 1000 times terrestrial ones.
- **Loss recovery is slow.** After a loss, TCP cuts cwnd and regrows it by
  roughly one packet per RTT. With a long RTT that regrowth takes seconds, so
  the same loss rate hurts far more on a long, fast link.
- **Buffer sizing.** satlink's `queue_ms` is a buffer measured in time. A
  buffer of about 1 BDP (`queue_ms` ≈ RTT) is the classic rule for keeping a
  single TCP flow busy. Much more than that only adds delay.
- **Raising the receive buffer** (`net.ipv4.tcp_rmem`, plus
  `net.core.rmem_max`) is the standard fix for a window-limited high-BDP TCP
  flow.
- **CFDP has no window.** It sends at a fixed `--rate` and asks for the missing
  pieces afterwards (NAKs), so its throughput is set by `--rate`, not by the
  RTT. The BDP still matters to it in two ways:
  - Every outage, or every lost packet before a NAK arrives, costs about one
    BDP of data that has to be re-sent.
  - The UDP socket buffers must hold bursts of that size, which is why the CFDP
    entity requests 8 MB socket buffers.
