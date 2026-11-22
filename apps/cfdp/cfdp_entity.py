#!/usr/bin/env python3
"""CFDP (CCSDS 727.0-B) file sender/receiver over UDP, built on cfdp-py, for satlink tests.

  cfdp_entity.py send FILE DEST --local-id 1 --remote-id 2 --remote HOST:4444 --class 2 --rate 150mbit
  cfdp_entity.py recv --local-id 2 --remote-id 1 --out-dir /data --once

Class 1 = unacknowledged (no retransmission); class 2 = acknowledged (NAK-based retransmission).
CFDP has no congestion control, so --rate paces every outgoing PDU (retransmissions included).
The last line printed is a JSON report. Exit codes: 0 success, 2 CFDP fault or incomplete file,
3 local timeout or terminated before the transaction finished.
"""
import argparse
import json
import logging
import re
import select
import signal
import socket
import sys
import time
from pathlib import Path

from cfdppy import (CfdpState, CfdpUserBase, IndicationConfig, LocalEntityConfig, PutRequest,
                    RemoteEntityConfig, RemoteEntityConfigTable)
from cfdppy.filestore import NativeFilestore
from cfdppy.handler import DestHandler, SourceHandler
from cfdppy.mib import CheckTimerProvider, DefaultFaultHandlerBase
from cfdppy.restricted_filestore import RestrictedFilestore
from spacepackets.cfdp import ChecksumType, ConditionCode, DirectiveType, PduType, TransmissionMode
from spacepackets.cfdp.pdu import PduFactory
from spacepackets.countdown import Countdown
from spacepackets.seqcount import SeqCountProvider
from spacepackets.util import ByteFieldU16

WIRE_OVERHEAD = 14 + 20 + 8  # Ethernet + IPv4 + UDP, so --rate matches the link rate tc enforces
LOG = logging.getLogger("cfdp_entity")


def parse_rate(s):
    """'150mbit' -> bit/s; '0' means unpaced."""
    if str(s).strip() == "0":
        return 0
    m = re.fullmatch(r"([\d.]+)\s*(bit|kbit|mbit|gbit)", str(s).strip().lower())
    if not m:
        raise argparse.ArgumentTypeError(f"bad rate {s!r}; use e.g. 300kbit, 150mbit, 1gbit or 0")
    return int(float(m[1]) * {"bit": 1, "kbit": 1e3, "mbit": 1e6, "gbit": 1e9}[m[2]])


def parse_addr(s):
    host, _, port = s.rpartition(":")
    if not host or not port.isdigit():
        raise argparse.ArgumentTypeError(f"bad address {s!r}; use HOST:PORT")
    return host, int(port)


class Pacer:
    """Average-rate pacing with a 2 ms burst allowance (sleep only when ahead of schedule)."""

    def __init__(self, rate_bps, burst_s=0.002):
        self.rate, self.burst, self.t = rate_bps, burst_s, 0.0

    def wait(self, nbytes):
        if not self.rate:
            return
        now = time.monotonic()
        self.t = max(self.t, now - self.burst) + nbytes * 8 / self.rate
        if self.t - now > 0.0002:
            time.sleep(self.t - now)


class FaultHandler(DefaultFaultHandlerBase):
    """Records faults; the handlers perform the actual cancellation/abandonment themselves."""

    def __init__(self, events):
        super().__init__()
        self.events = events

    def _rec(self, kind, tid, cond, progress):
        LOG.warning("%s: %s at %d bytes", kind, cond.name, progress)
        self.events.append({"kind": kind, "condition": cond.name, "progress": progress})

    def notice_of_suspension_cb(self, tid, cond, progress):
        self._rec("suspension", tid, cond, progress)

    def notice_of_cancellation_cb(self, tid, cond, progress):
        self._rec("cancellation", tid, cond, progress)

    def abandoned_cb(self, tid, cond, progress):
        self._rec("abandoned", tid, cond, progress)

    def ignore_cb(self, tid, cond, progress):
        self._rec("ignored", tid, cond, progress)


class User(CfdpUserBase):
    """Collects indications for the final report."""

    def __init__(self, vfs, events):
        super().__init__(vfs)
        self.events, self.finished, self.file_size = events, None, None

    def transaction_indication(self, p):
        LOG.info("transaction %s", p.transaction_id)

    def eof_sent_indication(self, tid):
        LOG.info("EOF sent")

    def eof_recv_indication(self, tid):
        LOG.info("EOF received")

    def transaction_finished_indication(self, p):
        fp = p.finished_params
        self.finished = {"condition": fp.condition_code.name, "delivery_code": fp.delivery_code.name,
                         "file_status": fp.file_status.name}
        LOG.info("transaction finished: %s", self.finished)

    def metadata_recv_indication(self, p):
        self.file_size = p.file_size
        LOG.info("metadata: %s bytes -> %s", p.file_size, p.dest_file_name)

    def file_segment_recv_indication(self, p):
        pass

    def report_indication(self, tid, status_report):
        pass

    def suspended_indication(self, tid, cond_code):
        self.events.append({"kind": "suspended", "condition": cond_code.name})

    def resumed_indication(self, tid, progress):
        pass

    def fault_indication(self, tid, cond_code, progress):
        pass  # already recorded by FaultHandler

    def abandoned_indication(self, tid, cond_code, progress):
        pass


