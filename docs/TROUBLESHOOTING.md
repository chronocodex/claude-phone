# Troubleshooting

This is the actual list of things that broke while building and running
this project, and what fixed them. Nobody else has written most of these
down — that's the point of this file.

## Quick reference

| Symptom | Likely cause | See |
| --- | --- | --- |
| Your feature code does nothing | Dialplan not applied | Apply Config in FreePBX; `fwconsole reload` |
| Call connects then drops immediately | Service not listening | `systemctl status claude-phone`; `ss -tlnp \| grep 9000` |
| `CALL START` in the log but no audio | Codec negotiation issue | Check Asterisk's own log (`/var/log/asterisk/full`) |
| Frames are 640 bytes, not 320 | Asterisk chose 16kHz instead of 8kHz | Change `SR_PHONE` to 16000 everywhere |
| Words dropping mid-reply | Playback pacing | Check the `play:` log ratio — see #1 |
| It denies having run a tool it just ran | History not mirroring tool blocks | See #2 |
| On an alert call it says it can't call you | Wrong system prompt applied | See #3 |
| `ConnectionRefusedError` to the AMI port | AMI bound to localhost only | See #4 |
| It "can't hear me" | Whisper's `vad_filter` enabled | See #7 — remove it |
| A network call hangs instead of erroring | Likely a firewall silently dropping it, not a client bug | See the network note near the bottom |
| Answer cuts off to a fragment (e.g. `"Yes —"`) | Only taking the first text block from a tool response | See #10a |
| 20+ second wait then an apology | SDK retrying a dead DNS lookup | See #10b |
| It gives a filler ("one moment") on trivial replies | Filler threshold too aggressive | See #10c |
| Deployed a fix, bug's still there | `cp` + restart raced each other | See #10f |
| An unrelated local check failure gets blamed for an Anthropic outage | Both failure types were sharing one generic fallback message | See #13 |

## 1. `time.sleep()` per frame drops words — pace on absolute deadlines

**Symptom:** playback cuts out, skipping words or partial words. Worse on
longer replies.

**Cause:** the obvious loop — `sendall(frame); time.sleep(0.018)` —
fails. `time.sleep()` on Linux overshoots by 1–10ms (more on a loaded
low-core-count box), so an "18ms" sleep often takes 22–30ms. You send
**slower than realtime**, Asterisk starves, and plays a gap. The error
**accumulates** frame over frame, which is why short replies sound fine
and long ones fall apart.

**Fix (both parts required):**
1. **Absolute-deadline pacing** — target `start + n × 0.020` for frame
   *n*, so overshoot is absorbed rather than compounded.
2. **A short priming burst** (a few hundred ms) sent with no delay up
   front, as a jitter cushion.

**Verify with the log ratio:** `play: 9.21s audio in 8.92s wall (97%)`

| Ratio | Meaning |
| --- | --- |
| 95–100% | Healthy (sub-100% is the priming cushion) |
| >110% | Starving — increase the priming burst |
| <90% on long replies | Outrunning the phone system's buffer |

## 2. Tool results MUST go back into history

**Symptom:** it runs a tool, answers correctly, then on the next turn
says *"I didn't actually run the check"* — or re-runs the tool on a
simple "thank you."

**Cause:** appending only the final *text* to history. The model then
sees itself asserting facts with no evidence anywhere in the
conversation, so it correctly distrusts its own answer.

**Fix:** mirror the **full** message content — `tool_use` blocks and
`tool_result` blocks included:
```python
history.append({"role": "assistant", "content": message.content})
tool_response = runner.generate_tool_call_response()
if tool_response is not None:
    history.append(tool_response)
```
And **remove** any old text-only append, or you'll double-append and get
a 400.

> It wasn't hallucinating — it was correctly distrusting a history that
> had been lied to.

## 3. On an alert call, tell the model it placed the call

**Symptom:** it phones you about an outage, then says *"I can't call you
on my own."*

**Cause:** nothing in its context said the call was outbound. Fix: a
separate alert system prompt stating explicitly that this is a call it
placed — see `ALERT_SYSTEM` in `server.py`.

## 4. AMI binds to localhost by default

**Symptom:** `ConnectionRefusedError` connecting to the AMI port.

> **`Connection refused` ≠ firewall.** A firewall *drop* gives you a
> *hang then timeout*. Refused means the SYN arrived and something said
> no — i.e. nothing is listening on that interface.

