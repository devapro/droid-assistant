# Architecture

Where the code lives and why it is arranged this way. The specification is
[SRS.md](./SRS.md); this is the map from it to the source.

## The shape

```
  Browser ── getUserMedia → AudioWorklet → 16 kHz → 200 ms chunks ─┐
     ▲                                        IndexedDB buffer      │
     │                                        (retransmit)          │ WebSocket
     └── live transcript ◄── WebSocket ◄───────────────────┐        │
                                                           │        ▼
  Server: ingest → ring buffer → VAD ──► pipeline (per latency mode)
                                             ASR → diarize → translate
                                                    │
                                              ┌─────┴─────┐
                                          event bus ── SQLite + audio
                                                │
                                          plugin host (off the live path)
```

## Layout

```
src/droid_assistant/
├── domain.py        the vocabulary: Utterance, Speaker, AudioBuffer, …
├── config.py        TOML + env, validated once at startup
├── events.py        the event bus and its sequence numbering
├── logging.py       structured logs, per-component levels, secret redaction
├── cli.py           serve · models · doctor · eval · backup
├── api/
│   ├── app.py       FastAPI app, CSP, static client, SPA fallback
│   ├── services.py  the object graph and session lifecycle
│   ├── export.py    Markdown · JSON · SRT · VTT
│   ├── routes/      sessions · health · plugins
│   └── ws/          ingest (client→server) · events (server→client)
├── pipeline/
│   ├── ringbuffer.py    bounded, drops oldest, counts drops
│   ├── vad.py           Silero ONNX + an energy fallback
│   ├── localagreement.py  the streaming policy for Live mode
│   ├── speakers.py      online clustering for the live modes
│   ├── modes.py         Live · Balanced · Batch as data
│   └── orchestrator.py  per-session: ingest → … → emit
├── backends/
│   ├── asr/          base · faster_whisper · whisper_cpp · deepgram · openai_asr · mock
│   ├── diarization/  base · sherpa · pyannote · mock
│   ├── translation/  base · llm · ctranslate2
│   ├── llm/          base · openai_compat
│   └── registry.py   construction and startup validation
├── store/            db · repository · search · audio · schema.sql
└── plugins/          api (the contract) · host (the machinery) · builtin/
```

## Six decisions worth knowing

### The phone is a microphone and a screen

No phone can currently run good live speech recognition for Russian and Serbian
with diarization. Serbian has no viable on-device model at all, and no phone API
does live diarization. So the browser captures and renders; the server thinks.
The evidence is in [SRS §7](./SRS.md#7-decision-record-where-does-the-computation-run)
and should be re-read before anyone proposes a native app.

The consequence that costs something: recording needs a foregrounded tab with
the screen awake, which is why `capture/wakelock.ts` exists and is not optional.

### The ring buffer drops oldest

Under a stalled consumer something must give. Growing until the process dies is
not an option; dropping newest is wrong because new speech is what the user is
waiting to see, and audio from thirty seconds ago that nothing has consumed is
already past its latency budget. So: drop oldest, count it, surface it. Silent
loss is the failure users cannot detect.

### Acknowledge the highest *contiguous* sequence

`api/ws/ingest.py` acknowledges the highest sequence with no gap beneath it, not
the highest received. The client keeps everything above that and retransmits
from the first gap. Acknowledging the highest received would let the client
discard audio covering a hole nobody noticed — which is the difference between
lossless reconnection and a plausible-looking one.

Sequence state lives on the *session*, not the socket, so a reconnect continues
rather than expecting sequence 0 again.

### Persist before publish

An utterance is committed to SQLite before its event goes out. A client that saw
a line can always find it again after a crash. The reverse order would be faster
and occasionally wrong.

### Plugins are outside the pipeline

The plugin host subscribes to the event bus; it is not a pipeline stage. FR-PLG-9
("a 60-second plugin must not delay a single utterance") is therefore satisfied
structurally rather than by discipline. A plugin that hangs, crashes, or runs for
a minute cannot reach the audio path.

### Modes are data

`pipeline/modes.py` is three profile objects parameterising one graph, not three
code paths. That is what makes mid-session switching tractable: switching swaps
a profile at a VAD boundary. Switching mid-utterance is exactly how R8 predicted
a finalised utterance would be lost, so it does not happen.

## The two-path diarization design

- **Batch mode** runs the diarizer over the whole session at once. Clustering
  with everything available is the accurate answer.
- **Live and Balanced** cannot wait. Each finalised utterance is embedded and
  matched against running centroids (`pipeline/speakers.py`) — cheap,
  incremental, stable within the session.

The live path is knowingly weaker. It is also the only thing that can attribute
a speaker to a line the moment it appears, which is what makes a live transcript
readable rather than a wall of text.

A partial hypothesis never carries a speaker (FR-DIA-10): diarization needs a
completed segment, and a guess that later flips is worse than an honest
placeholder. The type enforces it — `Utterance.speaker_id` is `None` on partials.

## Adding a backend

1. Implement the Protocol in `backends/<kind>/base.py`.
2. Declare honest `capabilities` — this is what lets `registry.validate()`
   refuse an impossible configuration at startup instead of mid-session.
3. Add one branch to `backends/registry.py`.
4. The shared contract suite in `tests/test_backend_contract.py` now applies to
   it. That suite is the point of NFR-MNT-1: an abstraction with one
   implementation has never been tested as an abstraction.

## The client

`web/src/`:

- `capture/` — the AudioWorklet (off the main thread, so rendering a
  2000-line transcript cannot glitch the recording), the IndexedDB buffer, and
  the Wake Lock.
- `transport/` — the ingest socket with retransmission, and the event stream
  with replay. The recording tab and a watching laptop use the *same* event
  stream class, so the live view and the watching view cannot drift apart.
- `state/recording.ts` — the state machine. `reconnecting` and `error` are
  fields, not states, which is why a network blip cannot interrupt a recording.
- `views/` — the four screens from SRS §5.1.
- `i18n/` — every user-facing string, so a second locale is a data change.

Routing is forty lines of hash parsing rather than a router dependency: four
screens do not justify the bundle, and NFR-RES-6 caps it at 500 KB gzipped. The
current build is about 90 KB.

## Testing

| Level | Where | What it protects |
|---|---|---|
| Unit | `test_ringbuffer`, `test_vad`, `test_localagreement` | The algorithms, in isolation from any model |
| Contract | `test_backend_contract` | One suite, every implementation of an interface |
| Integration | `test_integration` | Synthetic audio → ingest → pipeline → utterances, no browser |
| Model | `test_silero`, `test_sherpa` | Real speech through the real VAD and diarizer, skipped without weights |
| Browser | `web/e2e/` | A real Chromium against a running server, with a WAV played into `getUserMedia` |
| Accuracy | `eval/` | WER and DER against a labelled corpus, over time |

Everything except the model-bearing and browser suites runs with no weights, no
network, and no credentials. That is what keeps the suite runnable, and a suite
that is not runnable is not run.

The browser layer is the only one that can reach the AudioWorklet, the
resampler, the IndexedDB buffer, and the Wake Lock. Chrome's fake capture device
(`--use-file-for-fake-audio-capture`) plays recorded speech into `getUserMedia`,
so the audio arriving at the server has travelled the real client path. It found
two bugs the unit tests could not: an `AudioContext` closed twice during
pre-flight teardown, and a session title made of 120 characters of transcript.

```bash
docker compose up -d
cd web && npx playwright install chromium && npm run test:e2e
```

`eval/` is the important one long-term. Without it, "did that change make
transcription better?" is unanswerable and every model decision becomes taste.
