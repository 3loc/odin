#!/usr/bin/env python3
"""odin — orchestrator daemon for loki.

On foot pedal press, drains the recent transcript from mimir's TCP
fanout, formats it together with a heimdall frame URL, and sends the
whole thing to zerokb to be typed into whatever app has focus on the
host MacBook.

Two threads:

  mimir_reader_thread   blocking recv() on tcp://mimir, splits into
                        lines, appends each into a shared buffer
                        under a lock.

  pedal_listener_thread blocking read() on the foot pedal evdev
                        device. Tracks press-down (value=1) and
                        release (value=0) events separately:

                          * short press (release < LONG_PRESS_SEC
                            after press-down): fires the two-press
                            cycle on release — press 1 captures the
                            snapshot, press 2 drains the buffer and
                            sends the payload to zerokb.

                          * long press (>= LONG_PRESS_SEC held): fires
                            the cancel gesture — clears odin's
                            transcript buffer, disarms any in-progress
                            two-press cycle, and sends an ABORT
                            command to zerokb's control port so the
                            Pi Zero drops whatever it was still
                            typing. Useful when you accidentally drain
                            an hour of meeting and the laptop is now
                            flooding with keystrokes.

                        Autorepeat events (value=2) are ignored.

Configured by environment variables, normally via the systemd unit's
EnvironmentFile. See odin.env for the full list.

This daemon is pure stdlib (matches heimdall) — no python-evdev, no
asyncio, no extra packages. Linux input_event records are unpacked
manually with struct.
"""

from __future__ import annotations

import logging
import os
import signal
import socket
import struct
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime


# ─── config ──────────────────────────────────────────────────────────────────

PEDAL_DEVICE = os.environ.get(
    "ODIN_PEDAL_DEVICE",
    "/dev/input/by-id/usb-PCsensor_FootSwitch-event-kbd",
)
PEDAL_KEYCODE = int(os.environ.get("ODIN_PEDAL_KEYCODE", "48"))  # KEY_B by default

MIMIR_HOST = os.environ.get("ODIN_MIMIR_HOST", "127.0.0.1")
MIMIR_PORT = int(os.environ.get("ODIN_MIMIR_PORT", "7200"))

# Pi Zero (zerokb) is reached as a literal IP because zerokb's own
# ansible playbook deliberately disables avahi-daemon as part of the
# read-only-root minimization. We can't use zero.local. The Pi's IP
# is the only literal LAN address still in odin's defaults — set
# ODIN_ZEROKB_HOST in odin.env if your Pi lives somewhere else.
ZEROKB_HOST = os.environ.get("ODIN_ZEROKB_HOST", "192.168.10.8")
ZEROKB_PORT = int(os.environ.get("ODIN_ZEROKB_PORT", "7070"))

# zerokb's out-of-band control port. A separate TCP port on the Pi
# Zero that accepts text commands (currently just "ABORT"). Used by
# the long-press handler to interrupt zerokb's in-flight typing when
# the user wants to cancel a monster transcript dump. Kept on the
# same host as ZEROKB_HOST by convention.
ZEROKB_CONTROL_PORT = int(os.environ.get("ODIN_ZEROKB_CONTROL_PORT", "7071"))

# Long-press threshold for the "cancel everything" gesture: hold
# the pedal down for at least this many seconds and odin will clear
# its own transcript buffer AND tell zerokb to abort whatever it's
# currently typing. Defaults to 3 s, which is comfortably longer
# than any accidental hold and much shorter than the typical
# multi-minute typing marathon we're trying to cancel.
LONG_PRESS_SEC = float(os.environ.get("ODIN_LONG_PRESS_SEC", "3.0"))

# Two-press cycle. Press 1: POST to SNAPSHOT_CAPTURE_URL, telling
# heimdall to capture+save the current frame. Press 2: drain the
# transcript buffer and type it via zerokb together with the
# SNAPSHOT_VIEW_URL footer pointing at the saved snapshot.
#
# CAPTURE_URL points at heimdall on localhost (we're on the same
# box). VIEW_URL is what gets typed into the host's Claude Code,
# so it has to be reachable from the host machine — `agneta.local`
# is the mDNS name agneta announces via avahi-daemon (installed by
# heimdall's playbook). This is portable across LAN moves and DHCP
# renewals.
SNAPSHOT_CAPTURE_URL = os.environ.get(
    "ODIN_SNAPSHOT_CAPTURE_URL",
    "http://127.0.0.1:7100/snapshot.png",
)
SNAPSHOT_VIEW_URL = os.environ.get(
    "ODIN_SNAPSHOT_VIEW_URL",
    "http://agneta.local:7100/snapshot.png",
)