**Fix**, in `manager.conf`:
```ini
[general]
enabled = yes
port = 5038
bindaddr = 0.0.0.0
```
Then reload and confirm with `ss -tlnp | grep 5038` (want `0.0.0.0:5038`).

⚠️ **FreePBX regenerates this on some upgrades.** If alerting silently
stops after an update, check this line first.

**Security note:** protection here is the AMI user's deny-all +
permit-your-server-IP ACL. AMI is effectively remote control of your
phone system — that ACL is doing the real work, not obscurity.

## 5. Whisper's cost is per-window, not per-second

Whisper's encoder always processes a **fixed 30-second window**, padding
shorter audio. **Transcription cost barely changes with utterance
length**, so being generous with silence-detection thresholds is nearly
free — see `docs/TUNING.md` for measured numbers.

## 6. `audioop` is gone; `webrtcvad` needs a fork

- **`audioop`** was removed in Python 3.13 (PEP 594). Every older blog
  post about Python telephony audio uses it. Use `soxr` for all
  resampling instead.
- **`pip install webrtcvad` fails on 3.13** — upstream is unmaintained
  with no modern wheels. Install **`webrtcvad-wheels`** instead; it
  still imports as `webrtcvad`, so no code changes needed.

## 7. Don't use Whisper's `vad_filter` on phone audio

`vad_filter=True` uses Silero VAD, which is stricter than `webrtcvad` and
**discards real speech** on 8kHz narrowband audio — producing an
assistant that seems to hear nothing. It also doesn't reliably fix
whatever stall you were hoping it would (a cough registers as speech to
Silero too).

Keep `condition_on_previous_text=False` — that only reduces hallucination
loops, it never discards audio.

## 8. Never block the socket-reading thread

Whisper consumes effectively all CPU while transcribing on a
resource-constrained box. Transcribing *inside* the audio-receive loop
stalls frame draining, the TCP buffer backs up, and audio arrives late —
**which looks exactly like a network fault.**

Architecture that works: the receive thread does nothing but read frames
onto a queue; a worker thread does STT → Claude → TTS. `faster-whisper`
releases the GIL during inference, so this genuinely helps on a
multi-core box.

## 9. Whisper hallucinates on silence

Feed it near-silence and it confidently returns "Thank you." or "Thanks
for watching!" (YouTube training data), **and takes several seconds
doing it.** Always drop empty/very-short transcriptions before sending
them onward.

## 10. Accuracy limits on real speech

`base.en` is solid on clear speech but reliably fumbles specific words —
including, amusingly, its own wake word if you're saying "Claude" (heard
as "plus," "Vlad," "Flod," especially on speakerphone), and casual
contractions.

The model reads through most transcription noise fine in normal
conversation, **but it matters for tool arguments** — design tools that
need no free-text hostname/name argument where possible, or fuzzy-match
against a known list instead of expecting an exact match.

## 10a. With server-side tools, take EVERY text block — not the first

**Symptom:** a slow web search returns a single fragment like `"Yes —"`.
The caller says *"hello?"* and the real answer arrives on the next turn.

**Cause:**
```python
return next((b.text for b in final.content if b.type == "text"), "")   # WRONG
```
With `web_search` (or any server-side tool), the response contains
**several** text blocks interleaved around the search results — a
lead-in, then the answer. `next()` grabs the lead-in and silently
discards the rest.

**Fix:**
```python
return " ".join(b.text for b in final.content if b.type == "text").strip()
```
Client-side tools don't expose this, since their results come back as
separate messages. It only bites once server-side tools are involved —
exactly when the answer matters most.

## 10b. The SDK's `timeout` does NOT cover DNS failure

**Symptom:** your DNS resolver dies; a question takes 20+ seconds to
fail, playing several fillers and then apologising. The caller has
already hung up.

**Cause:** name resolution happens in the system resolver, below the
HTTP client — the SDK's own timeout clock hasn't started yet.
`timeout=15.0` doesn't help; retry settings can make it worse.

**Fix: check DNS before dialing out**, not after:
```python
if not _dns_check(timeout=2.0):
    result["reply"] = _local_fallback(dns_ok=False)
    return
```
Pass the DNS result into the fallback function rather than re-probing —
otherwise you burn the 2 seconds twice, confirming what you already
learned.

## 10c. Filler timing: too aggressive is worse than no filler

A too-low threshold fires on nearly everything — a filler line for
"thanks, Claude" reads badly. Tune it so only genuinely slow turns
(searches, multi-step tool chains) trigger one; a few seconds is a
reasonable starting point.

