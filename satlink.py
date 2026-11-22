#!/usr/bin/env python3
"""satlink: emulate an asymmetric satellite link between two Docker containers using tc.

Roles: `satellite` and `ground`. Downlink = satellite -> ground, uplink = ground -> satellite.
Each direction is shaped on the host-side veth of the RECEIVING container:

    root  1:  netem  (one-way delay + isl delay, jitter, raw netem options)
    child 10: tbf    (rate and FIFO depth in ms; the bottleneck)

Needs root, Docker, iproute2, ethtool and nsenter. See README.md and plan.md.
"""
import argparse
import csv
import functools
import json
import os
import re
import shlex
import subprocess
import sys
import threading
import time
import tomllib
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
COMPOSE = ROOT / "docker" / "compose.yml"
STATE_FILE = ROOT / ".satlink-state.json"
CONTAINER = {"satellite": "satlink-satellite", "ground": "satlink-ground"}
IP = {"satellite": "172.30.0.2", "ground": "172.30.0.3"}
DIRS = ("down", "up")
RECEIVER = {"down": "ground", "up": "satellite"}  # whose host veth carries the shaping
SECTION = {"down": "downlink", "up": "uplink"}
DIR_OF = {v: k for k, v in SECTION.items()}
DEFAULT_DIR = {"rate": "100mbit", "delay": "25ms", "jitter": "0ms", "queue_ms": 300, "netem": ""}
EVENT_KINDS = ("set", "outage", "switch")

DRY = False
TCLOG = None
LOG_T0 = time.monotonic()


# ----------------------------------------------------------------------------- helpers

def run(cmd, check=True, log=False):
    """Run a command (or just print it with --dry-run). Returns CompletedProcess."""
    cmd = [str(c) for c in cmd]
    line = " ".join(shlex.quote(c) for c in cmd)
    if log and TCLOG:
        TCLOG.write(f"{time.monotonic() - LOG_T0:9.3f} {line}\n")
        TCLOG.flush()
    if DRY:
        print("[dry-run]", line)
        return subprocess.CompletedProcess(cmd, 0, "", "")
    p = subprocess.run(cmd, capture_output=True, text=True)
    if check and p.returncode != 0:
        raise SystemExit(f"command failed ({p.returncode}): {line}\n{p.stderr.strip()}")
    return p


def need_root():
    if not DRY and os.geteuid() != 0:
        raise SystemExit("satlink must run as root (use sudo)")


_RATE = {"bit": 1, "kbit": 1e3, "mbit": 1e6, "gbit": 1e9}
_TIME = {"us": 1e-6, "ms": 1e-3, "s": 1.0}
_SIZE = {"": 1, "k": 1024, "m": 1024**2, "g": 1024**3}


def parse_rate(s):
    """'300kbit' -> bits per second (tc units: kbit = 1000 bit)."""
    m = re.fullmatch(r"([\d.]+)\s*(bit|kbit|mbit|gbit)", str(s).strip().lower())
    if not m:
        raise ValueError(f"bad rate {s!r}; use e.g. 300kbit, 50mbit, 1gbit")
    return int(float(m[1]) * _RATE[m[2]])


def parse_time(s):
    """'25ms' -> seconds."""
    if str(s).strip() == "0":
        return 0.0
    m = re.fullmatch(r"([\d.]+)\s*(us|ms|s)", str(s).strip().lower())
    if not m:
        raise ValueError(f"bad time {s!r}; use e.g. 500us, 25ms, 1s")
    return float(m[1]) * _TIME[m[2]]


def parse_size(s):
    """'500M' -> bytes (binary units)."""
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([kmg]?)b?", str(s).strip().lower())
    if not m:
        raise ValueError(f"bad size {s!r}; use e.g. 500M, 2G")
    return int(float(m[1]) * _SIZE[m[2]])


# ----------------------------------------------------------------------------- scenarios

