# tc and netem tutorial

A hands-on introduction to Linux traffic control, the layer satlink is built
on. You run it inside a throwaway container made from the satlink endpoint
image, and inside that container you build two network namespaces joined by a
virtual cable. Nothing touches your host's network, and leaving the container
removes everything. Run the blocks in order; it takes about 30 minutes.

## Five concepts first

1. **qdisc (queueing discipline):** the rule that decides how packets leave an
   interface. Every interface has one.
2. **Egress only:** tc shapes packets
   *leaving* an interface. To shape traffic going to a container, you shape the
   interface pointing at it. That's why satlink shapes the host-side veth.
3. **netem:** adds delay, jitter, loss, duplication and reordering. It has no real
   notion of bandwidth.  4. **tbf:** limits rate, and queues excess packets up to
   a latency limit, then drops them. This is your "link speed plus modem buffer".
5. **Chaining:** a qdisc can have a child. `root handle 1: netem` with a child
   at `parent 1:1` called `tbf` is exactly what satlink builds.

## Step 0: start the lab container (on the host)

```bash
# Run these three commands on the HOST, from the repository root.

# Build the endpoint image. It already contains everything the tutorial uses:
# ip and tc (iproute2), iperf3, ping, ethtool and pkill. `satlink up` builds the
# same image, so skip this if you have already run it.
sudo docker build -f docker/endpoint/Dockerfile -t satlink-endpoint .

# Containers share the host's kernel, so netem and tbf must be available in the
# HOST kernel. Loading the modules here avoids "Specified qdisc kind is unknown".
sudo modprobe sch_netem
sudo modprobe sch_tbf

# Start a throwaway container and open a shell in it.
#   --rm          -> delete the container when you exit the shell
#   -it           -> interactive terminal
#   --privileged  -> allow creating namespaces and changing qdiscs inside the container.
#                    The satlink containers never get this; it is only for this lab.
#   --name tc-lab -> a name, so a second terminal can join with: sudo docker exec -it tc-lab bash
#   bash          -> run a shell instead of the image's default `sleep infinity`
sudo docker run --rm -it --privileged --name tc-lab satlink-endpoint bash
```

From here until the satlink section, every command runs **inside the
container**, at the `root@...:/data#` prompt. You are already root there, so
there is no `sudo`.

Some steps ask for a second terminal. Open one on the host and run `sudo docker
exec -it tc-lab bash` to get another shell in the same container.

## Step 1: build the sandbox

```bash
# A network namespace is an isolated copy of the network stack: its own interfaces,
# addresses, routes and qdiscs. Docker gave this container one; we make two more inside it.
ip netns add a          # namespace "a" plays the sender
ip netns add b          # namespace "b" plays the receiver

# A veth pair is a virtual Ethernet cable: two interfaces, and whatever is sent
# into one comes out of the other. Created in the container's own namespace first.
ip link add va type veth peer name vb

# Move each end of the cable into its namespace. va now exists only inside "a",
# vb only inside "b". They disappear from the container's `ip link` list.
ip link set va netns a
ip link set vb netns b

# `ip -n NAME ...` runs an ip command inside a namespace.
# Give each end an address on the same /24 subnet, then bring the interface up
# (interfaces start "down" and pass no traffic until set up).
ip -n a addr add 10.0.0.1/24 dev va
ip -n a link set va up
ip -n b addr add 10.0.0.2/24 dev vb
ip -n b link set vb up

# `ip netns exec NAME CMD` runs any program inside a namespace.
# Ping from a to b. Expect ~0.05 ms: a veth is just memory copies, no real wire.
ip netns exec a ping -c 3 10.0.0.2
```

## Step 2: add delay

