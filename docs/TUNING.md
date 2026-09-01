# Tuning

## Hardware baseline these numbers were measured on

A modest 2-core/4-thread Haswell-era mini PC (i5-4570T class) with **AVX2**
support and ~8GB RAM, running STT/TTS entirely on CPU. **AVX2 is the single
most important spec if you're picking hardware** — `faster-whisper`'s
CTranslate2 engine leans on it heavily. Without it, this whole approach is
borderline.

## Measured latency

| Stage | Time |
| --- | --- |
| VAD endpointing | ~1.2s (default `silence_frames_to_end: 60`) |
| Whisper `base.en` | ~1.0s (near-constant regardless of utterance length — see below) |
| Claude API (`effort: low`) | 1.4–3.3s, mean ~2.4s ← **the actual bottleneck** |
| Piper synthesis | ~0.3–0.7s |
| **Turn total** | 2.5–5.0s, mean ~3.8s (plus the ~1.2s endpoint wait, perceived) |

**A tool-use turn costs roughly two API round-trips** (~2x a normal
reply). The turn immediately after a tool call also tends to run slower,
since the model spends a moment weighing whether it needs to check again.

| Path | Time | Note |
| --- | --- | --- |
| Normal turn | ~3.8s | |
| Tool-use turn | ~5–8s | two API round-trips |
| Web search turn | 12–22s | fillers cover this |
| DNS down → local fallback | ~7s | 2s DNS probe + live TCP checks |

**A prediction worth stating plainly since it's counterintuitive:** it's
tempting to assume the local STT step is your bottleneck, since it's
running on modest CPU-only hardware. In practice, **the Claude API call
is the slower half of the turn**, and its time scales with reply length —
which is why the system prompt's brevity constraint ("one sentence when
possible, two at most") is a real latency lever, not just a style choice.

This is also why sentence-by-sentence TTS streaming isn't implemented
here: it would only overlap the *already-fast* synthesis step (~0.5s
total) against playback, while the actual multi-second wait is entirely
on the API side, which streaming synthesis doesn't touch at all. Measure
before optimizing something that only looks like a bottleneck.

## Whisper model choice

| Clip length | `tiny.en` | `base.en` | `small.en` |
| --- | --- | --- | --- |
| 10s | 0.68s | 1.22s | 3.64s |
| 2.89s | 0.61s | 0.99s | 3.11s |
| **Change vs. 3x shorter clip** | −10% | −19% | −15% |

**Whisper's encoder always processes a fixed 30-second window**, padding
shorter audio — so transcription time barely changes with how long the
caller actually talks. Two consequences:

- Being generous with your silence-detection threshold is nearly free —
  there's no real cost penalty to letting callers talk longer before the
  utterance is considered "done."
- `small.en` costs meaningfully more per turn than `base.en` — only worth
  it if misheard words become a real, recurring problem for your use
  case. `base.en` is the recommended default.

## Voice choice

Any Piper `.onnx` voice works — drop it in `voices/` and point
`piper_voice` in `config.yaml` at it. There's substantial CPU headroom on
even modest hardware, so a higher-quality tier voice is generally
affordable if you want something less robotic than the default.

## Key thresholds in `config.yaml`

| Setting | Default | What raising it costs | What raising it buys |
| --- | --- | --- | --- |
| `silence_frames_to_end` | 60 (~1.2s) | Slightly more dead air on every turn | Fewer cut-off callers who pause mid-sentence |
| `filler_first_seconds` | 3.0 | — | Avoids a filler firing on trivially fast replies (too low reads as twitchy) |
| `check_interval_seconds` | 120 | Slower detection of an outage | Fewer background API calls |

## Model speed vs. accuracy (Claude side)

Swapping to a faster/cheaper model, or tightening the word limit in the
system prompt, are both direct latency levers — the API call dominates
turn time far more than the local STT/TTS steps do. Start with the
default and only change it if turn latency is a real problem for your
use case, not preemptively.
