# Configuration

Configuration is a TOML file at `$DROID_DATA/config.toml`, overridable by
environment variables. Precedence, highest first:

1. environment variables — `DROID_ASR__MODEL=medium`
2. `config.toml`
3. defaults

Everything is validated at startup. An invalid value fails with a message naming
the field, the value it got, and what it accepts — never a `KeyError` forty
minutes into a meeting (FR-CFG-2).

**Credentials are never configuration fields.** Config stores the *name* of the
environment variable to read (`api_key_env`), so no key material can reach a log
line, an API response, or a config export (FR-CFG-4).

## A minimal file

```toml
[server]
data_dir = "/mnt/ssd/droid"

[capture]
languages = ["en", "ru"]
target_language = "en"

[asr]
model = "large-v3-turbo"
```

## `[server]`

| Field | Default | Notes |
|---|---|---|
| `host` | `"127.0.0.1"` | Loopback by default. LAN exposure must be a deliberate edit (NFR-SEC-1) |
| `port` | `8000` | |
| `data_dir` | `./data` | Or `$DROID_DATA_DIR`. Put this on an SSD, not an SD card |
| `allowed_origins` | `[]` | Exact origins accepted on a WebSocket upgrade when not on loopback (NFR-SEC-4) |
| `disconnect_grace_s` | `90.0` | How long a session survives its recording client vanishing (FR-SES-4). Raise it if you switch apps often |
| `min_free_disk_mb` | `2048` | Below this, starting a session is refused rather than failing part-way (NFR-RES-7) |
| `warn_free_disk_mb` | `8192` | Must be ≥ `min_free_disk_mb` |
| `log_level` | `"info"` | `debug` \| `info` \| `warning` \| `error` |

## `[capture]`

| Field | Default | Notes |
|---|---|---|
| `languages` | `["en","ru","sr"]` | What the UI offers. Operator-editable, never hard-coded (FR-CFG-3) |
| `target_language` | `"en"` | Must appear in `languages` |
| `default_mode` | `"balanced"` | `live` \| `balanced` \| `batch` |
| `chunk_ms` | `200` | Audio chunk size. Smaller is lower latency and more overhead |
| `echo_cancellation` | `false` | See below |
| `noise_suppression` | `false` | See below |
| `auto_gain_control` | `false` | See below |
| `client_buffer_cap_mb` | `256` | Client-side buffer ceiling (NFR-RES-5) |

**Capture source** is a client-side choice, stored in the browser rather than
here, and recorded per session in `audio_constraints.source`. It is one of
`microphone`, `system` (whatever is playing on the machine running the browser),
or `both`. See the recording guidance in the README for when each is right.

**On the three audio-processing defaults.** All three are off, and that is
deliberate. They are tuned for a single near voice on a call, and noise
suppression in particular will attenuate the quieter people at a meeting table —
the exact recording this product exists for. Turn them on for one-to-one
dictation if it helps; leave them off for meetings. R6 exists to replace this
reasoning with a measurement.

## `[vad]`

| Field | Default | Notes |
|---|---|---|
| `backend` | `"silero"` | `silero` \| `energy`. Energy is a fallback for machines where onnxruntime will not install, and is much worse in noise |
| `threshold` | `0.5` | Speech probability above which a frame counts as speech |
| `min_speech_ms` | `250` | Shorter bursts are discarded — a door closing is not an utterance |
| `min_silence_ms` | `700` | A pause this long ends an utterance. **This is the main latency knob in Balanced mode**: every result is delayed by exactly this much |
| `speech_pad_ms` | `200` | Padding on both edges, so no word is clipped |
| `soft_max_speech_ms` | `8000` | Past this much unbroken speech, a much shorter pause is accepted as an endpoint |
| `min_silence_long_ms` | `180` | That shorter pause. Must be ≤ `min_silence_ms` |
| `force_split_after_ms` | `12000` | Past this, cut at the quietest moment seen even if no pause arrives at all |
| `max_speech_ms` | `30000` | Absolute backstop, so a monologue is never one enormous utterance |
| `turn_gap_ms` | `5000` | Balanced and Batch. A speaker's consecutive segments are rejoined into one message unless they paused longer than this. `0` keeps one message per segment |
| `max_turn_ms` | `120000` | …and no rejoined message grows past this |