# Debounce: ignore pedal presses that arrive within this many ms of
# the previous press. Prevents an accidental double-tap from
# immediately advancing past press 1 (capture) into press 2 (dump)
# before the user has had a chance to switch tabs.
PRESS_DEBOUNCE_MS = int(os.environ.get("ODIN_PRESS_DEBOUNCE_MS", "300"))

# Cap the transcript buffer to the last N lines, or 0 to disable
# the cap entirely. Default is 0 = unbounded — the buffer runs
# freely from the last successful press 2 (the dump) until the next
# press 2, no matter how long that is.
#
# Why this is safe: mimir's WhisperX VAD already filters out silence
# — windows with no speech generate zero segments and are never
# broadcast. So the buffer only accumulates lines for moments of
# actual speech, not for the wall-clock duration since the last
# press. A typical 30-minute meeting where people speak roughly half
# the time produces ~180 transcript lines, ~14 KB of payload, which
# zerokb types in about a minute.
#
# Set this to a positive integer if you want a hard cap as a safety
# net (e.g. 60 = roughly the last 5 minutes of speech).
BUFFER_MAX_LINES = int(os.environ.get("ODIN_BUFFER_MAX_LINES", "0"))

# Why no bracketed paste mode here:
#
# An earlier version of odin wrapped the payload in ESC[200~ ... ESC[201~
# bracketed-paste escape sequences, on the theory that modern terminals
# would treat the wrapped payload as a single atomic input regardless
# of internal newlines. That theory was wrong — not because terminals
# don't support bracketed paste (they do), but because zerokb's USB-HID
# typing doesn't send the ESC byte (0x1b). zerokb's keyboard mapping
# only handles printable ASCII + a few control chars (tab, newline);
# control bytes below 0x20 get silently dropped. So the ESC was stripped
# and the literal "[200~" and "[201~" leaked into the typed text as
# visible characters, which is exactly what you don't want.
#
# The solution is simpler: produce a payload that contains NO newlines
# in the body, and exactly ONE newline at the very end (the submit).
# Transcript lines are joined with single spaces and read as continuous
# prose, which mimir's sentence-aware segments support naturally. The
# footer goes on the same single line. One \n at the end submits the
# whole thing as one Claude message.

PAYLOAD_HEADER = os.environ.get("ODIN_PAYLOAD_HEADER", "")
PAYLOAD_FOOTER = os.environ.get(
    "ODIN_PAYLOAD_FOOTER",
    "(screenshot for more context: {url})",
)


# ─── linux input_event format ────────────────────────────────────────────────

# struct input_event {
#     struct timeval time;   // 8 + 8 bytes on 64-bit
#     __u16 type;
#     __u16 code;
#     __s32 value;
# };
INPUT_EVENT_FORMAT = "llHHi"
INPUT_EVENT_SIZE = struct.calcsize(INPUT_EVENT_FORMAT)
EV_KEY = 1
EV_SYN = 0


# ─── logging ─────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
log = logging.getLogger("odin")


# ─── shared state ────────────────────────────────────────────────────────────

shutdown_event = threading.Event()

# Transcript buffer: list of complete lines (newline-stripped) received
# from mimir since the last successful press 2 (the dump). Press 1 does
# NOT drain the buffer.
buffer: list[str] = []
buffer_lock = threading.Lock()

# Two-press state. False = waiting for press 1 (capture). True = capture
# done, waiting for press 2 (dump). Mutated by the pedal-listener
# thread (short-press path) AND by the long-press timer callback
# (cancel path), so it's now protected by press_state_lock below.
snapshot_armed = False
last_press_at = 0.0  # time.monotonic() of the most recent short press, for debounce

# Press state for the long-press detector. The pedal-listener thread
# owns the transitions; the long-press timer callback runs on its
# own thread (threading.Timer) and reads/writes the same fields, so
# both sides hold press_state_lock.
press_state_lock = threading.Lock()
press_state = {
    # time.monotonic() at press-down, or None when not currently pressed
    "down_at": None,  # type: float | None
    # threading.Timer scheduled to fire the long-press callback
    # LONG_PRESS_SEC after press-down. Cancelled on release; also
    # cleared after it fires.
    "long_press_timer": None,  # type: threading.Timer | None
    # True after the long-press callback has fired, so the matching
    # release event knows NOT to also fire a short press. Reset on
    # the next press-down.
    "long_press_fired": False,
}