class CheckTimers(CheckTimerProvider):
    def __init__(self, seconds):
        self.seconds = seconds

    def provide_check_timer(self, local_entity_id, remote_entity_id, entity_type):
        return Countdown.from_seconds(self.seconds)


def build(args, vfs, events):
    mode = TransmissionMode.ACKNOWLEDGED if args.cls == 2 else TransmissionMode.UNACKNOWLEDGED
    local = LocalEntityConfig(
        ByteFieldU16(args.local_id),
        IndicationConfig(file_segment_recvd_indication_required=False),
        FaultHandler(events))
    remote = RemoteEntityConfig(
        entity_id=ByteFieldU16(args.remote_id), max_file_segment_len=args.segment,
        max_packet_len=args.segment + 64, closure_requested=args.cls == 2, crc_on_transmission=False,
        default_transmission_mode=mode, crc_type=ChecksumType.CRC_32,
        positive_ack_timer_interval_seconds=args.ack_interval, positive_ack_timer_expiration_limit=args.ack_limit,
        nak_timer_interval_seconds=args.nak_interval, nak_timer_expiration_limit=args.nak_limit,
        check_limit=args.check_limit)
    user = User(vfs, events)
    table, timers = RemoteEntityConfigTable([remote]), CheckTimers(args.check_interval)
    return local, user, table, timers, mode


def recv_pdu(sock, timeout, stats):
    """Return (pdu or None, sender address). Undecodable datagrams are counted and dropped."""
    if not select.select([sock], [], [], timeout)[0]:
        return None, None
    data, addr = sock.recvfrom(65535)
    stats["pdus_received"] += 1
    try:
        pdu = PduFactory.from_raw(data)
    except Exception as e:  # noqa: BLE001 - corrupted PDU: treat like a loss
        LOG.warning("undecodable PDU dropped: %s", e)
        stats["pdus_undecodable"] += 1
        return None, addr
    if pdu is not None and pdu.pdu_type == PduType.FILE_DATA:
        stats["filedata_bytes_received"] += len(pdu.file_data)
    elif pdu is not None and getattr(pdu, "directive_type", None) == DirectiveType.NAK_PDU:
        stats["naks_received"] += 1
    return pdu, addr


def send_pdu(sock, holder, addr, pacer, stats):
    raw = bytes(holder.pack())
    pacer.wait(len(raw) + WIRE_OVERHEAD)
    sock.sendto(raw, addr)
    stats["pdus_sent"] += 1
    stats["bytes_sent"] += len(raw)
    if holder.pdu_type == PduType.FILE_DATA:
        stats["filedata_bytes_sent"] += len(holder.to_file_data_pdu().file_data)
    elif holder.pdu_directive_type == DirectiveType.NAK_PDU:
        stats["naks_sent"] += 1


def new_stats():
    return dict.fromkeys(["pdus_sent", "pdus_received", "pdus_undecodable", "bytes_sent", "filedata_bytes_sent",
                          "filedata_bytes_received", "naks_sent", "naks_received"], 0)