**Why segments are rejoined.** A VAD segment is a breath, but a *message* is a
turn: everything one person says before someone else speaks. Left as segments,
a conversation renders as a column of one-word lines with the same name over
each, and continuous speech renders as a new message every
`soft_max_speech_ms`. So Balanced and Batch reassemble them, which buys two
things:

* the utterance already published is **updated** as its turn grows, rather than
  another one being added per breath;
* every segment after the first is recognised with the turn so far as its
  decoding prompt, so the second half of a sentence comes back agreeing with
  the first. Backends that accept a prompt — `faster_whisper`, `whisper_cpp`,
  `openai` — use it; the rest ignore it and only the joining applies.

Text is only ever **appended**: nothing already on screen is rewritten, which is
the property FR-LAT-5 exists for. Live is left alone — LocalAgreement already
governs how its utterances settle.

**A segment is not one speaker, either.** VAD hears speech and silence, not
people, so an interruption or a handover mid-sentence comes back as one segment
holding two voices. Recognised text is therefore cut at the diarizer's speaker
boundaries, using word timings, before any of it becomes a message. Backends
without word timings (`whisper_cpp`, `gigaam`) cannot be cut and fall back to
attributing the whole segment by majority. See `diarization.window_ms`.

Two consequences worth knowing. A message is translated once, when it closes,
so translations land up to `turn_gap_ms` later than they used to — on whole
turns, which is also what FR-TRA-3's context exists to get right. And a
monologue merges up to `max_turn_ms`; lower it if you want shorter blocks with
more timestamps to navigate by.

**Why three ceilings.** Conversation pauses constantly, so `min_silence_ms`
alone segments it well. Continuous speech does not: a narrated video, a lecture,
or anyone reading aloud can run half a minute without a single 700 ms gap, and
waiting for one means **no transcript appears until the recording stops**.

So the bar drops as an utterance runs long:

| Speech so far | Ends on |
|---|---|
| under 8 s | a 700 ms pause — clean sentence boundaries |
| 8–12 s | a 180 ms pause — a breath is enough |
| over 12 s | the quietest moment since 8 s, pause or not |
| 30 s | now, wherever that falls |

The forced cut picks the quietest frame rather than firing on a timer, so it
lands at the least bad place available instead of mid-syllable. Lower
`soft_max_speech_ms` for a livelier transcript at some cost in accuracy —
recognition is better with more context — and raise it if you mostly record
conversation and prefer whole sentences.

## `[asr]`

| Field | Default | Notes |
|---|---|---|
| `backend` | `"faster_whisper"` | `faster_whisper` \| `whisper_cpp` \| `deepgram` \| `openai` \| `mock` |
| `model` | `"large-v3-turbo"` | Tier A. Use `medium`/`small` int8 on Tier B, `small` on Tier C. **Size matters much more for non-English** — see below. Can also be a path to a converted fine-tune |
| `device` | `"auto"` | `auto` \| `cpu` \| `cuda` \| `metal`. CTranslate2 has no Metal path — use `whisper_cpp` on a Mac to reach the GPU |
| `compute_type` | `"auto"` | `int8` on CPU, `float16` on CUDA |
| `beam_size` | `5` | Lower is faster and slightly worse |
| `no_speech_threshold` | `0.6` | Above this probability a segment is dropped as a hallucination (FR-ASR-9) |
| `condition_on_previous_text` | `false` | `true` amplifies hallucination loops. Leave it off |
| `word_timestamps` | `true` | Needed for click-to-seek (FR-ASR-5) |
| `serbian_script` | `"latin"` | `latin` \| `cyrillic`. Output is normalised to one script (FR-ASR-10) |
| `preload` | `[]` | Extra `backend:model` ids to keep on disk so they can be selected from the UI without a wait. See below |
| `download_missing` | `false` | Fetch anything in the routing set that is missing, on start, in the background |