# ─── mimir reader ────────────────────────────────────────────────────────────

def mimir_reader_thread() -> None:
    """Connect to mimir's TCP fanout, append every received line to the buffer.

    Reconnects with exponential backoff on disconnect or refusal —
    mimir might restart (e.g. via PartOf=heimdall) and odin should
    just pick back up.
    """
    backoff = 0.5
    while not shutdown_event.is_set():
        try:
            log.info("mimir: connecting to %s:%d", MIMIR_HOST, MIMIR_PORT)
            sock = socket.create_connection((MIMIR_HOST, MIMIR_PORT), timeout=5)
            # create_connection's timeout applies to BOTH connect and
            # subsequent recv. mimir emits a line every ~5 seconds, so
            # a 5s recv timeout would cause spurious "read error: timed
            # out" reconnect loops during quiet patches. Clear the
            # timeout for the read phase — recv blocks until data or
            # actual disconnect.
            sock.settimeout(None)
        except (ConnectionRefusedError, socket.timeout, OSError) as e:
            log.warning("mimir: not ready (%s); retrying in %.1fs", e, backoff)
            shutdown_event.wait(backoff)
            backoff = min(backoff * 2, 5.0)
            continue

        log.info("mimir: connected")
        backoff = 0.5
        line_buf = bytearray()
        try:
            while not shutdown_event.is_set():
                chunk = sock.recv(8192)
                if not chunk:
                    log.warning("mimir: closed the connection")
                    break
                line_buf.extend(chunk)
                # Split on newline; keep incomplete tail for next iteration.
                while True:
                    nl = line_buf.find(b"\n")
                    if nl < 0:
                        break
                    line = line_buf[:nl].decode("utf-8", errors="replace")
                    del line_buf[: nl + 1]
                    line = line.strip()
                    if not line:
                        continue
                    # Skip mimir's welcome banner.
                    if line.startswith("#"):
                        log.debug("mimir: ignoring banner: %s", line)
                        continue
                    with buffer_lock:
                        buffer.append(line)
                        # Optional safety cap. When BUFFER_MAX_LINES > 0,
                        # trim oldest lines so the buffer never exceeds
                        # the cap. When 0 (default), the buffer grows
                        # unbounded between press 2's — relying on
                        # WhisperX's VAD to keep silence from inflating
                        # it.
                        if BUFFER_MAX_LINES > 0 and len(buffer) > BUFFER_MAX_LINES:
                            del buffer[0 : len(buffer) - BUFFER_MAX_LINES]
        except OSError as e:
            log.error("mimir: read error: %s", e)
        finally:
            try:
                sock.close()
            except OSError:
                pass

        if not shutdown_event.is_set():
            log.info("mimir: reconnecting in %.1fs", backoff)
            shutdown_event.wait(backoff)
            backoff = min(backoff * 2, 5.0)

    log.info("mimir: reader exiting")


# ─── pedal listener ──────────────────────────────────────────────────────────

def pedal_listener_thread() -> None:
    """Read evdev events from the pedal device. On press, fire on_press()."""
    backoff = 0.5
    while not shutdown_event.is_set():
        try:
            log.info("pedal: opening %s", PEDAL_DEVICE)
            fd = os.open(PEDAL_DEVICE, os.O_RDONLY)
        except (FileNotFoundError, PermissionError) as e:
            log.warning("pedal: not ready (%s); retrying in %.1fs", e, backoff)
            shutdown_event.wait(backoff)
            backoff = min(backoff * 2, 5.0)
            continue

        log.info(
            "pedal: listening for keycode=%d (press + release, long-press=%.1fs)",
            PEDAL_KEYCODE, LONG_PRESS_SEC,
        )
        backoff = 0.5
        try:
            while not shutdown_event.is_set():
                # Read one event at a time. Blocking — if shutdown happens
                # mid-read we'll get cancelled when systemd sends SIGTERM.
                data = os.read(fd, INPUT_EVENT_SIZE)
                if len(data) != INPUT_EVENT_SIZE:
                    log.warning("pedal: short read (%d bytes), reopening", len(data))
                    break
                _sec, _usec, ev_type, code, value = struct.unpack(
                    INPUT_EVENT_FORMAT, data
                )
                if ev_type != EV_KEY or code != PEDAL_KEYCODE:
                    continue
                # value == 1 → press-down (start long-press timer)
                # value == 0 → release  (fire short press or cleanup after long-press)
                # value == 2 → autorepeat (ignore — we care about the edges only)
                if value == 1:
                    log.info("pedal: PRESS-DOWN")
                    _handle_press_down()
                elif value == 0:
                    log.info("pedal: RELEASE")
                    _handle_press_up()
        except OSError as e:
            log.error("pedal: read error: %s", e)
        finally:
            try:
                os.close(fd)
            except OSError:
                pass

        if not shutdown_event.is_set():
            log.info("pedal: reopening in %.1fs", backoff)
            shutdown_event.wait(backoff)
            backoff = min(backoff * 2, 5.0)

    log.info("pedal: listener exiting")