**Pre-synthesize fillers at startup**, not on demand — rendering TTS live
adds real latency at the exact moment you're trying to cover it.

Also tell the model *not* to say "let me check" itself in its system
prompt, or the caller hears the sentiment twice — once from the filler,
once from the model.

## 10d. Secrets that a fresh file deploy would wipe belong in the env file, not the source

Pasting a fresh `server.py` repeatedly wipes any hardcoded value, which
fails **silently and confusingly** — e.g. an alert call connects, a
comparison UUID doesn't match, and the caller hears the normal greeting
instead of the alert. Nothing errors; it just quietly does the wrong
thing.

Read anything install-specific from `.env` (or `config.yaml`), and log a
clear warning at startup if it's unset — see the "Startup summary" block
in `server.py`.

## 10e. Discord rejects the default Python User-Agent

`urllib.request`'s default User-Agent gets a **403** from Discord's
Cloudflare. Reads exactly like "my webhook is broken" and sends you
regenerating URLs for twenty minutes. Fix is one header:
```python
"User-Agent": "YourAppName/1.0 (link to your repo or contact)",
```
Success is **HTTP 204**, not 200 — Discord returns No Content. Log the
status code so you can tell "worked" from "silently didn't."

Also: catch `HTTPError` separately and **read the response body**
(`e.read()`) — Discord returns a JSON body explaining exactly what it
rejected. A bare `except Exception` throws that detail away.

## 10f. `cp` (or a fresh file write) then `systemctl restart` can race

**Symptom:** you fix something, deploy, retest — **and the bug is still
there.** The file on disk is correct; `grep` confirms it. You start
doubting the fix.

**Cause:** the copy/write and the restart, issued back-to-back, can land
in the same second, and systemd may fork the new process before the
write is fully flushed to disk. The service then runs the **old** code
from a file that already looks new.

**Detect it:**
```bash
systemctl show claude-phone -p ActiveEnterTimestamp
stat -c '%y' /opt/claude-phone/server.py
```
If the file's mtime is at or after the process start time, restart
again.

## 11. If you add an authenticated API integration, check whether "multiple credentials" is actually supported

Some services let you *name* a credential when you generate it, which
strongly implies you can have several — but not all of them actually
support more than one active credential system-wide. If a newly-generated
key/password doesn't work while an older one still does, this is worth
checking directly rather than assuming you made a typo. (Hit this
integrating with a service's admin API during development of the
optional monitoring tools — the fix was using one shared, properly
scoped credential rather than fighting the platform's actual model.)

## 12. Hardened systemd units (`ProtectHome`, `ProtectSystem=strict`) hide more than you'd expect

If your systemd unit sets `ProtectHome=true` (a reasonable hardening
default — keep it), it makes `/home` **entirely invisible** to the
process, regardless of which user owns a given file inside it or which
user the service runs as. This bit the disk-space monitoring tool during
development: an SSH key living under a normal user's `~/.ssh/` was simply
unreachable from the service, failing cleanly (caught exception, no
crash) with a confusing lack of an obvious cause.

**Fix:** any file a hardened service needs to read must live under a path
explicitly covered by `ReadWritePaths` in the unit — not a default or
home-relative path. This includes SSH's `known_hosts` file too (override
with `-o UserKnownHostsFile=...` if you add SSH-based tooling), since
that also defaults into the now-invisible home directory.

## 13. A generic error-fallback message can accidentally blame the wrong thing

If your code has one shared "something went wrong, here's local status"
fallback message for *every* kind of failure, a genuine upstream API
outage (a `5xx`) can end up sitting in the same sentence as an unrelated
local check failure — which reads like the local issue *caused* the API
failure, even when they're completely unconnected. Worth explicitly
distinguishing "the AI provider's own infrastructure is having an issue"
from "something in your own setup is down," with a separate, deterministic
message for the former. `ANTHROPIC_DOWN_MESSAGE` in `server.py` is that
fix — added after a real `529 Overloaded` happened to occur at the same
moment as an unrelated local test failure, making the old combined
message actively misleading.

## A general network note

If a **new** network capability you're adding hangs instead of failing
cleanly, suspect a firewall silently dropping the connection before you
suspect your own client code or library. A firewall *drop* and a
client-side hang look identical from the calling end — both just sit
there until a timeout. A firewall *reject* (or a refused connection) at
least tells you something quickly. When in doubt, test raw TCP
reachability to the specific port you need, independent of whatever
higher-level client (SSH, an HTTP library, etc.) you're troubleshooting.