**Choosing a size for a non-English language.** Whisper's multilingual capacity
is weighted towards English, so the smaller models degrade far faster away from
it. Measured on a small Russian set with `droid-assistant eval`:

| Model | English WER | Russian WER | RTF (CPU, M-series) |
|---|---|---|---|
| `tiny` | 0.0% | 19.7% | 0.05 |
| `base` | 0.0% | 9.8% | 0.08 |
| `small` | 0.0% | 4.9% | 0.18 |
| `large-v3-turbo` | 0.0% | 3.3% | 0.37 |

English barely moves; Russian improves sixfold. Note the cost of that: the
largest model is still comfortably faster than realtime on CPU (RTF 0.37), so
for Balanced and Batch mode there is rarely a reason to economise. Do not trade
model size away for a non-English language — and measure on your own audio,
since these are clean synthetic recordings and optimistic in absolute terms.

For a language where the stock model is still weak, `droid-assistant models
suggest --language ru` lists community fine-tunes. A fine-tune published in the
usual Hugging Face format needs converting once:

```bash
droid-assistant models convert antony66/whisper-large-v3-russian
# then set asr.model to the printed path
```

A fine-tune is a trade, not a free win: better on its language, usually worse on
everything else, and typically worse at code-switching. Compare before adopting:

```bash
droid-assistant eval --backends faster_whisper:large-v3-turbo,faster_whisper:<path>
```

### `[asr.by_language]` — a different engine per language

No model is best at everything. Whisper is the strongest all-rounder; a
language-specialised model beats it on its own language and knows nothing else.
Rather than choosing one compromise, route per language:

```toml
[asr]
backend = "faster_whisper"
model = "large-v3-turbo"        # everything not listed below

[asr.by_language.ru]
backend = "gigaam"              # Russian only
```

The route is chosen when a session **pins exactly one language**. With several
pinned there is nothing to route on, so the default applies — and the model
detects one language per window anyway (R13).

Backends are loaded once and cached, so switching language between sessions does
not reload weights. Re-routing a language releases the model it used to point
at, so changing your mind repeatedly does not leave three sets of weights
resident.

**You do not have to edit this file.** Settings → Speech models does the same
thing from a browser: a default, one row per language in `capture.languages`,
and a list of what is on disk. It takes effect on the next session, with no
restart (FR-CFG-8). The picker only offers models this server actually has —
routing a language to weights that are not present would fail at the moment of
recording, which is the worst time to find out.

### Which models get downloaded

The download set is **derived from the routing**, not listed separately:
`asr.model` plus every model `asr.by_language` points at. A hand-maintained
list drifts out of step with the routing, and the failure surfaces mid-meeting.

```bash
droid-assistant models list              # what exists, what is on disk, what uses it
droid-assistant models download          # everything this configuration routes to
droid-assistant models download --asr gigaam:v3-rnnt   # just one
```

`asr.preload` adds to that set without routing anything to it — the models you
want *available to switch to* from the UI:

```toml
[asr]
preload = ["faster_whisper:small", "gigaam:v3-rnnt"]
```

In `.env`, where a TOML list is not expressible, a comma-separated string works:

```bash
DROID_ASR__PRELOAD=faster_whisper:small,gigaam:v3-rnnt
DROID_ASR__DOWNLOAD_MISSING=true    # fetch them on start, in the background
```

`download_missing` is off by default because on a fresh volume it is a
multi-gigabyte download, and that should be a decision rather than a surprise.
The server answers throughout either way — downloading never blocks the socket,
and `GET /api/models` reports progress.

