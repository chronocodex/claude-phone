# claude-phone

Dial a phone extension and have a real spoken conversation with Claude.
Speech-to-text and text-to-speech run **entirely locally** — only the
"thinking" happens in the cloud. It can also **call you** when something
you're monitoring goes down.

Built on Asterisk/FreePBX + [AudioSocket](https://wiki.asterisk.org/wiki/display/AST/AudioSocket),
`faster-whisper`, Piper, and the Claude API.

## What it actually does

- Dial your feature code → hold a real conversation, entirely by voice
- It calls **you** when something you're monitoring drops (optional)
- Works when the internet is flaky — DNS-down and API-down are both
  handled explicitly with local fallbacks, not just left to crash
- A handful of optional tools: server/network status checks, an outage
  history, a shared household list, Pi-hole stats, disk-space checks,
  UniFi AP/switch monitoring, and web search

## Quickstart — talk to Claude in about 15 minutes (Tier 1)

**Prerequisites:** a working Asterisk/FreePBX install with a registered
extension, and a Linux (or Linux-like) box to run this on with enough CPU
to run Whisper locally — see `docs/TUNING.md` for the hardware baseline
this was built and measured on.

```bash
git clone https://github.com/YOUR_USERNAME/claude-phone.git
cd claude-phone
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# edit .env — at minimum, set ANTHROPIC_API_KEY

# download a Piper voice into voices/ (e.g. en_US-lessac-medium.onnx + .onnx.json)
# https://github.com/rhasspy/piper/blob/master/VOICES.md

python server.py
```

Then follow `asterisk/ASTERISK_SETUP.md`'s **Tier 1** section to wire up
the dialplan and a feature code. Dial in — you should hear the greeting
and be able to hold a conversation.

**Tier 1 requires no `config.yaml` at all.** Everything past this point is
optional, additive functionality.

## Everything past Tier 1 is opt-in

| Tier | What you get | Extra setup |
| --- | --- | --- |
| **1 — Core** | Dial in, converse, local STT/TTS | ~15 min, above |
| **2 — Monitoring** | Server/network status checks, outage history | Fill in a `hosts:` list in `config.yaml`. +5 min |
| **3 — Alerting** | It calls *you* when something drops | AMI user + a second dialplan context. +20 min, see `asterisk/ASTERISK_SETUP.md` |
| **4 — Notifications** | Discord posts for list items/alerts | Paste a webhook URL into `.env`. +2 min |
| **Optional tools** | Pi-hole stats, disk-space checks, UniFi AP/switch monitoring | Each is its own config section — see `config.example.yaml` |

Copy `config.example.yaml` to `config.yaml` and fill in whichever
sections you want. Every section is independently optional — leaving one
blank just means that capability quietly doesn't get offered to the
model. Nothing crashes on a partial config.

## How it works

See `docs/ARCHITECTURE.md` for the full pipeline diagram and the
two-UUID trick that makes outbound alerting possible without a second
server.

## If something breaks

`docs/TROUBLESHOOTING.md` is the real value in this repo — every hard-won
gotcha hit while building and running this, written down so you don't
have to rediscover them. Start there.

## Tuning / performance

`docs/TUNING.md` covers measured latency numbers, Whisper model choice,
voice selection, and the actual bottleneck (it's probably not what you'd
guess).

## Security notes

- **Never commit `.env` or `config.yaml`** — both are in `.gitignore`.
  Only commit the `.example` versions.
- The AMI (alerting) integration is effectively remote control of your
  phone system. Scope the AMI user's permit/deny ACL to only the IP
  running this service.
- Any SSH-based tool you add (like the optional disk-space checker)
  should use a `command=`-restricted key in `authorized_keys` on the
  target — never a general-purpose key. See the disk-space section in
  `config.example.yaml` for the pattern.
- If you're running this under a hardened `systemd` unit (recommended —
  see `systemd/claude-phone.service.example`), read
  `docs/TROUBLESHOOTING.md` #12 before adding any capability that touches
  the filesystem outside `ReadWritePaths`.

## License

MIT — see `LICENSE`.