def deep_merge(base, over):
    out = dict(base)
    for k, v in over.items():
        out[k] = deep_merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def load_scenario(target):
    """target: path to a scenario .toml, or a preset name from presets/."""
    p = Path(target)
    if p.is_file():
        scen, name = tomllib.loads(p.read_text()), p.stem
    else:
        scen, name = {"preset": target}, str(target)
    if "preset" in scen:
        pp = Path(scen["preset"])
        if not pp.is_file():
            pp = ROOT / "presets" / f"{scen['preset']}.toml"
        if not pp.is_file():
            raise SystemExit(f"unknown preset or scenario: {scen['preset']}")
        scen = deep_merge(tomllib.loads(pp.read_text()), {k: v for k, v in scen.items() if k != "preset"})
    scen["name"] = name
    try:
        validate_scenario(scen)
    except (ValueError, KeyError) as e:
        raise SystemExit(f"invalid scenario {name}: {e}")
    return scen


def validate_scenario(cfg):
    for sec in SECTION.values():
        d = cfg.get(sec, {})
        bad = set(d) - set(DEFAULT_DIR)
        if bad:
            raise ValueError(f"[{sec}] unknown keys {sorted(bad)}; allowed {sorted(DEFAULT_DIR)}")
        d = {**DEFAULT_DIR, **d}
        parse_rate(d["rate"]), parse_time(d["delay"]), parse_time(d["jitter"]), float(d["queue_ms"])
    parse_time(cfg.get("isl", {}).get("delay", "0ms"))
    for ev in cfg.get("event", []):
        if "t" not in ev:
            raise ValueError(f"event without t: {ev}")
        kinds = [k for k in EVENT_KINDS if k in ev]
        if len(kinds) != 1 or set(ev) - {"t", *EVENT_KINDS}:
            raise ValueError(f"event needs t and exactly one of {EVENT_KINDS}: {ev}")
        if "outage" in ev:
            if ev["outage"].get("dir", "both") not in ("up", "down", "both"):
                raise ValueError(f"outage dir must be up|down|both: {ev}")
            float(ev["outage"]["duration"])


# ----------------------------------------------------------------------------- link model