### `[asr.gigaam]` — Russian

GigaAM is trained for Russian specifically. It runs through `sherpa-onnx`, which
this project already uses for diarization, so it needs no PyTorch, no gated
download, and no new dependency.

| Field | Default | Notes |
|---|---|---|
| `model` | `"v3-rnnt"` | `v3-rnnt` \| `v3-ctc` \| `v2-rnnt` \| `v2-ctc` |
| `num_threads` | `4` | |
| `feature_dim` | `64` | Matches the published conversions |
| `provider` | `"cpu"` | `cpu` \| `cuda` \| `coreml` |

```bash
droid-assistant models gigaam v3-rnnt
```

Measured against Whisper on this project's Russian set:

| Backend | Russian WER | RTF |
|---|---|---|
| `faster_whisper:small` | 4.9% | 0.21 |
| `faster_whisper:large-v3-turbo` | 3.3% | 0.39 |
| `gigaam:v3-ctc` | 4.9% | **0.02** |
| `gigaam:v3-rnnt` | **3.3%** | **0.02** |

The accuracy figures tie on *this* set, because clean synthetic speech is easy
and both models saturate. **The speed does not tie: GigaAM is roughly twenty
times faster.** GigaAM's accuracy advantage is expected on spontaneous
conversational Russian, which this corpus does not contain — so measure on your
own recordings before concluding anything about quality.

Two caveats that do not depend on the corpus:

* **Russian only.** Configuring it as the global `asr.backend` alongside other
  languages is refused at startup, because a single-language model given other
  speech returns confident nonsense rather than an error.
* **Worse at code-switching.** An English word inside a Russian sentence tends
  to come back transliterated. If your speech mixes languages, Whisper may still
  be the better choice despite being weaker at Russian alone.

Licence: the upstream repository is MIT and permits commercial use. Some
third-party pages describe the weights as non-commercial; if that matters to
you, check the LICENSE in the model directory before relying on it.

### `[asr.deepgram]`

Used when `asr.backend = "deepgram"`.

| Field | Default | Notes |
|---|---|---|
| `api_key_env` | `"DEEPGRAM_API_KEY"` | Variable *name*, never a key |
| `model` | `"nova-2"` | |
| `endpoint` | Deepgram's | Point it at another Deepgram-shaped provider without a code change |
| `price_per_minute_usd` | `0.0043` | Reporting only. Check it against your own contract — list and negotiated pricing are rarely the same |

### `[asr.openai]`

Used when `asr.backend = "openai"`. It reuses the credential the translation and
plugin features already need, so there is no second account to set up.

Two endpoints sit behind this one backend: Balanced and Batch mode post each
VAD-closed segment to `/v1/audio/transcriptions`, while Live mode streams over
the Realtime WebSocket — the only genuinely streaming path of the two.

| Field | Default | Notes |
|---|---|---|
| `api_key_env` | `"OPENAI_API_KEY"` | Variable *name*, never a key |
| `base_url` | OpenAI | Any API-compatible gateway |
| `model` | `"gpt-4o-transcribe"` | Balanced and Batch mode |
| `realtime_model` | `"gpt-live-transcribe"` | Live mode |
| `realtime_url` | Realtime WS | Configurable because the transcription-session query string is not pinned in the published API |
| `realtime_delay` | `"low"` | `minimal` \| `low` \| `medium` \| `high` \| `xhigh` — the provider's own latency dial |
| `prompt` | `""` | Standing steering text, combined with the per-session vocabulary |
| `price_per_minute_usd` | published rates | Per-model table, used only to *report* spend |
| `fallback_price_per_minute_usd` | `0.006` | Used for a model absent from the table, so an unknown model reports something rather than nothing |

**Choosing a model is a real trade, not a preference:**