# ─── press gesture state machine ─────────────────────────────────────────────

def _handle_press_down() -> None:
    """Record a press-down event and schedule the long-press timer.

    Called from the pedal-listener thread on value=1. Starts a
    threading.Timer that fires after LONG_PRESS_SEC unless cancelled
    by a release event before it fires. The timer runs on its own
    thread; both it and this function touch press_state under
    press_state_lock.
    """
    with press_state_lock:
        if press_state["down_at"] is not None:
            # Already pressed (spurious duplicate press-down, e.g.
            # missed release event). Treat as a no-op rather than
            # stacking timers.
            log.warning("pedal: press-down while already down, ignoring")
            return
        press_state["down_at"] = time.monotonic()
        press_state["long_press_fired"] = False
        timer = threading.Timer(LONG_PRESS_SEC, _long_press_callback)
        timer.daemon = True
        press_state["long_press_timer"] = timer
        timer.start()


def _handle_press_up() -> None:
    """Handle a release event — fire short press unless long-press already fired.

    Called from the pedal-listener thread on value=0. Cancels the
    long-press timer and, if the long-press hasn't already fired,
    fires the short-press action (the existing two-press cycle —
    snapshot on press 1, drain-and-dump on press 2).
    """
    fire_short = False
    with press_state_lock:
        if press_state["down_at"] is None:
            # Stray release with no matching press-down. Likely a
            # state-recovery situation after a missed event. Ignore.
            return
        if press_state["long_press_timer"] is not None:
            press_state["long_press_timer"].cancel()
            press_state["long_press_timer"] = None
        if not press_state["long_press_fired"]:
            fire_short = True
        press_state["down_at"] = None
        # long_press_fired stays True until the next press-down so
        # nothing else can sneak a short press in; _handle_press_down
        # resets it.

    if fire_short:
        on_pedal_press()


def _long_press_callback() -> None:
    """Fired by threading.Timer after LONG_PRESS_SEC of continuous hold.

    Runs on the timer thread, not the pedal-listener thread. Checks
    that the press is still active (race with the cancel path in
    _handle_press_up) and, if so, fires the cancel gesture.
    """
    with press_state_lock:
        if press_state["down_at"] is None:
            # User released between timer fire and lock acquisition —
            # the short-press path already ran. Nothing to do.
            return
        if press_state["long_press_fired"]:
            return  # defensive; shouldn't happen
        press_state["long_press_fired"] = True
        press_state["long_press_timer"] = None

    # Fire the long-press action outside the lock so it can't deadlock
    # on anything held by the short-press path.
    on_pedal_long_press()


# ─── pedal-press handler ─────────────────────────────────────────────────────

def capture_snapshot() -> bool:
    """Tell heimdall to capture the current frame and save it to disk.

    Called on press 1. Returns True on success. The actual frame
    capture (ffmpeg cold-open + warm-frame skip) happens inside
    heimdall and takes ~1 second; we wait for it.
    """
    try:
        req = urllib.request.Request(SNAPSHOT_CAPTURE_URL, method="POST", data=b"")
        with urllib.request.urlopen(req, timeout=10) as resp:
            log.info("snapshot: captured (HTTP %d, %s)", resp.status, resp.reason)
            return True
    except urllib.error.HTTPError as e:
        log.error("snapshot: HTTP %d %s", e.code, e.reason)
    except urllib.error.URLError as e:
        log.error("snapshot: URL error: %s", e.reason)
    except Exception as e:
        log.error("snapshot: capture failed: %s", e)
    return False