```bash
# Anatomy of a tc command:
#   tc qdisc add      -> attach a new queueing discipline (qdisc)
#   dev va            -> on interface va
#   root              -> as the top-level qdisc (every packet leaving va passes it)
#   netem delay 100ms -> the qdisc type and its options
# tc only acts on EGRESS: packets *leaving* va. So only a->b traffic is delayed.
ip netns exec a tc qdisc add dev va root netem delay 100ms

# The ping request leaves va (+100 ms); the reply leaves vb (no qdisc, +0 ms).
# RTT is therefore ~100 ms, not 200.
ip netns exec a ping -c 3 10.0.0.2

# Now delay the other direction too, from inside namespace b on vb.
# Using a different value shows the asymmetry: RTT = 100 + 50 = ~150 ms.
ip netns exec b tc qdisc add dev vb root netem delay 50ms
ip netns exec a ping -c 3 10.0.0.2

# List qdiscs on an interface. Use this whenever you're unsure what's applied.
ip netns exec a tc qdisc show dev va
```

## Step 3: change settings live and add loss

```bash
# `change` modifies the existing qdisc in place. Packets already queued inside it
# are kept, which is why satlink uses `change` for rate and delay steps.
# All options are restated: "delay 100ms" must be repeated or it resets to 0.
# loss 10% = each packet independently has a 10% chance of being dropped.
ip netns exec a tc qdisc change dev va root netem delay 100ms loss 10%

# 50 pings, 0.2 s apart; `tail -2` shows just the summary lines.
# Expect around 10% packet loss (it's random, so 6-14% is normal for 50 samples).
ip netns exec a ping -c 50 -i 0.2 10.0.0.2 | tail -2

# Gilbert-Elliott (gemodel) loss: a two-state model. The link is usually in a
# "good" state; with probability 1% per packet it enters a "bad" state, where packets
# are dropped, and leaves it with probability 30% per packet. Losses come in bursts,
# like a satellite fade, instead of being evenly spread.
ip netns exec a tc qdisc change dev va root netem delay 100ms loss gemodel 1% 30%
ip netns exec a ping -c 200 -i 0.05 10.0.0.2 | grep -c "icmp_seq"   # count replies
# Watch the icmp_seq numbers in a full ping: gaps come in clusters.
```

## Step 4: limit bandwidth with tbf

```bash
# Remove the netem qdiscs. `del ... root` returns the interface to its default qdisc
# and discards anything that was queued.
ip netns exec a tc qdisc del dev va root
ip netns exec b tc qdisc del dev vb root

# Start an iperf3 server in b. -s = server mode, -D = run in the background (daemon).
ip netns exec b iperf3 -s -D

# Measure the unshaped link: a TCP test from a to b for 5 seconds (-t 5).
# Expect tens of Gbit/s; veths are only limited by CPU.
ip netns exec a iperf3 -c 10.0.0.2 -t 5

# tbf = Token Bucket Filter. Options:
#   rate 10mbit   -> long-term average rate allowed out (tc units: kbit, mbit, gbit)
#   burst 10kb    -> bucket size: how many bytes can go out back-to-back at full speed.
#                    Must be at least one packet (1514 B); too small and tbf under-delivers.
#   latency 300ms -> how long a packet may wait in tbf's queue before being dropped.
#                    This is the "modem buffer"; satlink's queue_ms sets it.
ip netns exec a tc qdisc add dev va root tbf rate 10mbit burst 10kb latency 300ms

# Same test again: expect ~9.5 Mbit/s (a little under 10 because of header overhead).
ip netns exec a iperf3 -c 10.0.0.2 -t 5
```

## Step 5: chain netem and tbf (what satlink builds)

```bash
ip netns exec a tc qdisc del dev va root

# Parent: netem as root, with handle "1:". A handle is a name for the qdisc
# so children can be attached to it. netem provides one slot for a child, called 1:1.
ip netns exec a tc qdisc add dev va root handle 1: netem delay 25ms

# Child: tbf, attached at parent 1:1, named 10:.
# Packets now flow: leave va -> netem holds each for 25 ms -> tbf enforces 10 Mbit/s
# and queues up to 300 ms of excess -> out on the wire.
ip netns exec a tc qdisc add dev va parent 1:1 handle 10: tbf rate 10mbit burst 10kb latency 300ms

# -s adds statistics. For each qdisc you'll see:
#   Sent X bytes Y pkt   -> total that left this qdisc
#   dropped N            -> packets discarded (netem loss, or tbf queue overflow)
#   backlog B b P p      -> bytes and packets waiting in the queue right now
ip netns exec a tc -s qdisc show dev va

# Run a long test in the background and look at the stats while it runs.
# tbf's backlog grows: TCP keeps filling the 300 ms buffer. That is bufferbloat:
# ping RTT during the transfer rises from ~25 ms toward ~325 ms.
ip netns exec a iperf3 -c 10.0.0.2 -t 20 &
sleep 5; ip netns exec a tc -s qdisc show dev va
ip netns exec a ping -c 5 10.0.0.2          # RTT inflated by the queue
wait
```