| Model | Word timings | Speaker labels | Use it when |
|---|---|---|---|
| `gpt-4o-transcribe` | no | no | the general default |
| `gpt-4o-mini-transcribe` | no | no | cost matters more than accuracy |
| `gpt-4o-transcribe-diarize` | no | **yes** | you want speakers from the recogniser rather than from `[diarization]` |
| `whisper-1` | **yes** | no | you want click-a-word-to-seek (FR-ASR-5) |

The backend reports these differences in its capabilities rather than claiming
everything, so the UI shows what you will actually get. With
`gpt-4o-transcribe-diarize`, the model's speaker labels are used in preference
to the pipeline's own clustering — otherwise you would be paying for a result
that gets discarded.

**Audio leaves your machine either way.** `privacy.local_only = true` refuses to
start with any cloud ASR backend, and the UI marks such sessions.

## `[diarization]`

| Field | Default | Notes |
|---|---|---|
| `enabled` | `true` | `false` gives unattributed transcripts, which is fine for dictation |
| `backend` | `"sherpa"` | `sherpa` \| `pyannote` \| `mock`. `pyannote` is more accurate and needs a Hugging Face token plus two gated licence acceptances |
| `min_speakers` / `max_speakers` | `null` | `null` infers the count (FR-DIA-2). Setting both to the same value pins it, which is the single most effective correction for a known group (FR-DIA-3) |
| `clustering_threshold` | `0.5` | Lower merges two people into one label; higher fragments one person across several |
| `window_ms` | `30000` | Balanced only. How much of the recent recording to diarize before recognising each segment. `0` turns it off |

**Why Balanced diarizes a window.** Batch diarizes the whole session at once,
which is the accurate answer. Balanced cannot — most of the session has not
happened yet — so before recognising a segment it diarizes the last
`window_ms` of audio. Two things depend on that and nothing else can supply
them:

* **whether a segment holds two people**, so the handover can be cut at the
  right word rather than the whole segment going to whoever spoke most;
* **whether a short interjection came from someone else.** An embedder needs
  about a second of audio before its vector means anything, so "угу" cannot be
  clustered at all — but a segmentation model reading half a minute around it
  can place it. Without this, a backchannel lands inside the message of
  whoever happened to be talking.

Identity still comes from embedding clustering, because a window's speaker
numbers only separate the voices inside that window and are not comparable
across windows. The window says *that* the speaker changed; the clusterer says
who they are.

It costs one diarizer pass per utterance, on top of recognition. Set it to `0`
on a machine that is already struggling to keep up, and attribution falls back
to embedding clustering alone — at the cost of both behaviours above.

## `[translation]`

| Field | Default | Notes |
|---|---|---|
| `enabled` | `true` | |
| `backend` | `"llm"` | `llm` \| `ctranslate2` \| `identity` |
| `context_utterances` | `6` | Rolling context (FR-TRA-3). Below about 4, pronoun and gender agreement in Slavic source text starts to fail — which is the whole reason this exists |
| `batch_max_utterances` | `4` | Batch short consecutive turns into one request (FR-TRA-9) |
| `batch_max_chars` | `240` | |
| `batch_max_delay_ms` | `600` | The ceiling that keeps batching from breaking the latency budget |
| `local_model` | Opus-MT | Local fallback. Permissively licensed, so shippable as a default |

## `[llm]`

One OpenAI-shaped client serves both the cloud and a local endpoint. Point
`base_url` at Ollama, vLLM, LM Studio, or llama.cpp and everything else is
unchanged — that is what makes "works with no internet" nearly free.

| Field | Default | Notes |
|---|---|---|
| `base_url` | OpenAI | `http://localhost:11434/v1` for Ollama |
| `model` | `"gpt-4.1-mini"` | |
| `api_key_env` | `"OPENAI_API_KEY"` | Ignored by local endpoints |
| `max_tokens` | `2048` | |
| `temperature` | `0.2` | |
| `timeout_s` | `60.0` | |
| `max_retries` | `3` | Exponential backoff with jitter (NFR-REL-4) |
| `price_per_1m_input_usd` | `0.40` | Used only to *report* spend. Wrong values cost accuracy, not money |
| `price_per_1m_output_usd` | `1.60` | |