def build_payload(lines: list[str]) -> bytes:
    """Build the byte stream odin sends to zerokb.

    Layout (single line, single submit):
        <header> <transcript> <footer><\n>

    Strict rule: NO newline anywhere in the body. Every newline becomes
    an Enter keystroke when zerokb types it, and every Enter submits
    whatever's in Claude Code's input field. We want exactly one
    submit at the very end, so exactly one \n — at the end. All
    section boundaries (header / transcript / footer) are joined with
    single spaces.
    """
    parts: list[str] = []
    if PAYLOAD_HEADER:
        parts.append(PAYLOAD_HEADER)
    if lines:
        # Join transcript lines with single spaces. mimir's segments
        # are sentence-aware (most end with punctuation) so this reads
        # as continuous prose without manual sentence merging.
        parts.append(" ".join(line.strip() for line in lines if line.strip()))
    else:
        parts.append("(no recent transcript)")
    parts.append(PAYLOAD_FOOTER.format(url=SNAPSHOT_VIEW_URL))

    body = " ".join(parts)
    # Defensive: replace any stray newlines or carriage returns inside
    # the joined body with spaces. Mimir shouldn't emit newlines mid-
    # segment, but if a future model variant ever did, we'd silently
    # submit the typed text in pieces. Cheap belt-and-suspenders.
    body = body.replace("\r", " ").replace("\n", " ")
    # Note: NO trailing newline. The user explicitly does not want
    # zerokb to press Enter for them at the end of the dump — they
    # want a chance to read what was typed in Claude Code's input
    # field, edit if needed, and submit manually. Earlier versions
    # appended b"\n" here to auto-submit; that was the wrong default.
    return body.encode("utf-8")


def send_to_zerokb(payload_bytes: bytes) -> bool:
    try:
        log.info("zerokb: sending %d bytes to %s:%d",
                 len(payload_bytes), ZEROKB_HOST, ZEROKB_PORT)
        with socket.create_connection((ZEROKB_HOST, ZEROKB_PORT), timeout=3) as zk:
            zk.sendall(payload_bytes)
        log.info("zerokb: payload sent")
        return True
    except OSError as e:
        log.error("zerokb: send failed (%s)", e)
        return False


def abort_zerokb() -> bool:
    """Tell zerokb to drop whatever it's currently typing.

    Connects to zerokb's separate control port (ZEROKB_CONTROL_PORT,
    default 7071) and sends a single "ABORT\\n" line. zerokb's
    control listener runs on its own thread so this works even when
    the data port (7070) is blocked writing a huge payload to HID.
    The control handler sets a shared atomic flag that the data-side
    typing loop checks between every byte; once set, the remaining
    bytes from the in-flight connection are drained silently rather
    than typed.

    Returns True on successful send, False on any IO error. Failure
    is logged but not raised — the long-press cancel path is a
    best-effort thing; if the Pi is unreachable, the user can still
    fall back to unplugging the USB cable.
    """
    try:
        log.info("zerokb: sending ABORT to %s:%d",
                 ZEROKB_HOST, ZEROKB_CONTROL_PORT)
        with socket.create_connection(
            (ZEROKB_HOST, ZEROKB_CONTROL_PORT), timeout=2
        ) as zk:
            zk.sendall(b"ABORT\n")
            # Read the response (zerokb writes "OK\n" or "ERR ..."),
            # best effort. Not worth failing the abort if the
            # response is malformed — the flag has been set either
            # way on the server side.
            try:
                zk.settimeout(1.0)
                resp = zk.recv(64)
                log.info("zerokb: ABORT response: %r", resp.strip())
            except OSError:
                pass
        return True
    except OSError as e:
        log.error("zerokb: ABORT send failed (%s)", e)
        return False