class Link:
    """Desired and applied tc state for both directions."""

    def __init__(self, cfg, devs):
        self.devs = devs
        self.cfg = {d: {**DEFAULT_DIR, **cfg.get(SECTION[d], {})} for d in DIRS}
        self.isl = parse_time(cfg.get("isl", {}).get("delay", "0ms"))
        self.mode = {d: "up" for d in DIRS}  # up | outage (chain flushed) | gap (switch blackout)
        self.applied = {d: None for d in DIRS}
        self.lock = threading.RLock()

    # -- desired state
    def rate(self, d):
        return parse_rate(self.cfg[d]["rate"])

    def delay(self, d):
        return parse_time(self.cfg[d]["delay"]) + self.isl

    def netem_args(self, d, blackhole=False):
        c = self.cfg[d]
        delay, jitter = self.delay(d), parse_time(c["jitter"])
        # netem sits in front of the bottleneck, so it holds everything in flight for `delay`.
        # Size the limit for a 10 Gbit/s offered load so overload drops happen in tbf, not here.
        limit = min(max(int(1.25e6 * (delay + jitter)) + 10000, 10000), 2_000_000)
        a = ["limit", limit, "delay", f"{round(delay * 1e6)}us"]
        if jitter:
            a.append(f"{round(jitter * 1e6)}us")
        a += ["loss", "100%"] if blackhole else shlex.split(c["netem"])
        return [str(x) for x in a]

    def tbf_args(self, d):
        rate = self.rate(d)
        burst = max(4500, rate // 8 // 250 + 3028)  # >= rate/HZ plus headroom for 2 MTUs
        return ["rate", f"{rate}bit", "burst", str(burst), "latency", f"{float(self.cfg[d]['queue_ms']):g}ms"]

    # -- tc operations
    def _tc(self, *a):
        run(["tc", *a], log=True)

    def build(self, d):
        dev = self.devs[d]
        run(["tc", "qdisc", "del", "dev", dev, "root"], check=False, log=True)
        n, t = self.netem_args(d), self.tbf_args(d)
        self._tc("qdisc", "add", "dev", dev, "root", "handle", "1:", "netem", *n)
        self._tc("qdisc", "add", "dev", dev, "parent", "1:1", "handle", "10:", "tbf", *t)
        self.applied[d] = {"netem": n, "tbf": t}
        self.mode[d] = "up"

    def sync(self, d):
        """Bring the kernel in line with the desired state, changing only what differs."""
        with self.lock:
            if self.mode[d] != "up":
                return
            a = self.applied[d]
            if a is None:
                return self.build(d)
            dev, n, t = self.devs[d], self.netem_args(d), self.tbf_args(d)
            if a["netem"] != n:
                self._tc("qdisc", "change", "dev", dev, "root", "handle", "1:", "netem", *n)
            if a["tbf"] != t:
                self._tc("qdisc", "change", "dev", dev, "parent", "1:1", "handle", "10:", "tbf", *t)
            self.applied[d] = {"netem": n, "tbf": t}

    def apply_all(self):
        for d in DIRS:
            self.build(d)

    def clear(self):
        for d in DIRS:
            run(["tc", "qdisc", "del", "dev", self.devs[d], "root"], check=False, log=True)

    def outage_start(self, dirs):
        """Silent blackhole. Deleting the root qdisc flushes queued and in-flight packets."""
        with self.lock:
            for d in dirs:
                dev = self.devs[d]
                run(["tc", "qdisc", "del", "dev", dev, "root"], check=False, log=True)
                self._tc("qdisc", "add", "dev", dev, "root", "handle", "1:", "netem", "loss", "100%")
                self.applied[d] = None
                self.mode[d] = "outage"

    def outage_end(self, dirs):
        with self.lock:
            for d in dirs:
                self.build(d)

    def set_many(self, changes):
        """changes: {"downlink.rate": "20mbit", "isl.delay": "12ms", ...}"""
        with self.lock:
            for key, val in changes.items():
                sec, field = key.split(".", 1)
                if sec == "isl" and field == "delay":
                    self.isl = parse_time(val)
                elif sec in DIR_OF and field in DEFAULT_DIR:
                    self.cfg[DIR_OF[sec]][field] = val
                else:
                    raise ValueError(f"cannot set {key!r}")
            validate_scenario({SECTION[d]: self.cfg[d] for d in DIRS} | {"isl": {"delay": f"{self.isl}s"}})
            for d in DIRS:
                self.sync(d)

    def switch_begin(self, spec):
        """Main-satellite switch: new ISL path and downlink/uplink values; optional blackout."""
        with self.lock:
            ch = {}
            if "isl_delay" in spec:
                ch["isl.delay"] = spec["isl_delay"]
            for sec in SECTION.values():
                for k, v in spec.get(sec, {}).items():
                    ch[f"{sec}.{k}"] = v
            gap = float(spec.get("gap_ms", 0))
            if gap <= 0:
                return self.set_many(ch)
            for d in DIRS:  # blackout: drop new arrivals, keep packets already in flight
                if self.mode[d] == "up":
                    n = self.netem_args(d, blackhole=True)
                    self._tc("qdisc", "change", "dev", self.devs[d], "root", "handle", "1:", "netem", *n)
                    self.applied[d]["netem"] = n
                    self.mode[d] = "gap"
            self.set_many(ch)  # recorded now, applied when the gap ends

    def switch_end(self):
        with self.lock:
            for d in DIRS:
                if self.mode[d] == "gap":
                    self.mode[d] = "up"
                    self.sync(d)


# ----------------------------------------------------------------------------- timeline

def build_actions(events, link):
    """Expand scenario events into (time, kind, description, callable)."""
    acts = []
    for ev in events:
        t = float(ev["t"])
        if "set" in ev:
            acts.append((t, "set", json.dumps(ev["set"]), functools.partial(link.set_many, ev["set"])))
        elif "outage" in ev:
            o = ev["outage"]
            dirs = DIRS if o.get("dir", "both") == "both" else (o["dir"],)
            tag = f"{o.get('dir', 'both')} {o['duration']}s"
            acts.append((t, "outage_start", tag, functools.partial(link.outage_start, dirs)))
            acts.append((t + float(o["duration"]), "outage_end", tag, functools.partial(link.outage_end, dirs)))
        else:
            s = ev["switch"]
            acts.append((t, "switch", json.dumps(s), functools.partial(link.switch_begin, s)))
            gap = float(s.get("gap_ms", 0))
            if gap > 0:
                acts.append((t + gap / 1000, "switch_end", "", link.switch_end))
    return sorted(acts, key=lambda a: a[0])


def run_timeline(acts, t0, stop, marks):
    for t, kind, desc, fn in acts:
        if stop.wait(max(0.0, t0 + t - time.monotonic())):
            return
        fn()
        marks.append({"t": round(time.monotonic() - t0, 3), "kind": kind, "desc": desc})
        print(f"[t={marks[-1]['t']:8.1f}s] {kind} {desc}", flush=True)


# ----------------------------------------------------------------------------- metrics

class Sampler(threading.Thread):
    """Every `interval` seconds: tc -s counters per direction -> timeseries.csv."""

    HEADER = ["t", "dir", "mbps", "delivered_bytes", "drops", "backlog_bytes", "cfg_rate_mbps", "cfg_delay_ms", "mode"]

    def __init__(self, link, t0, path, interval=1.0):
        super().__init__(daemon=True)
        self.link, self.t0, self.path, self.interval = link, t0, path, interval
        self.halt = threading.Event()

    def read(self, d):
        p = run(["tc", "-s", "-j", "qdisc", "show", "dev", self.link.devs[d]], check=False)
        try:
            qs = json.loads(p.stdout)
        except ValueError:
            return None
        return {q["kind"]: (q["bytes"], q["drops"], q["backlog"]) for q in qs if q.get("kind") in ("tbf", "netem")}

    def run(self):
        prev, last, nxt = {}, self.t0, self.t0 + self.interval
        with open(self.path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(self.HEADER)
            while not self.halt.wait(max(0.0, nxt - time.monotonic())):
                now = time.monotonic()
                dt, last, nxt = now - last, now, nxt + self.interval
                for d in DIRS:
                    st = self.read(d)
                    if st is None:
                        continue
                    delta = {}
                    for kind in ("tbf", "netem"):
                        cur = st.get(kind, (0, 0, 0))
                        old = prev.get((d, kind), (0, 0, 0))
                        # counters restart when a chain is rebuilt (outage end)
                        delta[kind] = [c - o if c >= o else c for c, o in zip(cur[:2], old[:2])]
                        prev[(d, kind)] = cur
                    deliv = delta["tbf"][0]
                    w.writerow([f"{now - self.t0:.2f}", d, f"{deliv * 8 / dt / 1e6:.3f}", deliv,
                                delta["tbf"][1] + delta["netem"][1], st.get("tbf", (0, 0, 0))[2],
                                f"{self.link.rate(d) / 1e6:.3f}", f"{self.link.delay(d) * 1e3:.1f}", self.link.mode[d]])
                f.flush()


def make_plot(outdir, marks, title):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("matplotlib not available; skipping plot (timeseries.csv is still written)")
        return
    rows = list(csv.DictReader(open(outdir / "timeseries.csv")))
    if not rows:
        print("no samples collected; skipping plot")
        return
    fig, axes = plt.subplots(2, 1, sharex=True, figsize=(11, 7))
    names = {"down": "Downlink (satellite -> ground)", "up": "Uplink (ground -> satellite)"}
    for ax, d in zip(axes, DIRS):
        r = [x for x in rows if x["dir"] == d]
        t = [float(x["t"]) for x in r]
        ax.step(t, [float(x["cfg_rate_mbps"]) for x in r], where="post", color="0.55", label="configured rate")
        ax.plot(t, [float(x["mbps"]) for x in r], color="tab:blue" if d == "down" else "tab:orange", label="delivered")
        start = None
        for x in r + [None]:
            out = x is not None and x["mode"] == "outage"
            if out and start is None:
                start = float(x["t"])
            elif not out and start is not None:
                ax.axvspan(start - 1, float(x["t"]) if x else t[-1], color="tab:red", alpha=0.15)
                start = None
        for m in marks:
            if m["kind"] in ("set", "switch"):
                ax.axvline(m["t"], color="k", ls=":", lw=0.8)
        ax.set_ylabel("Mbit/s")
        ax.set_title(names[d])
        ax.set_ylim(0, max(float(x["cfg_rate_mbps"]) for x in r) * 1.3)  # headroom for the legend
        ax.legend(loc="upper right")
    axes[1].set_xlabel("seconds since transfer start (red = outage, dotted = set/switch)")
    fig.suptitle(title)
    fig.tight_layout()
    fig.savefig(outdir / "plot.png", dpi=110)


# ----------------------------------------------------------------------------- docker / topology

def discover():
    """Return ({'down': veth, 'up': veth}, {role: {'pid', 'host'}}). Rediscovered on every command."""
    if DRY:
        info = {r: {"pid": 0, "host": f"veth-{r}"} for r in CONTAINER}
        return {d: info[RECEIVER[d]]["host"] for d in DIRS}, info
    host = {}
    for p in Path("/sys/class/net").iterdir():
        host[int((p / "ifindex").read_text())] = p.name
    info = {}
    for role, cname in CONTAINER.items():
        pid = int(run(["docker", "inspect", "-f", "{{.State.Pid}}", cname], check=False).stdout.strip() or 0)
        if pid <= 0:
            raise SystemExit(f"{cname} is not running; run 'satlink up' first")
        out = run(["nsenter", "-t", pid, "-n", "ip", "-o", "link", "show", "eth0"]).stdout
        m = re.search(r"@if(\d+)", out)  # peer ifindex = the host-side veth
        if not m or int(m[1]) not in host:
            raise SystemExit(f"cannot find host veth for {cname} (eth0: {out.strip()!r})")
        info[role] = {"pid": pid, "host": host[int(m[1])]}
    return {d: info[RECEIVER[d]]["host"] for d in DIRS}, info


def disable_offloads(info):
    """TSO/GSO/GRO off on both ends of each veth so netem sees real MTU-sized packets."""
    for role, i in info.items():
        off = ["tso", "off", "gso", "off", "gro", "off"]
        for where, cmd in (("host", ["ethtool", "-K", i["host"], *off]),
                           ("container", ["nsenter", "-t", i["pid"], "-n", "ethtool", "-K", "eth0", *off])):
            if run(cmd, check=False).returncode != 0:
                print(f"warning: could not disable offloads on {role} ({where} side)", file=sys.stderr)


def dexec(role, cmd, check=True):
    return run(["docker", "exec", CONTAINER[role], "sh", "-c", cmd], check=check)


# ----------------------------------------------------------------------------- app harness

def last_json_line(path):
    """Return the last line of a log file if it is a JSON object, else None."""
    try:
        lines = Path(path).read_text(errors="replace").strip().splitlines()
        obj = json.loads(lines[-1]) if lines else None
    except (OSError, ValueError):
        return None
    return obj if isinstance(obj, dict) else None


class App:
    """Runs the user's send/receive commands inside the containers and verifies the file."""

    def __init__(self, a, outdir):
        self.a, self.outdir = a, outdir
        self.sender = a.get("sender", "satellite")
        if self.sender not in CONTAINER:
            raise SystemExit("[app] sender must be 'satellite' or 'ground'")
        self.recv = "ground" if self.sender == "satellite" else "satellite"
        self.size = parse_size(a["file_size"]) if "file_size" in a else None
        self.timeout = int(a.get("timeout", 3600))
        self.verify = a.get("verify", self.size is not None)
        self.vars = {"file": a.get("send_file", "/data/payload.bin"), "out": a.get("recv_file", "/data/received.bin"),
                     "peer": IP[self.recv], "self": IP[self.sender], "size": self.size or 0}
        self.recv_proc = None
        self.rc = self.duration = None

    def sub(self, cmd):
        return re.sub(r"\{(\w+)\}", lambda m: str(self.vars.get(m[1], m[0])), cmd)

    def prepare(self):
        if self.size:
            print(f"generating {self.size} random bytes on {self.sender} ...", flush=True)
            dexec(self.sender, f"mkdir -p $(dirname {self.vars['file']}) && head -c {self.size} /dev/urandom > {self.vars['file']}")
        dexec(self.recv, f"rm -rf {self.vars['out']}; mkdir -p $(dirname {self.vars['out']})")
        cmd = (f"echo $$ > /tmp/satlink-recv.pid; exec timeout -s KILL {self.timeout + 120} "
               f"sh -c {shlex.quote(self.sub(self.a['recv_cmd']))}")
        self.recv_proc = self._popen(self.recv, cmd, "receiver.log")
        time.sleep(float(self.a.get("recv_settle", 2)))

    def _popen(self, role, cmd, logname):
        argv = ["docker", "exec", CONTAINER[role], "sh", "-c", cmd]
        if DRY:
            print("[dry-run]", " ".join(shlex.quote(x) for x in argv))
            return None
        return subprocess.Popen(argv, stdout=open(self.outdir / logname, "w"), stderr=subprocess.STDOUT)

    def run_sender(self):
        cmd = f"exec timeout -s KILL {self.timeout} sh -c {shlex.quote(self.sub(self.a['send_cmd']))}"
        t = time.monotonic()
        p = self._popen(self.sender, cmd, "sender.log")
        if p is None:
            return
        try:
            self.rc = p.wait(self.timeout + 30)
        except subprocess.TimeoutExpired:
            p.kill()
            self.rc = -1
        self.duration = time.monotonic() - t

    def finish(self):
        """Stop the receiver and verify the file. Called after the link has been cleared."""
        res = {"sender": self.sender, "sender_exit": self.rc, "timed_out": self.rc in (137, -1),
               "duration_s": round(self.duration, 2) if self.duration else None, "size_bytes": self.size}
        if self.recv_proc is not None:
            try:
                res["receiver_exit"] = self.recv_proc.wait(float(self.a.get("recv_grace", 5)))
            except subprocess.TimeoutExpired:
                dexec(self.recv, "kill -TERM $(cat /tmp/satlink-recv.pid)", check=False)
                try:
                    res["receiver_exit"] = self.recv_proc.wait(3)
                except subprocess.TimeoutExpired:
                    dexec(self.recv, "kill -KILL $(cat /tmp/satlink-recv.pid)", check=False)
                    res["receiver_exit"] = "killed"
        # Apps may end their log with one JSON object (e.g. cfdp_entity.py); keep it in the summary.
        for key, name in (("sender_report", "sender.log"), ("receiver_report", "receiver.log")):
            report = last_json_line(self.outdir / name)
            if report is not None:
                res[key] = report
        if self.verify and not DRY:
            sha = lambda role, f: (dexec(role, f"sha256sum {f}", check=False).stdout.split() or [""])[0]
            a, b = sha(self.sender, self.vars["file"]), sha(self.recv, self.vars["out"])
            res["sha256_match"] = bool(a) and a == b
            if res["sha256_match"] and self.duration:
                res["goodput_mbps"] = round(self.size * 8 / self.duration / 1e6, 3)
        return res


# ----------------------------------------------------------------------------- commands

def cmd_up(a):
    need_root()
    env = dict(os.environ)
    if a.image:
        env["SATLINK_IMAGE"] = a.image
    args = ["docker", "compose", "-f", str(COMPOSE), "up", "-d"] + ([] if a.image else ["--build"])
    if DRY:
        print("[dry-run]", " ".join(args))
    else:
        subprocess.run(args, env=env, check=True)
    devs, info = discover()
    disable_offloads(info)
    print(f"ready: downlink shaped on {devs['down']}, uplink shaped on {devs['up']}")


def cmd_down(a):
    need_root()
    try:
        cmd_clear(a)
    except SystemExit:
        pass
    run(["docker", "compose", "-f", COMPOSE, "down"], check=False)


def cmd_clear(a):
    need_root()
    devs, _ = discover()
    Link({}, devs).clear()
    STATE_FILE.unlink(missing_ok=True)
    print("qdiscs removed")


def cmd_apply(a):
    need_root()
    cfg = load_scenario(a.target)
    devs, info = discover()
    disable_offloads(info)
    if cfg.get("event"):
        print(f"note: {len(cfg['event'])} timeline event(s) ignored by 'apply'; use 'run'")
    Link(cfg, devs).apply_all()
    STATE_FILE.write_text(json.dumps(cfg))
    print(f"applied {cfg['name']}")


def cmd_outage(a):
    need_root()
    if not STATE_FILE.exists():
        raise SystemExit("no applied profile; run 'satlink apply <preset>' first")
    cfg = json.loads(STATE_FILE.read_text())
    devs, _ = discover()
    link = Link(cfg, devs)
    dirs = DIRS if a.dir == "both" else (a.dir,)
    link.outage_start(dirs)
    print(f"outage {a.dir} for {a.seconds}s")
    try:
        time.sleep(a.seconds)
    finally:
        link.outage_end(dirs)
        print("link restored")


def cmd_run(a):
    global TCLOG, LOG_T0
    need_root()
    cfg = load_scenario(a.scenario)
    devs, info = discover()
    disable_offloads(info)
    outdir = ROOT / "results" / f"{datetime.now():%Y%m%d-%H%M%S}-{cfg['name']}"
    outdir.mkdir(parents=True)
    TCLOG = open(outdir / "tc.log", "w")
    (outdir / "scenario.effective.json").write_text(json.dumps(cfg, indent=2))
    link, marks, stop = Link(cfg, devs), [], threading.Event()
    acts = build_actions(cfg.get("event", []), link)
    app = App(cfg["app"], outdir) if "app" in cfg else None
    result, sampler = {}, None
    link.apply_all()
    try:
        if app:
            app.prepare()
        t0 = LOG_T0 = time.monotonic()
        sampler = Sampler(link, t0, outdir / "timeseries.csv")
        sampler.start()
        threading.Thread(target=run_timeline, args=(acts, t0, stop, marks), daemon=True).start()
        if app:
            app.run_sender()
        else:
            horizon = max([t for t, *_ in acts], default=0)
            stop.wait(float(cfg.get("run", {}).get("duration", horizon + 5)))
    finally:
        stop.set()
        if sampler:
            sampler.halt.set()
            sampler.join(3)
        link.clear()
        if app:
            result = app.finish() or {}
        TCLOG.close()
    result.update({"scenario": cfg["name"], "events": marks})
    (outdir / "summary.json").write_text(json.dumps(result, indent=2))
    if sampler and (outdir / "timeseries.csv").exists():
        make_plot(outdir, marks, cfg["name"])
    brief = {k: v for k, v in result.items() if k != "events"}
    print(json.dumps(brief, indent=2))
    print(f"results: {outdir}")
    if app and (result.get("sender_exit") != 0 or result.get("sha256_match") is False):
        sys.exit(1)


def cmd_validate(a):
    """Apply each preset and check that delay and rate are really delivered, both directions."""
    need_root()
    devs, info = discover()
    disable_offloads(info)
    failures = 0
    print(f"{'preset':<12} {'check':<14} {'expected':>14} {'measured':>14}  result")
    for name in a.presets or ["leo", "narrowband", "gigabit"]:
        cfg = load_scenario(name)
        link = Link(cfg, devs)
        link.apply_all()
        results = []
        # RTT: satellite -> ground pays the downlink, the echo pays the uplink (+ ISL both ways)
        exp = link.delay("down") + link.delay("up") + 84 * 8 / link.rate("down") + 84 * 8 / link.rate("up")
        out = dexec("satellite", f"ping -c 5 -i 0.3 -q {IP['ground']}", check=False).stdout
        m = re.search(r"= [\d.]+/([\d.]+)/", out)
        rtt = float(m[1]) / 1e3 if m else None
        ok = rtt is not None and abs(rtt - exp) <= max(0.003, 0.1 * exp)
        results.append(("rtt", f"{exp * 1e3:.1f} ms", f"{rtt * 1e3:.1f} ms" if rtt else "n/a", ok))
        for d, sender in (("down", "satellite"), ("up", "ground")):
            recv, rate = RECEIVER[d], link.rate(d)
            dexec(recv, "pkill iperf3; rm -f /tmp/srv.json", check=False)
            run(["docker", "exec", "-d", CONTAINER[recv], "sh", "-c", "iperf3 -4 -s -1 -J > /tmp/srv.json 2>/dev/null"])
            time.sleep(1)
            dexec(sender, f"iperf3 -c {IP[recv]} -u -b {int(rate * 1.2)} -l 1460 -t 8 >/dev/null", check=False)
            time.sleep(1.5)
            try:
                end = json.loads(dexec(recv, "cat /tmp/srv.json", check=False).stdout)["end"]
                got = (end.get("sum_received") or end["sum"])["bits_per_second"]  # sum_received: iperf3 >= 3.7
            except (ValueError, KeyError):
                got = None
            # payload is ~97% of link rate (headers); tbf's initial burst credit inflates very low rates
            ok = got is not None and 0.88 * rate <= got <= 1.08 * rate
            results.append((f"{d} rate", f"{rate / 1e6:.2f} Mbit/s", f"{got / 1e6:.2f} Mbit/s" if got else "n/a", ok))
        for check, e, g, ok in results:
            print(f"{name:<12} {check:<14} {e:>14} {g:>14}  {'ok' if ok else 'FAIL'}")
            failures += not ok
    link.clear()
    sys.exit(1 if failures else 0)


def main():
    global DRY
    ap = argparse.ArgumentParser(prog="satlink", description=__doc__.split("\n")[0])
    ap.add_argument("--dry-run", action="store_true", help="print commands instead of running them")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("up", help="start containers, discover veths, disable offloads")
    p.add_argument("--image", help="use this image instead of building docker/endpoint")
    sub.add_parser("down", help="clear qdiscs and stop containers")
    sub.add_parser("clear", help="remove all qdiscs")
    p = sub.add_parser("apply", help="apply a static profile (preset name or scenario file)")
    p.add_argument("target")
    p = sub.add_parser("outage", help="blackhole the applied profile for N seconds")
    p.add_argument("seconds", type=float)
    p.add_argument("--dir", choices=("up", "down", "both"), default="both")
    p = sub.add_parser("run", help="run a scenario: timeline, transfer, results")
    p.add_argument("scenario")
    p = sub.add_parser("validate", help="check that delay and rate are delivered")
    p.add_argument("presets", nargs="*")
    a = ap.parse_args()
    DRY = a.dry_run
    globals()[f"cmd_{a.cmd}"](a)


if __name__ == "__main__":
    main()