## `[privacy]`

| Field | Default | Notes |
|---|---|---|
| `local_only` | `false` | Disables every outbound call. Cloud-dependent features report as unavailable rather than failing one by one (FR-CFG-5) |
| `session_cost_ceiling_usd` | `0.0` | `0` means none. Counts **recognition and translation together**, because an operator means one budget for the session. On reaching it both switch to local processing and the session reports the change rather than failing (FR-CFG-7). If no local backend is available it says so and continues — failing the session is explicitly what the requirement forbids |

### What a session costs

Every paid call is attributed to the session and to a component, so the session
view can say *what* the money went on rather than only how much:

```json
{"cost_usd": 0.0182, "cost_breakdown": {"asr": 0.0142, "translation": 0.0040}}
```

Recognition is billed on **audio sent to the provider**, at the published
per-minute rate. Two consequences worth knowing:

- **Live mode over a cloud recogniser costs several times Balanced mode.** The
  sliding window re-sends overlapping audio, so a ten-minute session can bill
  twenty-five minutes. The figure reported is audio *sent*, which is the honest
  one. Prefer Balanced on a cloud backend unless you need the latency.
- **`gpt-live-transcribe` is roughly three times `gpt-4o-transcribe`** per
  minute, before that multiplier.

**Every figure is an estimate**, computed from published list prices and audio
duration. A negotiated rate, a minimum billing increment, or a request that
failed after being billed will all make it differ from the invoice. The UI
labels it as such.

## `[plugins]`

| Field | Default | Notes |
|---|---|---|
| `directory` | `./plugins` | Drop-in `.py` files, loaded at startup |
| `enabled` | `null` | `null` means every discovered plugin. A list narrows it |
| `timeout_s` | `180.0` | Per handler invocation |

## `[audio]`

| Field | Default | Notes |
|---|---|---|
| `persist` | `true` | `false` means no playback and no re-processing |
| `codec` | `"opus"` | `opus` \| `wav` \| `flac`. Opus needs ffmpeg; without it you silently get WAV, and are told once |
| `bitrate_kbps` | `32` | ≈ 15 MB/hour, comfortably inside the 30 MB/h budget (NFR-RES-3) |

## Environment variables

Any field, with `__` between the sections:

```bash
DROID_DATA_DIR=/mnt/ssd/droid
DROID_SERVER__PORT=9000
DROID_ASR__BACKEND=deepgram
DROID_PRIVACY__LOCAL_ONLY=true
DROID_CAPTURE__LANGUAGES='["en","ru"]'     # lists are JSON
DROID_LOG_JSON=1                            # structured logs for journald
DROID_LOG_PIPELINE=debug                    # per-component level (NFR-MNT-3)
DROID_MODELS_DIR=/srv/shared-models         # weights are regenerable and large
```

## Changing configuration without a restart

`PATCH /api/config`, or Settings → Speech models and Settings → Backends, can
change the ASR model — overall and per language — the translation and LLM
backends, the target language, the default mode, local-only, and the cost
ceiling. These take effect **on the next session**; a running session keeps
the backend it started with, because swapping a model out from under a live
pipeline is exactly the surprise this feature exists to avoid (FR-CFG-8).

Log levels apply immediately. Everything else needs a restart, and the API says
which is which.

Per-language routing merges rather than replacing, so a client editing one row
does not have to send back the others; `""` removes an entry:

```bash
curl -X PATCH localhost:8000/api/config -H 'content-type: application/json' \
     -d '{"asr_by_language": {"ru": "gigaam:v3-rnnt"}}'
curl -X PATCH localhost:8000/api/config -H 'content-type: application/json' \
     -d '{"asr_by_language": {"ru": ""}}'      # back to the default
```
