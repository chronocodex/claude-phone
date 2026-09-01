# Architecture

## The pipeline

```
Your phone (SIP extension)
   │
   ├── INBOUND: dial your feature code (e.g. *99)
   │      └─ Asterisk routes to [claude-phone-custom]
   │           └─ AudioSocket(UUID-A, your-server-ip:9000)
   │
   └── OUTBOUND (Tier 3 only): background monitor detects an outage
          └─ server.py sends AMI Originate → Asterisk
               └─ Asterisk rings your alert extension into [claude-phone-alert]
                    └─ AudioSocket(UUID-B, your-server-ip:9000)

server.py :9000 (Python):
   8 kHz PCM in ─► webrtcvad (endpointing)
                ─► soxr 8k→16k
                ─► faster-whisper (STT)      [LOCAL]
                ─► Claude API + tools        [CLOUD]
                ─► Piper (TTS)               [LOCAL]
                ─► soxr 22.05k→8k
   8 kHz PCM out ◄─ paced playback
```

**Speech-to-text and text-to-speech run entirely locally.** Only the
"thinking" — the actual Claude API call — leaves your network. Everything
else (audio capture, endpointing, transcription, synthesis, playback
pacing) happens on whatever machine runs `server.py`.

## The two-UUID trick

This is the mechanism that makes outbound alerting (Tier 3) possible
without a second server or a second port. Both Asterisk contexts —
`[claude-phone-custom]` (inbound) and `[claude-phone-alert]` (outbound
alert) — connect to the **same** Python server on the same port, but each
passes a **different UUID** in its `AudioSocket()` call. `server.py` reads
the UUID on connect and branches:

| UUID | Context | Behaviour |
| --- | --- | --- |
| UUID-A (yours) | `claude-phone-custom` | Normal greeting, empty conversation history |
| UUID-B (yours) | `claude-phone-alert` | Speaks the pending alert message, seeds history, uses a modified system prompt that tells Claude it placed this call |

Without this, there'd be no way for the code to distinguish "the user
dialed in" from "I called the user" — both arrive as an identical
AudioSocket TCP connection otherwise.

## Tiers — how the config-driven design works

The whole point of `config.yaml` and `.env` being separate from
`config.example.yaml`/`.env.example` is that **each tier is additive and
independently optional**:

| Tier | What it adds | What's required |
| --- | --- | --- |
| **1 — Core** | Dial in, converse, local STT/TTS | `ANTHROPIC_API_KEY`, Asterisk dialplan + feature code |
| **2 — Monitoring** | `check_homelab`, outage history | A `hosts:` list in `config.yaml` |
| **3 — Alerting** | It calls *you* when something drops | `ami:` section in `config.yaml` + `AMI_SECRET`/`ALERT_UUID` in `.env` |
| **4 — Notifications** | Discord posts for list items and alerts | `DISCORD_WEBHOOK` in `.env` |
| **Optional tools** | Pi-hole stats, disk-space checks, UniFi AP/switch monitoring | Each tool's own config section + secret |

At startup, `server.py` inspects what's actually configured and builds
its tool list and behavior around that — nothing crashes if a section is
blank, it just quietly doesn't offer that capability, and the startup log
prints a summary of what's on and off. See the "Startup summary" block in
the log on first run.

## Degraded modes

The design assumption throughout is that **something in the chain will be
down when you actually need this the most** — so several failure modes
are handled explicitly rather than just erroring:

| Failure | What still works |
| --- | --- |
| **Claude API unreachable (5xx, e.g. overloaded)** | A deterministic message explicitly says it's an Anthropic-side issue, not your setup — see `ANTHROPIC_DOWN_MESSAGE` in `server.py` |
| **Claude API unreachable (DNS dead, or an ambiguous error)** | Falls back to live local TCP checks (`_local_fallback()`) and reports what's actually up/down from your `hosts:` list, using raw IPs so it works even without DNS |
| **DNS dead** | Everything except the Claude API call itself. All host checks in `config.yaml` should be **raw IPs**, and the WAN probe defaults to `1.1.1.1` by address specifically so it needs no DNS |
| **Internet dead (Tier 3 only)** | The alert call still happens — TTS is local and Asterisk only needs to reach a phone on the same network. No external notifier (Discord, etc.) can reach you during a real WAN outage; the phone call is the only channel that survives it |
