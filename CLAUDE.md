# CLAUDE.md — odin

## What this is

**odin** is the orchestrator module for [loki](https://github.com/3loc/loki). It listens for the foot pedal, drains the recent transcript from mimir, and ferries the whole thing — transcript + a URL pointing at heimdall's current frame — through zerokb so it gets typed into whatever app has focus on the host MacBook.

Named after Odin, who sent his ravens out to gather knowledge of the world and brought it back to him. Same idea: odin is the small daemon that *coordinates* the others — it doesn't transcribe (mimir does), doesn't capture (heimdall does), doesn't type (zerokb does). It just *decides when to ask the others*.

## What odin actually does

Two threads, plus one trigger:

```
mimir tcp:7200 ──► odin reader thread ──► in-memory buffer
                                                │
foot pedal ──evdev──► odin pedal thread ──pedal pressed?
                                                │
                                                ▼
                                          drain buffer
                                          format payload
                                                │
                                                ▼
                                          tcp to zerokb @ pi:7070
                                                │
                                                ▼
                                       Pi types into host
                                                │
                                                ▼
                                       Host's Claude Code
                                       reads typed payload
                                       (incl. http URL),
                                       fetches frame on its own
```

The payload looks like:

```
[10:42:03] alright let's get started with the security review
[10:42:18] the first item is the SAML config we discussed last week
[10:42:33] the redirect URL we settled on was the one in dev not prod

Look at screenshot at http://192.168.10.13:7100/frame.png as reference.
```

That's it. No diarization, no Claude session on agneta, no smart instructions in the payload. The host's Claude Code is the brain — vanilla, off-the-shelf, no Loki software on the host. Earlier testing proved Claude Code automatically curls URLs in user messages and Reads them as images, so the payload doesn't even need a "fetch this" instruction.

## Why pedal-driven and not "every meeting chunk"

**You decide when context goes to Claude.** Continuous transcript dumping into Claude Code would be noisy — every line gets typed into your meeting app, taking over your keyboard, costing tokens, and interrupting whatever you were doing. Pedal-driven means you ask for help only when you actually want it, you stay in control of what gets typed and when, and the transcript buffer drains *to* Claude only at the moments you choose.

Each pedal press is a "turn" in a long Claude Code conversation. Long-term context accumulates in Claude Code's session history, not in odin's buffer. odin's buffer only holds since-the-last-press, which keeps the typed payloads short.

## Configuration

All via environment variables in `/etc/odin/odin.env`:

| Var | Default | Notes |
|---|---|---|
| `ODIN_PEDAL_DEVICE` | `/dev/input/by-id/usb-PCsensor_FootSwitch-event-kbd` | The PCsensor foot pedal's keyboard interface (it presents three; this is the one that emits KEY events) |
| `ODIN_PEDAL_KEYCODE` | `48` | KEY_B — what the pedal currently emits. Configurable in case the pedal is reprogrammed |
| `ODIN_MIMIR_HOST` | `127.0.0.1` | Where mimir's TCP fanout lives |
| `ODIN_MIMIR_PORT` | `7200` | |
| `ODIN_ZEROKB_HOST` | `192.168.10.8` | The Pi Zero on the LAN |
| `ODIN_ZEROKB_PORT` | `7070` | zerokb's TCP HID typer |
| `ODIN_FRAME_URL` | `http://192.168.10.13:7100/frame.png` | What gets pasted into the payload — the host's Claude Code curls this when it wants the image |
| `ODIN_PAYLOAD_HEADER` | `""` | Optional header line above the transcript (e.g. "MEETING CONTEXT:") |
| `ODIN_PAYLOAD_FOOTER` | `Look at screenshot at {ODIN_FRAME_URL} as reference.` | Default mentions the URL |

The defaults are right for agneta. Override per-instance if anything changes.

## Status

- [x] Scaffolding + ansible playbook
- [x] First working version: pedal → drain → zerokb → vanilla Claude Code on host
- [ ] Configurable per-host destination payload templates
- [ ] Multiple pedal types (e.g. left pedal = "ask Claude", right pedal = "save context, don't ask yet")
- [ ] Crash-safe transcript buffer (file instead of in-memory) — only if losing the buffer on a restart starts mattering

## ADR

See `loki/docs/decisions/0007-transcribe-whisperx-small.md` for the broader transcription stack. odin doesn't have its own ADR — the design was negotiated in conversation, not via formal RFC. If we ever fork the design (e.g. multiple pedals, multiple payload destinations), that's when an ADR earns its keep.

## Layout

```
odin/
├── CLAUDE.md
├── README.md
├── Makefile
├── .gitignore
├── ansible/
│   ├── inventory.yml
│   └── deploy-odin.yml
├── src/
│   └── odin.py
└── systemd/
    ├── odin.service
    └── odin.env
```
