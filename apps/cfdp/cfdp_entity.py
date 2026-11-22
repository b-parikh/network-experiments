#!/usr/bin/env python3
"""CFDP sender and receiver (CCSDS 727.0-B) over UDP for satlink tests.

Built on cfdp-py (import name `cfdppy`). One file per run, Class 1 (unacknowledged) or
Class 2 (acknowledged, NAK-based retransmission). CFDP has no congestion control, so the sender
is paced to --rate; set it at or below the link rate.

  cfdp_entity.py recv --local-id 2 --remote-id 1 --out-dir /data/recv --once
  cfdp_entity.py send /data/payload.bin payload.bin --local-id 1 --remote-id 2 \\
                      --remote 172.30.0.3:4444 --class 2 --rate 150mbit

stdout carries one JSON report line, printed last. Logs go to stderr.
Exit codes: 0 success, 2 CFDP fault or incomplete delivery, 3 timeout, 1 usage or other error.
"""
import argparse
import collections
import json
import logging
import re
import select
import signal
import socket
import sys
import time
from datetime import timedelta
from pathlib import Path

from cfdppy import CfdpState, PacketDestination, PutRequest, get_packet_destination
from cfdppy.handler.dest import DestHandler
from cfdppy.handler.source import SourceHandler
from cfdppy.mib import (
    CheckTimerProvider,
    DefaultFaultHandlerBase,
    IndicationConfig,
    LocalEntityConfig,
    RemoteEntityConfig,
    RemoteEntityConfigTable,
)
from cfdppy.restricted_filestore import RestrictedFilestore
from cfdppy.user import CfdpUserBase
from spacepackets.cfdp import (
    ChecksumType,
    ConditionCode,
    DeliveryCode,
    DirectiveType,
    FaultHandlerCode,
    PduType,
    TransmissionMode,
)
from spacepackets.cfdp.pdu import PduFactory
from spacepackets.countdown import Countdown
from spacepackets.seqcount import SeqCountProvider
from spacepackets.util import ByteFieldU16

LOG = logging.getLogger("cfdp")
WIRE_OVERHEAD = 14 + 20 + 8  # Ethernet + IPv4 + UDP: pacing counts what the link counts
MAX_UDP_PAYLOAD = 1500 - 20 - 8
UNSENT_LIMIT = 32  # PDUs generated ahead of the pacer before the state machine is paused


def parse_rate(s):
    """'150mbit' or a plain number of bit/s -> int bit/s. 0 disables pacing."""
    m = re.fullmatch(r"([\d.]+)\s*(bit|kbit|mbit|gbit)?", str(s).strip().lower())
    if not m:
        raise argparse.ArgumentTypeError(f"bad rate {s!r}; use e.g. 300kbit, 150mbit, 1gbit")
    return int(float(m[1]) * {None: 1, "bit": 1, "kbit": 1e3, "mbit": 1e6, "gbit": 1e9}[m[2]])


def parse_addr(s):
    host, _, port = s.rpartition(":")
    if not host or not port.isdigit():
        raise argparse.ArgumentTypeError(f"bad address {s!r}; use HOST:PORT")
    return host, int(port)


class Pacer:
    """Leaky bucket on the wire rate, with a few ms of idle credit so timer jitter averages out."""

    def __init__(self, rate_bps, burst_s=0.004):
        self.rate, self.burst, self.free_at = rate_bps, burst_s, 0.0

    def wait_time(self):
        return max(0.0, self.free_at - time.monotonic()) if self.rate else 0.0

    def sent(self, payload_len):
        if self.rate:
            self.free_at = max(self.free_at, time.monotonic() - self.burst) + (payload_len + WIRE_OVERHEAD) * 8 / self.rate


class FixedCheckTimer(CheckTimerProvider):
    def __init__(self, seconds):
        self.seconds = seconds

    def provide_check_timer(self, local_entity_id, remote_entity_id, entity_type):
        return Countdown(timedelta(seconds=self.seconds))