def on_pedal_press() -> None:
    """Two-press cycle: press 1 captures snapshot, press 2 drains + dumps.

    Called from the pedal-listener thread. Single-threaded entry, so
    snapshot_armed and last_press_at don't need a lock.
    """
    global snapshot_armed, last_press_at

    # Debounce: a real pedal stomp lasts a few hundred ms (kernel
    # autorepeat fires after 250ms with our pedal). value=1 only
    # arrives once per actual press, but a quick double-tap could
    # advance past press 1 immediately. Drop presses within the
    # debounce window of the previous one.
    now = time.monotonic()
    if (now - last_press_at) * 1000 < PRESS_DEBOUNCE_MS:
        log.info("pedal: ignored (within %dms debounce window)", PRESS_DEBOUNCE_MS)
        return
    last_press_at = now

    if not snapshot_armed:
        # PRESS 1 — capture the current frame and arm the dump.
        log.info("press 1: capturing snapshot...")
        if capture_snapshot():
            snapshot_armed = True
            log.info("press 1: snapshot armed — switch to Claude Code, press again to dump")
        else:
            log.warning("press 1: snapshot capture failed; not armed, try again")
        return

    # PRESS 2 — drain the buffer and type it via zerokb together with
    # the snapshot URL footer.
    with buffer_lock:
        lines_to_send = list(buffer)
    payload = build_payload(lines_to_send)
    log.info("press 2: dumping %d buffered transcript lines (%d bytes)",
             len(lines_to_send), len(payload))

    if not send_to_zerokb(payload):
        log.error("press 2: zerokb send failed; staying armed (press again to retry)")
        return

    # Success — drop the lines we sent. Mimir may have appended more
    # while zerokb was typing; we keep those for the next cycle.
    with buffer_lock:
        if len(buffer) >= len(lines_to_send):
            del buffer[: len(lines_to_send)]
        else:
            buffer.clear()
    snapshot_armed = False
    log.info("press 2: complete, ready for next cycle")


def on_pedal_long_press() -> None:
    """Cancel gesture — drop everything and tell zerokb to stop typing.

    Called from the long-press timer callback after LONG_PRESS_SEC
    of continuous hold. Does three things, in order:

      1. Clear odin's own transcript buffer so the next short press
         doesn't re-send anything we're trying to cancel.
      2. Disarm any in-progress two-press cycle (if the user long-
         presses after press 1 but before press 2, they probably
         want the armed snapshot forgotten too).
      3. Send ABORT to zerokb's control port so the Pi Zero drops
         whatever it's currently typing into the host.

    The long-press gesture is best-effort: if zerokb is unreachable
    (e.g. Pi Zero powered off) the odin-side cleanup still happens
    and the error is logged. The user's fallback is to unplug the
    Pi Zero's USB cable.
    """
    global snapshot_armed

    log.warning("pedal: LONG PRESS — canceling everything")

    # 1. Clear the local transcript buffer.
    with buffer_lock:
        dropped = len(buffer)
        buffer.clear()
    log.info("long-press: dropped %d buffered transcript lines", dropped)

    # 2. Disarm any armed two-press cycle.
    if snapshot_armed:
        snapshot_armed = False
        log.info("long-press: disarmed in-progress two-press cycle")

    # 3. Tell zerokb to abort.
    if abort_zerokb():
        log.info("long-press: zerokb abort sent successfully")
    else:
        log.error(
            "long-press: zerokb abort failed — the Pi Zero may still be "
            "typing; unplug the USB cable as a fallback"
        )


# ─── main ────────────────────────────────────────────────────────────────────

def shutdown(signum, frame) -> None:
    log.info("received signal %d, shutting down", signum)
    shutdown_event.set()


def main() -> None:
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    log.info(
        "odin starting: pedal=%s keycode=%d mimir=%s:%d "
        "zerokb=%s:%d (control=%d) "
        "snapshot_capture=%s snapshot_view=%s "
        "debounce=%dms long_press=%.1fs",
        PEDAL_DEVICE, PEDAL_KEYCODE, MIMIR_HOST, MIMIR_PORT,
        ZEROKB_HOST, ZEROKB_PORT, ZEROKB_CONTROL_PORT,
        SNAPSHOT_CAPTURE_URL, SNAPSHOT_VIEW_URL,
        PRESS_DEBOUNCE_MS, LONG_PRESS_SEC,
    )

    threads = [
        threading.Thread(target=mimir_reader_thread, name="mimir-reader", daemon=True),
        threading.Thread(target=pedal_listener_thread, name="pedal-listener", daemon=True),
    ]
    for t in threads:
        t.start()

    # Watchdog. If either thread crashes, exit so systemd restarts us.
    while not shutdown_event.is_set():
        for t in threads:
            if not t.is_alive():
                log.error("thread %s died unexpectedly, exiting", t.name)
                shutdown_event.set()
                break
        shutdown_event.wait(2.0)

    log.info("odin stopped")
    # No heavy C++ libs in this process, plain sys.exit is fine (unlike
    # mimir, which needs os._exit to dodge a libtorch destructor crash).
    sys.exit(0)


if __name__ == "__main__":
    main()
