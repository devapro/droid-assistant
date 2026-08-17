# droid-assistant

Open a web page on your phone, press record, and watch a speaker-attributed,
optionally translated transcript appear live. Recognition, diarization,
translation, and plugins run on a server you own.

The phone is a microphone and a screen. The server is the brain. There is no
native app and there will not be one — the reasoning is recorded in
[SRS §7](docs/SRS.md#7-decision-record-where-does-the-computation-run).

- **Live transcription** in three latency modes — Live, Balanced, Batch
- **Speaker diarization** with no enrollment, renamable across a session
- **Near-live translation** with rolling conversational context, so pronouns and
  gender survive the trip out of Russian and Serbian
- **Plugins** that receive conversation events and produce artifacts; summary
  and action-items ship as references
- **Your own prompts** for a summary, written once in Settings and chosen per
  recording
- **Capture the room or the machine** — a microphone, the audio playing on the
  device running the browser (a video call, a recording), or both mixed
- **Local-first**: nothing leaves the machine unless you enable a cloud backend
  (Deepgram or OpenAI transcription, both optional)

---

## Install

```bash
git clone https://github.com/arsenii/droid-assistant
cd droid-assistant
cp .env.example .env          # set OPENAI_API_KEY for translation and plugins
docker compose up -d          # or: docker compose -f compose.cuda.yml up -d
```

Native, without Docker:

```bash
uv sync --extra local --extra cloud
uv run droid-assistant models download
uv run droid-assistant serve
```

Then **read the next section**, because on any device other than the server
itself the app cannot access a microphone until you do.

## HTTPS is not optional

Browsers refuse microphone access outside a secure context. `http://localhost`
counts, so a browser on the server works immediately — every other device needs
real HTTPS.

```bash
tailscale up
tailscale serve --bg 8000
# → https://<machine>.<tailnet>.ts.net
```

That issues a genuine certificate, needs no port forwarding, works from outside
your LAN, and provides the access control this project deliberately does not
implement itself. Alternatives are in [docs/INSTALL.md](docs/INSTALL.md).

Check everything at once:

```bash
uv run droid-assistant doctor
```

## Hardware

The server is expected to be a small always-on machine.

| Tier | Hardware | Local ASR ceiling | Modes that hold up without cloud |
|---|---|---|---|
| A | Apple Silicon Mac, or x86-64 + NVIDIA ≥ 6 GB | `large-v3-turbo` at realtime | all three |
| **B — recommended** | Intel N100/N150 mini PC, 16 GB (~$170) | `small`/`medium` int8 | Balanced, Batch |
| C | Raspberry Pi 5 | `small` int8, around realtime | Batch |
| D | Raspberry Pi 4 | `tiny`/`base` only | **none — cloud ASR required** |

That last column is a recommendation, not a restriction. Live is a sliding
window over the same batch recogniser rather than a separate engine, so every
mode is offered on every tier and none of them is refused for want of a
streaming backend. What the tier decides is whether recognition keeps up: Live
re-decodes the last few seconds about once a second, so it wants roughly three
times realtime from the model, and recognition that falls behind stays behind
for the rest of the session. Below tier A, try Live with a smaller model than
the ceiling — the numbers that would turn this column into a measurement are
still open (SRS R2).

A Pi 4 is an excellent always-on host and a poor inference host. It will run the
web app, ingest, storage, and plugins comfortably, but Russian accuracy at
`base` is weak and Serbian is unusable, so it implies cloud ASR. That is a
supported configuration; it is simply a different trade.

**Do not keep the database on a Pi's SD card.** Attach a USB SSD and point
`DROID_DATA_DIR` at it. SD cards fail under sustained write and take the history
with them.

## Recording well

This affects results more than any setting in the app.

- Get the microphone close. 1.5 m is much worse than 0.5 m.
- For a meeting that matters, use a laptop with a USB microphone — the browser
  offers it like any other input, and it recovers most of the accuracy lost to a
  phone on a table.
- Keep the screen on. The app holds a Wake Lock; if the browser does not support
  one, it says so.
- **Turn off noise suppression for multi-speaker recordings.** It is tuned for a
  single near voice and will suppress the quieter people at the table. It is off
  by default here for that reason.
- Record 30 seconds and check the transcript before a session you cannot repeat.

**For an online call, capture the machine instead of the room.** Set *Capture
from* to "Audio playing on this machine" and share the call's browser tab with
audio. That takes the remote voices before they become sound in your room —
skipping the loudspeaker, the room, and the microphone — and transcribes them
about as well as someone sitting beside you. Choose "Both, mixed" when you are
speaking in the call yourself.

Chrome and Edge on a desktop can do this. Firefox and Safari cannot. On macOS
only *tab* audio is available, not whole-screen audio — that is an OS
limitation, not one of this project.

## Marking what matters

Two different things, and it is worth knowing which you want.

**A mark** is yours: *"come back to this"*. Instant, free, no model involved.
Press **Mark** while recording to flag whatever is being said, or click the flag
on any line at any time — during the recording or a month later. Marked lines
are tinted in the transcript, and the history list says how many each recording
holds, so you can find the one you are thinking of without opening all of them.

**An action item** is a task somebody committed to. Click the checkbox on a line
and the `action_items` plugin writes it up — owner and due date if the line names
them — and adds it to that recording's list. It needs an LLM, so it costs a call
and, on a cloud credential, sends that line to a provider. Lines already on the
list show a filled checkbox.

Marks are the cheap one. If you are not sure which you want mid-conversation,
mark it and decide later.

## Summaries in your own words

A summary is generated when you ask for one — it costs a call over the whole
transcript, and most recordings are never opened twice. Open a recording, and the
**Summary** tab has the button.

The built-in prompt writes bullets, prose, or minutes. That covers the common
cases and none of the specific ones, so you can write your own:

> **Settings → Prompts → New prompt**
>
> *Customer call* — "Summarise this for the account team. Lead with what the
> customer asked for, then what we committed to, then anything left open. Quote
> figures exactly. Write in the language of the call."

It then appears in a picker beside **Generate**, and beside **Re-run** on a
summary that already exists — so a summary in the wrong shape is one choice and
one click from the right one. Each summary records which prompt produced it, by
name, and keeps saying so after that prompt is edited or deleted.

Two things a prompt cannot switch off: the instruction not to invent anything the
transcript does not support, and the word ceiling in the plugin's own settings.
The first is what keeps a summary trustworthy; the second is what bounds the
bill. Everything else — shape, headings, ordering, output language — is yours.
Any plugin can offer this; see
[docs/PLUGINS.md](docs/PLUGINS.md#taking-a-prompt-fr-plg-14).

## Accuracy, honestly

Transcription is imperfect, and more so away from English. The targets in
[SRS §4.2](docs/SRS.md#42-accuracy) are targets pending measurement against
your own recordings, not promises — run `droid-assistant eval` against the
corpus in `eval/corpus/` to get real numbers for your hardware and your voices.

**Model size matters far more away from English.** Whisper's multilingual
capacity is heavily weighted towards English, and the gap widens sharply as the
model shrinks. Measured with `droid-assistant eval` on a small Russian set:

| Model | English WER | Russian WER |
|---|---|---|
| `tiny` | 0.0% | 19.7% |
| `base` | 0.0% | 9.8% |
| `small` | 0.0% | 4.9% |
| `large-v3-turbo` | 0.0% | 3.3% |

English is unaffected by size here; Russian is transformed by it — a sixfold
difference between the smallest and largest. So if Russian
looks much worse than English, **check which model you are running first** —
`GET /api/health` reports it. Those figures come from clean synthetic speech and
are optimistic in absolute terms; the *ratio* between sizes is the point.

If the stock model is still not good enough for your language, use a different
one **for that language only**:

```toml
[asr.by_language.ru]
backend = "gigaam"        # Russian-specialised, local, ~20x faster than turbo
```

`droid-assistant models gigaam v3-rnnt` fetches it. Other languages keep using
whatever `asr.backend` says. `droid-assistant models suggest --language ru` also
lists Whisper fine-tunes. Measure any of them against your own recordings rather
than trusting anyone's benchmark — including the one above.

Serbian is the largest open question — there is no equivalent of GigaAM for it,
only community fine-tunes nobody has benchmarked on real conversation, and one
cloud model that can do it in Live mode. See
[Serbian](docs/CONFIGURATION.md#serbian) for the options and their caveats.

Russian–English code-switching is likely to bite harder in daily use: speech
recognition detects **one language per window**, so a sentence that mixes
languages will come out in whichever one the model picks. Pin a single language per session where you can. Note that a
language-specialised model usually makes code-switching *worse*, not better —
it is tuned to expect one language.

The product answer to all of this is correction, not denial: transcripts are
editable, plugins re-run over the corrected text, and custom vocabulary is
per session.

## Documentation

| | |
|---|---|
| [docs/SRS.md](docs/SRS.md) | The specification this implements |
| [docs/IMPLEMENTATION_PLAN.md](docs/IMPLEMENTATION_PLAN.md) | Milestones and sequencing |
| [docs/INSTALL.md](docs/INSTALL.md) | Install, HTTPS, always-on setup, backups |
| [docs/CONFIGURATION.md](docs/CONFIGURATION.md) | Every configuration field |
| [docs/PLUGINS.md](docs/PLUGINS.md) | Writing a plugin |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | How the pieces fit |

## Recording law

**Recording law varies by jurisdiction, and the operator — not this software —
is responsible for lawful use.** Roughly a dozen US states require all-party
consent. Germany criminalises recording confidential speech under §201 StGB.
Serbia, Russia, and the EU each impose their own constraints.

This project ships no feature designed to conceal that recording is taking
place, and will not.

## Security

v1 has **no user authentication**. Access control is delegated to the network
layer, which in practice means Tailscale. Do not expose an instance to the
public internet without a reverse proxy that authenticates.

**Plugins run in-process with full server privileges.** Installing a plugin is
equivalent to running arbitrary code on the server. Read it first.

## Licence

Apache-2.0. Optional non-commercially-licensed models (NLLB-200) are opt-in and
never a default.