class Faults(DefaultFaultHandlerBase):
    """Records faults. A checksum failure cancels (the library default ignores it), so a lossy
    Class 1 transfer is reported as a failure instead of a silent success."""

    def __init__(self):
        super().__init__()
        self.events = []
        self.set_handler(ConditionCode.FILE_CHECKSUM_FAILURE, FaultHandlerCode.NOTICE_OF_CANCELLATION)

    def _note(self, kind, cond, progress):
        LOG.warning("fault: %s %s at %d bytes", kind, cond.name, progress)
        self.events.append({"action": kind, "condition": cond.name, "progress": progress})

    def notice_of_suspension_cb(self, tid, cond, progress):
        self._note("suspension", cond, progress)

    def notice_of_cancellation_cb(self, tid, cond, progress):
        self._note("cancellation", cond, progress)

    def abandoned_cb(self, tid, cond, progress):
        self._note("abandoned", cond, progress)

    def ignore_cb(self, tid, cond, progress):
        self._note("ignored", cond, progress)


class Recorder(CfdpUserBase):
    """CFDP user: remembers the indications we report on."""

    def __init__(self, vfs):
        super().__init__(vfs)
        self.finished = None  # FinishedParams
        self.eof_sent = False
        self.transaction_seen = False
        self.file_size = None
        self.indicated_faults = []

    def transaction_indication(self, params):
        self.transaction_seen = True

    def eof_sent_indication(self, transaction_id):
        self.eof_sent = True

    def transaction_finished_indication(self, params):
        self.transaction_seen = True
        self.finished = params.finished_params

    def metadata_recv_indication(self, params):
        self.transaction_seen = True
        self.file_size = params.file_size

    def fault_indication(self, transaction_id, cond_code, progress):
        self.indicated_faults.append(cond_code.name)

    def abandoned_indication(self, transaction_id, cond_code, progress):
        self.indicated_faults.append(f"abandoned:{cond_code.name}")

    def file_segment_recv_indication(self, params):
        pass

    def report_indication(self, *args):
        pass

    def suspended_indication(self, *args):
        pass

    def resumed_indication(self, *args):
        pass

    def eof_recv_indication(self, transaction_id):
        pass


def pdu_name(holder):
    if holder.pdu_type == PduType.FILE_DATA:
        return "FILE_DATA"
    d = holder.pdu_directive_type
    return DirectiveType(d).name if d is not None else "UNKNOWN"


def build_entity(args, role):
    """Create the source or destination handler with the CLI's timer settings."""
    remote_id, local_id = ByteFieldU16(args.remote_id), ByteFieldU16(args.local_id)
    cls2 = role == "send" and args.cls == 2
    closure = cls2 or getattr(args, "closure", False)
    remote_cfg = RemoteEntityConfig(
        entity_id=remote_id,
        max_packet_len=MAX_UDP_PAYLOAD,
        max_file_segment_len=args.segment,
        closure_requested=closure,
        crc_on_transmission=False,
        default_transmission_mode=TransmissionMode.ACKNOWLEDGED if cls2 else TransmissionMode.UNACKNOWLEDGED,
        crc_type=ChecksumType.CRC_32,
        positive_ack_timer_interval_seconds=args.ack_interval,
        positive_ack_timer_expiration_limit=args.ack_limit,
        nak_timer_interval_seconds=args.nak_interval,
        nak_timer_expiration_limit=args.nak_limit,
        check_limit=args.check_limit,
    )
    table = RemoteEntityConfigTable()
    table.add_config(remote_cfg)
    faults = Faults()
    vfs = RestrictedFilestore(Path(args.out_dir).resolve()) if role == "recv" else None
    user = Recorder(vfs)
    cfg = LocalEntityConfig(local_id, IndicationConfig(), faults)
    timer = FixedCheckTimer(args.check_interval)
    if role == "send":
        handler = SourceHandler(cfg=cfg, user=user, remote_cfg_table=table, check_timer_provider=timer,
                                seq_num_provider=SeqCountProvider(16))
    else:
        handler = DestHandler(cfg=cfg, user=user, remote_cfg_table=table, check_timer_provider=timer)
    return handler, user, faults