## Step 6: an outage, the way satlink does it

```bash
# In a second terminal (host: sudo docker exec -it tc-lab bash), watch the outage:
#   ip netns exec a ping 10.0.0.2

# del + add instead of `change`: deleting the root qdisc throws away every packet
# currently queued or held for delay. That matches a loss of signal, where data
# "in the air" never arrives.
ip netns exec a tc qdisc del dev va root

# loss 100% = drop everything leaving va. No error is reported to the sender;
# packets just vanish. The interface stays "up", as with a real satellite fade.
ip netns exec a tc qdisc add dev va root netem loss 100%

sleep 10            # the outage; the ping terminal shows no replies, no errors

# Link reacquired: remove the blackhole. In satlink, this step rebuilds the
# full netem + tbf chain from the scenario's current settings.
ip netns exec a tc qdisc del dev va root

# This was a one-way (downlink) outage: b's replies were never blocked, only a's
# requests. Do the same on vb in namespace b for a two-way outage.
```

## Step 7: clean up

```bash
# Optional: removes the namespaces now, if you want to start over without leaving.
pkill iperf3
ip netns del a
ip netns del b

# Leave the container. Because it was started with --rm, Docker deletes it, along
# with every namespace, veth and qdisc you created inside it.
exit
```

## Pitfalls, explained

- **Shape the receiving side, not the sender's own interface.** When tbf sits
  on the sending host's interface, the kernel pushes back on TCP locally before
  anything is dropped. TCP then behaves as if the link were perfect but slow, and
  you never see loss recovery. In this sandbox, `va` stood in for a router's
  outgoing port. satlink uses the host-side veth for the same reason.
- **Offloads distort netem.** With TSO, GSO or GRO on, the kernel builds
  packets of up to 64 KB and splits them later, so netem "drops one packet"
  that was really about 45. Check with `ip netns exec a ethtool -k va | grep
  offload` and turn them off with `ip netns exec a ethtool -K va tso off gso off
  gro off`. satlink does this for you in `up`.
- **`replace` vs `del`+`add`:** `tc qdisc replace` on a qdisc of the same type
  acts like `change` and keeps the queue. Only `del` flushes it.
- **Jitter reorders:** `netem delay 25ms 5ms` gives each packet a random delay,
  so later packets can overtake earlier ones. That's realistic, but be aware of
  it when you see reordering in results.
- **The kernel is the host's.** A container brings its own tools but not its
  own kernel. If a qdisc is missing, load it on the host with `sudo modprobe`,
  not inside the container.

## Then connect it to satlink (on the host)

satlink itself runs on the host, not in a container: it starts the containers
and shapes their host-side veths, which a container cannot reach. Exit the lab
container first.

```bash
# Prints the exact tc commands for the "leo" preset, without running anything.
# Each line should now read like steps 4-5 above.
./satlink.py --dry-run apply leo

# Builds the Docker image, starts the satellite and ground containers, finds their
# host-side veths and turns off offloads.
sudo ./satlink.py up

# For each preset: applies it, pings to check RTT, and uses iperf3 UDP to check each
# direction's rate. Prints ok/FAIL per check.
sudo ./satlink.py validate

# Runs a scenario. Afterward, open results/<timestamp>-<name>/tc.log to see every
# tc command with its time, alongside plot.png showing the effect.
sudo ./satlink.py run scenarios/narrowing-link.toml
```

## Further reading

`man tc`, `man tc-netem` and `man tc-tbf` are short and worth skimming after
step 5. They are on the host; the slim image has no man pages.

Why TCP runs slower than the link on long-delay paths: [window-rtt-bdp.md](window-rtt-bdp.md).