def make_socket(port):
    """UDP socket with large buffers: at high rates the default ~200 KB overflows under bursts."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    for opt in (socket.SO_RCVBUF, socket.SO_SNDBUF):
        sock.setsockopt(socket.SOL_SOCKET, opt, 8 * 1024 * 1024)  # kernel caps at net.core.[rw]mem_max
    sock.bind(("0.0.0.0", port))
    return sock


def finish(report, code):
    print(json.dumps(report), flush=True)
    sys.exit(code)


def run_send(args):
    events, stats = [], new_stats()
    src_file, dest_file = Path(args.file), Path(args.dest)
    if not src_file.is_file():
        sys.exit(f"source file not found: {src_file}")
    size = src_file.stat().st_size
    local, user, table, timers, mode = build(args, NativeFilestore(), events)
    handler = SourceHandler(local, user, table, timers, SeqCountProvider(16))
    sock = make_socket(args.port)
    pacer, t0 = Pacer(args.rate), time.monotonic()
    deadline = t0 + args.timeout
    handler.put_request(PutRequest(
        destination_id=ByteFieldU16(args.remote_id), source_file=src_file, dest_file=dest_file,
        trans_mode=mode, closure_requested=args.cls == 2))
    timed_out, produced = False, True
    while True:
        while (h := handler.get_next_packet()) is not None:
            send_pdu(sock, h, args.remote, pacer, stats)
            produced = True
        if handler.state == CfdpState.IDLE:
            break
        if time.monotonic() > deadline:
            timed_out = True
            break
        pdu, _ = recv_pdu(sock, 0.0 if produced else 0.01, stats)
        produced = False
        try:
            handler.state_machine(pdu)
        except Exception as e:  # noqa: BLE001 - stray/duplicate PDUs are not fatal
            LOG.warning("PDU ignored by source handler: %s", type(e).__name__)
        produced = produced or handler.packets_ready
    dur = time.monotonic() - t0
    faults = [e for e in events if e["kind"] != "suspended"]
    ok = not timed_out and not faults and (args.cls == 1 or (user.finished or {}).get("condition") == "NO_ERROR")
    cond = "LOCAL_TIMEOUT" if timed_out else (faults[0]["condition"] if faults else "NO_ERROR")
    finish({"role": "sender", "class": args.cls, "ok": ok, "condition": cond, "file_size": size,
            "duration_s": round(dur, 3), "goodput_mbps": round(size * 8 / dur / 1e6, 3) if ok and dur else None,
            "retransmitted_bytes": max(0, stats["filedata_bytes_sent"] - size),
            "finished": user.finished if args.cls == 2 else None,  # class 1 sender cannot know the outcome
            "events": events, **stats}, 0 if ok else 3 if timed_out else 2)


def run_recv(args):
    events, stats = [], new_stats()
    vfs = RestrictedFilestore(Path(args.out_dir))
    local, user, table, timers, _ = build(args, vfs, events)
    handler = DestHandler(local, user, table, timers)
    sock = make_socket(args.port)
    stop = []
    signal.signal(signal.SIGTERM, lambda *_: stop.append(1))
    signal.signal(signal.SIGINT, lambda *_: stop.append(1))
    peer, started, t_start, t0 = args.remote, False, None, time.monotonic()
    deadline, pacer = t0 + args.timeout, Pacer(0)
    while not stop and time.monotonic() < deadline:
        while (h := handler.get_next_packet()) is not None:
            if peer is None:
                LOG.warning("no peer address known yet; dropping %s", h)
                continue
            send_pdu(sock, h, peer, pacer, stats)
        pdu, addr = recv_pdu(sock, 0.02, stats)
        if addr is not None and args.remote is None:
            peer = addr
        try:
            handler.state_machine(pdu)
        except Exception as e:  # noqa: BLE001 - stray/duplicate PDUs are not fatal
            LOG.warning("PDU ignored by dest handler: %s", type(e).__name__)
        if handler.state != CfdpState.IDLE and not started:
            started, t_start = True, time.monotonic()
        if started and handler.state == CfdpState.IDLE and args.once:
            while (h := handler.get_next_packet()) is not None:  # flush the final ACK/Finished
                send_pdu(sock, h, peer, pacer, stats)
            break
    done = started and handler.state == CfdpState.IDLE
    fin = user.finished
    complete = bool(fin) and fin["condition"] == "NO_ERROR" and fin["delivery_code"] == "DATA_COMPLETE"
    faults = [e for e in events if e["kind"] != "suspended"]
    cond = ("NO_ERROR" if complete else faults[0]["condition"] if faults else
            fin["condition"] if fin else "NOT_STARTED" if not started else "TERMINATED")
    dur = time.monotonic() - t_start if t_start else None
    finish({"role": "receiver", "class": args.cls, "ok": complete, "condition": cond, "completed": done,
            "file_size": user.file_size, "duration_s": round(dur, 3) if dur else None,
            "duplicate_bytes": max(0, stats["filedata_bytes_received"] - (user.file_size or 0)),
            "finished": fin, "events": events, **stats}, 0 if complete else 3 if not done else 2)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="mode", required=True)
    for name in ("send", "recv"):
        p = sub.add_parser(name)
        p.add_argument("--local-id", type=int, required=True)
        p.add_argument("--remote-id", type=int, required=True)
        p.add_argument("--port", type=int, default=4444, help="local UDP port (default 4444)")
        p.add_argument("--class", dest="cls", type=int, choices=(1, 2), default=2)
        p.add_argument("--segment", type=int, default=1400, help="file data bytes per PDU")
        p.add_argument("--timeout", type=float, default=3600, help="local wall-clock limit in seconds")
        p.add_argument("--ack-interval", type=float, default=10.0)
        p.add_argument("--ack-limit", type=int, default=2)
        p.add_argument("--nak-interval", type=float, default=10.0)
        p.add_argument("--nak-limit", type=int, default=2)
        p.add_argument("--check-interval", type=float, default=5.0)
        p.add_argument("--check-limit", type=int, default=2)
        p.add_argument("-v", "--verbose", action="count", default=0)
    s = sub.choices["send"]
    s.add_argument("file")
    s.add_argument("dest", help="destination path on the receiver")
    s.add_argument("--remote", type=parse_addr, required=True, help="receiver HOST:PORT")
    s.add_argument("--rate", type=parse_rate, default=0, help="send rate, e.g. 150mbit; 0 = unpaced")
    r = sub.choices["recv"]
    r.add_argument("--remote", type=parse_addr, help="sender HOST:PORT (default: learn from first PDU)")
    r.add_argument("--out-dir", default="/data", help="received files are confined to this directory")
    r.add_argument("--once", action="store_true", help="exit after one transaction")
    args = ap.parse_args()
    logging.basicConfig(level=[logging.WARNING, logging.INFO, logging.DEBUG][min(args.verbose, 2)],
                        format="%(asctime)s %(levelname)s %(message)s", stream=sys.stderr)
    (run_send if args.mode == "send" else run_recv)(args)


if __name__ == "__main__":
    main()