def run_loop(args, role, handler, user, pacer):
    """Single-threaded loop: receive, feed the state machine, send paced. Returns (outcome, stats)."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 << 20)
    sock.bind(("0.0.0.0", args.port))
    sock.setblocking(False)
    mine = PacketDestination.SOURCE_HANDLER if role == "send" else PacketDestination.DEST_HANDLER
    peer = args.remote
    outq = collections.deque()
    st = {"sent": collections.Counter(), "recv": collections.Counter(), "wire_bytes_sent": 0,
          "file_data_bytes_sent": 0, "retransmitted_bytes": 0}
    high = 0  # highest file offset sent so far, to spot retransmissions
    stop = []
    signal.signal(signal.SIGTERM, lambda *_: stop.append("terminated"))
    signal.signal(signal.SIGINT, lambda *_: stop.append("terminated"))
    started = time.monotonic()
    outcome = "running"

    def pump(packet):
        nonlocal high
        try:
            res = handler.state_machine(packet)
        except Exception as e:  # stray or late PDUs must not kill the entity
            LOG.debug("state machine rejected %s: %r", type(packet).__name__, e)
            return 0
        n = 0
        while res.states.num_packets_ready > 0:
            h = handler.get_next_packet()
            outq.append(h)
            n += 1
        return n

    while True:
        now = time.monotonic()
        if stop:
            outcome = stop[0]
            break
        if args.timeout and now - started > args.timeout:
            outcome = "timeout"
            break
        # Receive everything waiting.
        while True:
            try:
                data, addr = sock.recvfrom(65535)
            except BlockingIOError:
                break
            if peer is None:
                peer = addr  # receiver replies to whoever sent to it
            try:
                holder = PduFactory.from_raw_to_holder(data)
                dest = get_packet_destination(holder.pdu)
            except Exception as e:
                LOG.debug("undecodable PDU dropped: %r", e)
                continue
            st["recv"][pdu_name(holder)] += 1
            if dest == mine:
                pump(holder.pdu)
        # Send what the pacer allows.
        while outq and pacer.wait_time() == 0:
            h = outq.popleft()
            data = h.pack()
            sock.sendto(data, peer)
            pacer.sent(len(data))
            name = pdu_name(h)
            st["sent"][name] += 1
            st["wire_bytes_sent"] += len(data) + WIRE_OVERHEAD
            if name == "FILE_DATA":
                fd = h.to_file_data_pdu()
                n = len(fd.file_data)
                st["file_data_bytes_sent"] += n
                if fd.offset < high:
                    st["retransmitted_bytes"] += n
                high = max(high, fd.offset + n)
        # Let the state machine run (timers, next file segment) unless the pacer is the bottleneck.
        produced = pump(None) if len(outq) < UNSENT_LIMIT else 0
        idle = handler.state == CfdpState.IDLE
        if idle and not outq:
            if role == "send" and (user.transaction_seen or user.finished or user.eof_sent):
                outcome = "done"
                break
            if role == "recv" and args.once and user.transaction_seen:
                outcome = "done"
                break
        if produced:
            continue
        wait = pacer.wait_time() if outq else 0.05
        select.select([sock], [], [], min(wait, 0.05) if wait else 0)
    # Give final PDUs (Finished, ACK, cancel EOF) a moment to leave.
    deadline = time.monotonic() + 2
    while outq and time.monotonic() < deadline:
        if pacer.wait_time() == 0:
            h = outq.popleft()
            data = h.pack()
            sock.sendto(data, peer)
            pacer.sent(len(data))
            st["sent"][pdu_name(h)] += 1
            st["wire_bytes_sent"] += len(data) + WIRE_OVERHEAD
        else:
            time.sleep(pacer.wait_time())
    sock.close()
    return outcome, st


def make_report(args, role, outcome, st, user, faults, started, extra):
    fin = user.finished
    finished_condition = fin.condition_code.name if fin is not None else None
    # Own first fault wins: a sender that gave up reports its fault, not the peer's Finished code.
    condition = faults.events[0]["condition"] if faults.events else (finished_condition or "NO_ERROR")
    delivered = fin is not None and fin.condition_code == ConditionCode.NO_ERROR and fin.delivery_code == DeliveryCode.DATA_COMPLETE
    if role == "send" and args.cls == 1 and not args.closure:
        ok = outcome == "done" and user.eof_sent and not faults.events  # best effort: success means EOF sent
    else:
        ok = outcome == "done" and delivered and not faults.events
    rep = {"role": role, "class": args.cls if role == "send" else None, "ok": ok, "outcome": outcome,
           "condition": condition, "finished_condition": finished_condition, "delivery": fin.delivery_code.name if fin is not None else None,
           "duration_s": round(time.monotonic() - started, 3), "pdus_sent": dict(st["sent"]), "pdus_recv": dict(st["recv"]),
           "wire_bytes_sent": st["wire_bytes_sent"], "file_data_bytes_sent": st["file_data_bytes_sent"],
           "retransmitted_bytes": st["retransmitted_bytes"], "faults": faults.events + user.indicated_faults}
    rep.update(extra)
    return rep


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter,
                                 epilog="\n".join(__doc__.split("\n")[2:]))
    sub = ap.add_subparsers(dest="role", required=True)
    for name in ("send", "recv"):
        p = sub.add_parser(name)
        p.add_argument("--local-id", type=int, required=True)
        p.add_argument("--remote-id", type=int, required=True)
        p.add_argument("--port", type=int, default=4444, help="local UDP port (default 4444)")
        p.add_argument("--remote", type=parse_addr, help="peer HOST:PORT (receiver default: reply to the sender)")
        p.add_argument("--rate", type=parse_rate, default=0, help="pace transmission to this wire rate; 0 = unpaced")
        p.add_argument("--segment", type=int, default=1400, help="file bytes per PDU (default 1400)")
        p.add_argument("--timeout", type=float, default=0, help="give up after N seconds (exit 3); 0 = never")
        p.add_argument("--ack-interval", type=float, default=10.0, help="seconds to wait for an ACK (default 10)")
        p.add_argument("--ack-limit", type=int, default=2, help="ACK timer expirations before a fault (default 2)")
        p.add_argument("--nak-interval", type=float, default=10.0, help="seconds between repeated NAKs (default 10)")
        p.add_argument("--nak-limit", type=int, default=2, help="NAK timer expirations before a fault (default 2)")
        p.add_argument("--check-interval", type=float, default=5.0, help="Class 1 check timer seconds (default 5)")
        p.add_argument("--check-limit", type=int, default=2, help="check timer expirations before a fault (default 2)")
        p.add_argument("-v", "--verbose", action="count", default=0)
        if name == "send":
            p.add_argument("file", help="source file")
            p.add_argument("dest", help="destination path, relative to the receiver's --out-dir")
            p.add_argument("--class", dest="cls", type=int, choices=(1, 2), default=2)
            p.add_argument("--closure", action="store_true", help="Class 1: request a Finished PDU from the receiver")
        else:
            p.add_argument("--out-dir", required=True)
            p.add_argument("--once", action="store_true", help="exit after one transaction")
    args = ap.parse_args()
    logging.basicConfig(stream=sys.stderr, level=[logging.WARNING, logging.INFO, logging.DEBUG][min(args.verbose, 2)],
                        format="%(asctime)s %(name)s %(message)s")
    if not 64 <= args.segment <= MAX_UDP_PAYLOAD - 40:
        ap.error(f"--segment must be between 64 and {MAX_UDP_PAYLOAD - 40} so a PDU fits one unfragmented UDP datagram")
    if args.role == "send" and args.remote is None:
        ap.error("send needs --remote HOST:PORT")
    if args.role == "recv":
        Path(args.out_dir).mkdir(parents=True, exist_ok=True)
    handler, user, faults = build_entity(args, args.role)
    extra = {}
    started = time.monotonic()
    if args.role == "send":
        src = Path(args.file)
        if not src.is_file():
            print(json.dumps({"role": "send", "ok": False, "outcome": "error", "error": f"no such file: {src}"}))
            return 1
        extra["file_bytes"] = src.stat().st_size
        req = PutRequest(destination_id=ByteFieldU16(args.remote_id), source_file=src, dest_file=Path(args.dest),
                         trans_mode=TransmissionMode.ACKNOWLEDGED if args.cls == 2 else TransmissionMode.UNACKNOWLEDGED,
                         closure_requested=args.cls == 2 or args.closure)
        if not handler.put_request(req):
            print(json.dumps({"role": "send", "ok": False, "outcome": "error", "error": "put request rejected"}))
            return 1
    outcome, st = run_loop(args, args.role, handler, user, Pacer(args.rate))
    if args.role == "send" and extra["file_bytes"] and outcome == "done":
        extra["goodput_mbps"] = round(extra["file_bytes"] * 8 / max(time.monotonic() - started, 1e-9) / 1e6, 3)
    if args.role == "recv":
        extra["file_size"] = user.file_size
    rep = make_report(args, args.role, outcome, st, user, faults, started, extra)
    print(json.dumps(rep), flush=True)
    return 0 if rep["ok"] else 3 if outcome == "timeout" else 2


if __name__ == "__main__":
    sys.exit(main())
