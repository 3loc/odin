# odin

Orchestrator module for [loki](https://github.com/3loc/loki). On foot-pedal press, drains the recent transcript from mimir, formats it together with a URL to a fresh screenshot from heimdall, and sends the whole thing to zerokb to be typed into whatever app has focus on the host MacBook.

Named after the Norse god who sent his ravens to gather knowledge of the world. odin is the small coordinator: it doesn't transcribe, capture, or type — it just decides when to *ask* the others.

## What it does

Two threads:

- **mimir reader** — subscribes to mimir's TCP fanout (`tcp://localhost:7200`), accumulates transcript lines in a buffer
- **pedal listener** — reads from the foot pedal evdev device, on press drains the buffer, formats `<transcript>\n\nLook at screenshot at <heimdall_url> as reference.`, sends to zerokb

The host's Claude Code does the rest — reads the typed payload, fetches the URL, looks at the image, responds.

## Hardware path

```
PCsensor foot pedal ──USB──► agneta
                                │
                                ▼
                              odin ──TCP──► Pi Zero (zerokb)
                                              │
                                              ▼ USB HID
                                            Host MacBook
                                              │
                                              ▼
                                          Claude Code
```

Agneta also runs heimdall and mimir. odin reads from both and writes to zerokb.

## Install

```
make deploy
```

Requires heimdall and mimir running on the same host.

## Status

v0 — pedal → drain → zerokb. See `CLAUDE.md` for what's not done.
