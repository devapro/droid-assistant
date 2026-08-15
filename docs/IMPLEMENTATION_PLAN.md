# Implementation plan — droid-assistant

**Companion to:** [`SRS.md`](./SRS.md) v1.4
**Date:** 2026-08-15
**Status:** Draft for execution

This plan sequences the work by **risk and dependency**, not by the order things appear in the SRS. The ordering principle throughout: *build the thing that can invalidate the design first, and the thing that is merely tedious last.*

---

## Contents

1. [Blocking decisions](#1-blocking-decisions)
2. [Milestones at a glance](#2-milestones-at-a-glance)
3. [M0 — Decide and de-risk](#m0--decide-and-de-risk)
4. [M1 — Walking skeleton](#m1--walking-skeleton)
5. [M2 — Sessions and history](#m2--sessions-and-history)
6. [M3 — Speakers](#m3--speakers)
7. [M4 — Translation](#m4--translation)
8. [M5 — Plugins](#m5--plugins)
9. [M6 — Latency modes](#m6--latency-modes)
10. [M7 — Daily-use polish](#m7--daily-use-polish)
11. [M8 — Robustness and packaging](#m8--robustness-and-packaging)
12. [Repository layout](#12-repository-layout)
13. [Stack](#13-stack)
14. [Testing strategy](#14-testing-strategy)
15. [If you run out of time](#15-if-you-run-out-of-time)
16. [Scope discipline](#16-scope-discipline)

---

## 1. Blocking decisions

Three answers are needed before M0 finishes. Everything else can be decided as you go.

| # | Decision | Why it blocks | Default if you don't decide |
|---|---|---|---|
| D-1 | **Server hardware**: N100 mini PC (~$170) or Raspberry Pi 4 (~$60) | A Pi 4 cannot run Whisper above `base`, so it forces cloud ASR and changes the privacy posture, the cost model, and which spikes matter. See [SRS §6.3](./SRS.md#63-server-hardware-tiers) | N100 — it is the cheapest tier where the accuracy targets are reachable locally |
| D-2 | **Cloud ASR provider**, if any | Needed for Live mode, and mandatory on a Pi 4. Determines credentials, cost model, and one backend implementation | Deepgram — streaming, good Russian, simple API |
| D-3 | **Serbian reference audio** — do you have ~10 minutes you can label? | R1 cannot close without it, and R1 determines whether Serbian is a local or cloud-only language | Record it yourself during M0 |

---

## 2. Milestones at a glance

Effort is in **focused days** — roughly six hours of actual work, no meetings. Scale to your own availability; at two evenings plus a weekend per week, one focused day is about three calendar days.

| # | Milestone | Effort | Gate | Ships |
|---|---|---|---|---|
| **M0** | Decide and de-risk | 4–6 d | — | Hardware running, HTTPS working, eval harness, four spikes answered |
| **M1** | Walking skeleton | 6–9 d | M0 | Speak into a phone, see English text appear. Architecture proven |
| **M2** | Sessions and history | 6–8 d | M1 | Recordings persist, are browsable, playable, editable, searchable |
| **M3** | Speakers | 4–6 d | M1, R3 | Utterances attributed and renamable |
| **M4** | Translation | 3–5 d | M2 | Near-live RU/SR → EN alongside the original |
| **M5** | Plugins | 5–7 d | M2 | Plugin host plus summary and action-items |
| **M6** | Latency modes | 5–8 d | M4, R2 | Batch and Live alongside Balanced |
| **M7** | Daily-use polish | 6–9 d | M2 | Presets, pause, marking, notifications, share, pre-flight |
| **M8** | Robustness and packaging | 5–7 d | all | Offline capture, PWA, Docker, backups, docs |
|  | **Total** | **44–65 d** |  | v1 |

**Roughly 9–13 weeks full-time, or 5–7 months at a steady evenings-and-weekends pace.** This is a real project. [§15](#15-if-you-run-out-of-time) describes a useful subset at about half the cost.

---

## M0 — Decide and de-risk

**Goal:** answer the questions that could invalidate the architecture, before building on top of it.

Three of the SRS risks cannot close without measurement, and measurement needs a harness. So the evaluation harness is **the first code written**, not the last. It is standalone — audio files in, WER and DER out — and depends on nothing else in the system.

### Tasks

| Task | Notes |
|---|---|
| Acquire and set up the server | Install OS, `DROID_DATA_DIR` on a USB SSD (mandatory on Pi-class — SD cards fail under sustained write), systemd `Restart=always`, BIOS restore-on-AC-loss |
| Tailscale + HTTPS | `tailscale up`, `tailscale serve`. **Verify `getUserMedia` works from a phone on the resulting origin** — this gates every browser task in M1 |
| Record the evaluation corpus | ≥ 10 min labelled per language (EN, RU, SR), plus one 4-speaker desk recording and one table recording. **Record through a browser**, not a studio path (NFR-EVAL-5) |
| Build `eval/` | `run.py`, `wer.py`, `der.py`. Compare backends side by side, emit a table |
| **Spike R1 — Serbian** | `large-v3` vs `large-v3-turbo` vs cloud, WER per script. Decides whether Serbian is local or cloud-only |
| **Spike R13 — code-switching** | RU/EN mixed clip, auto-detect vs pinned vs cloud. Likely to bite harder than R1 for daily use |
| **Spike R3 — diarization** | DER on the 4-speaker table recording. Sets realistic expectations for the whole product |
| **Spike R2 — live latency** | LocalAgreement-2 over `faster-whisper`; median/p95 latency and hypothesis rewrite rate. Decides whether Live mode is achievable locally |
| **Spike R6 — browser audio processing** | WER and DER with echo cancellation, noise suppression, and AGC each on and off. Sets the defaults |

### Definition of done

- A phone browser reaches the server over HTTPS and is granted microphone permission
- `uv run droid-assistant eval` prints a backend comparison table
- Each of R1, R2, R3, R6, R13 has a **measured number** in the SRS risk table, not a guess
- The default ASR backend per language is chosen from data
- [SRS §4.2](./SRS.md#42-accuracy) accuracy targets are revised to match reality (NFR-EVAL-4)

> If R1 shows Serbian is unusable locally, that is a success for this milestone — you learn it in week one instead of week ten.

---

## M1 — Walking skeleton

**Goal:** the thinnest possible end-to-end path. Speak into a phone; English text appears in the browser. One language, Balanced mode, no persistence, no speakers, no translation, no plugins.

This is the milestone that proves the architecture. Everything afterwards is filling in known shapes.

### Server

- Repo scaffold: `pyproject.toml`, `uv` lock, package layout ([§12](#12-repository-layout)), ruff + mypy + pytest in CI
- `config.py` — pydantic settings from TOML plus environment, validated at startup with field-level errors (`FR-CFG-1`, `FR-CFG-2`)
- FastAPI app, static asset serving, `GET /api/health` (`FR-ASR-1` reporting)
- `WS /ws/ingest` — token-gated, sequence-numbered, acknowledging ([SRS §5.3](./SRS.md#53-audio-ingest-protocol), `NFR-SEC-3`)
- `pipeline/ringbuffer.py` — bounded, drops oldest, counts drops (`FR-SIG-1`)
- `pipeline/vad.py` — Silero ONNX, configurable padding (`FR-SIG-2`)
- `backends/asr/faster_whisper.py` behind `ASRBackend` (`FR-ASR-1`, `FR-ASR-2`)
- `WS /ws/sessions/{id}` — emits `utterance.final` (`FR-ASR-4`)

### Client

- Vite + React + TS + Tailwind scaffold, built into the Python package's static dir
- `capture/` — `getUserMedia` → `AudioWorklet` → resample to 16 kHz → 200 ms chunks (`FR-CAP-1`, `FR-CAP-3`, `FR-CAP-4`)
- **Screen Wake Lock** (`FR-CAP-7`) — 10 lines, and without it you cannot realistically test a 30-minute mobile session
- `transport/` — WebSocket client with sequence numbers
- Record screen: start/stop, level meter, live transcript list (`FR-UI-1`, `FR-UI-3`, `FR-UI-10`)

### Definition of done

- A 10-minute recording from a phone, over Tailscale, produces a live transcript with no dropped audio
- Killing the server mid-session does not hang the client
- Latency measured and recorded against `NFR-PERF-2`

> **Do not add anything else to M1.** The temptation to "just also store it in the database" is how walking skeletons become month-long milestones. The point is to prove the pipe.

---

## M2 — Sessions and history

**Goal:** recordings become durable objects you can find again. This is what turns a demo into a tool.

| Area | Work | Requirements |
|---|---|---|
| Storage | SQLite WAL, schema, migrations, `DROID_DATA_DIR` layout | [SRS §5.9](./SRS.md#59-storage-layout), `NFR-REL-5` |
| Lifecycle | Create, stop, disconnect grace period, crash recovery | `FR-SES-1` … `FR-SES-4` |
| Audio | Persist to Opus, range-request serving | `FR-SIG-3` |
| Buffering | IndexedDB spill, retransmit from first gap on reconnect | `FR-CAP-6`, `FR-CAP-15`, `NFR-REL-3` |
| History UI | Session list, filters, session detail, tabs | `FR-UI-7`, `FR-SES-13`, [SRS §5.1](./SRS.md#51-user-interface) |
| Playback | Player synced both directions with the transcript | `FR-UI-8` |
| Editing | Inline utterance edit, marked as edited, original retained | `FR-SES-8` |
| Search | FTS5 over utterances **and** artifacts | `FR-SES-10` |
| Multi-viewer | Laptop watches while phone records | `FR-SES-5` |
| Disk guard | Refuse to start below a threshold | `NFR-RES-7` |

### Definition of done

- Record on a phone, walk to a laptop, open the session, play it back, click a line and hear it
- Force-kill the server mid-session; the session survives with everything committed before the kill
- Disconnect the network for 60 s mid-session; no audio is lost (`NFR-PERF-7`)
- Search finds a phrase across 50+ sessions in under 500 ms

---

## M3 — Speakers

**Goal:** who said what. Gated on the R3 spike, because the measured DER determines how much UI affordance manual correction needs.

- `backends/diarization/sherpa.py` behind `DiarizationBackend` (`FR-DIA-1`, `FR-DIA-6`)
- **Store an embedding per utterance from the very first session** (`FR-DIA-5`) — costs nothing now, and is the only thing that makes retroactive naming possible in v2. Skipping it is the one decision here that is expensive to reverse
- Automatic speaker-count inference, plus an optional pinned count (`FR-DIA-2`, `FR-DIA-3`)
- Attribution timing per latency mode — partials carry no speaker (`FR-DIA-10`)
- Rename a speaker anywhere, applied across the session (`FR-DIA-4`)
- Colourblind-safe stable palette, label always present (`FR-UI-16`)
- Optional `pyannote` backend behind a flag, proving the abstraction (`NFR-MNT-1`)

**Definition of done:** a 4-person recording is attributed at or better than the DER measured in M0, and renaming one speaker updates the whole session.

---

## M4 — Translation

**Goal:** near-live RU/SR → EN beside the original.

- `backends/llm/openai_compat.py` — one client for both cloud OpenAI and a local endpoint (Ollama, vLLM, llama.cpp) via base URL ([SRS §6.2](./SRS.md#62-technology-choices-and-rejected-alternatives))
- Per-utterance translation with rolling context of the previous *N* utterances (`FR-TRA-3`) — this is what keeps pronouns and gender correct in Slavic source text, and it is the difference between usable and embarrassing
- Stream per utterance, never batch to session end (`FR-TRA-4`)
- Batch consecutive short utterances within a latency ceiling (`FR-TRA-9`)
- Skip utterances already in the target language (`FR-TRA-5`)
- Original / translation / side-by-side views (`FR-TRA-7`)
- Pending state visually distinct from failed (`FR-UI-14`)
- Local NMT fallback via CTranslate2 (`FR-TRA-6`)
- Per-session cost tracking and ceiling (`FR-CFG-7`)

**Definition of done:** a 20-utterance RU→EN sample reviewed by hand has correct pronoun and gender agreement where the referent sits in a prior utterance. Cost for a 60-minute 4-person session is measured and recorded (closes R10).

---

## M5 — Plugins

**Goal:** the extensibility that motivated the project.

- `plugins/api.py` — `Plugin`, `Context`, `Event`, `Artifact`, `api_version` ([SRS §5.5](./SRS.md#55-plugin-api-contract))
- `plugins/host.py` — entry-point and directory discovery, supervised tasks, per-handler timeouts, failure isolation (`FR-PLG-1` … `FR-PLG-4`)
- **Off the live path** (`FR-PLG-9`) — a 60-second plugin must not delay a single utterance
- Config schema → auto-rendered settings form (`FR-PLG-5`)
- Artifact versioning; re-run after a transcript edit (`FR-PLG-12`, `FR-SES-9`)
- `builtin/summary.py`, `builtin/action_items.py` (`FR-PLG-10`, `FR-PLG-11`)

**Definition of done:** a deliberately crashing plugin is installed; the session completes normally and the UI reports the plugin as failed. Writing a third plugin from the documentation alone takes under an hour.

---

## M6 — Latency modes

**Goal:** all three profiles, switchable. Gated on the R2 spike.

- `pipeline/modes.py` — a profile object parameterising the stage graph
- **Batch** first: buffer the session, process on stop. Easy, and it is the mode most likely to be used daily for meetings (`FR-LAT-6`)
- **Live** second: LocalAgreement-2 sliding window, partial hypotheses, in-place rewriting (`FR-LAT-4`). If R2 showed this is not achievable locally for RU/SR, ship it as cloud-ASR-only for those languages and document it
- Mid-session switching at a VAD boundary (`FR-LAT-3`)
- Mode's cost and quality implication shown in the UI (`FR-LAT-7`)

**Definition of done:** 20 consecutive mid-session mode switches lose no finalised utterance (closes R8).

---

## M7 — Daily-use polish

**Goal:** the difference between "works" and "you actually use it every day." Individually small, collectively decisive.

| Feature | Requirement | Why it matters daily |
|---|---|---|
| Session presets | `FR-SES-14` | Removes three decisions from every single recording |
| Pause / resume | `FR-CAP-16` | Breaks and side conversations are routine |
| Pre-flight check | `FR-UI-18` | Never again discover after 40 minutes that the mic was muted |
| Mark this moment | `FR-CAP-18` | One tap instead of re-listening to 40 minutes |
| Completion notification | `FR-UI-17` | Makes "record, walk away" actually work |
| Copy / share artifacts | `FR-UI-19` | The commonest action should not be a file download |
| Autoscroll pin and release | `FR-UI-13` | Reading back during a live session without being yanked |
| Empty states | `FR-UI-15` | First-run impression |
| Backend hot-swap | `FR-CFG-8` | Removes a restart from every model experiment |
| String externalisation | `FR-UI-20` | v1 ships English; the structure allows RU/SR later |
| Mic check | [SRS §8.4](./SRS.md#84-recording-well) | Placement affects results more than any setting in the app |

**Definition of done:** record a real meeting end to end without touching Settings once, and get the summary to a colleague in two taps.

---

## M8 — Robustness and packaging

**Goal:** survives reality; someone else can install it.

- Offline capture with deferred sync, service-worker-cached shell (`FR-CAP-17`) — with an always-on server this is a *should*, covering reboots and being out of Tailscale reach
- PWA installability (`FR-UI-10`)
- Docker Compose, CPU and CUDA variants; native `uv` path; systemd unit (`NFR-PORT-1`, `NFR-PORT-2`)
- `droid-assistant doctor` — models, GPU, disk, HTTPS reachability ([SRS §8.3](./SRS.md#83-first-run-checklist))
- Scheduled backup of `$DROID_DATA` (`NFR-REL-7`)
- 4-hour soak test (`NFR-REL-1`)
- Documentation: install, HTTPS, recording well, plugin authoring, legal notice (`NFR-LEG-2`)
- Client bundle ≤ 500 KB gzipped (`NFR-RES-6`)

**Definition of done:** a clean machine goes from `git clone` to a working recording in under 30 minutes, following only the README.

---

## 12. Repository layout

```
droid-assistant/
├── pyproject.toml
├── compose.yml  compose.cuda.yml
├── docs/            SRS.md · IMPLEMENTATION_PLAN.md · srs.html
├── scripts/         build_srs_page.py
├── src/droid_assistant/
│   ├── cli.py                    serve · models download · doctor · eval
│   ├── config.py                 pydantic settings, TOML + env
│   ├── api/
│   │   ├── app.py
│   │   ├── routes/               sessions · plugins · search · health
│   │   └── ws/                   ingest · events
│   ├── pipeline/
│   │   ├── ringbuffer.py  vad.py  segmenter.py
│   │   ├── modes.py              Live · Balanced · Batch profiles
│   │   └── orchestrator.py
│   ├── backends/
│   │   ├── asr/                  base · faster_whisper · whisper_cpp · deepgram
│   │   ├── diarization/          base · sherpa · pyannote
│   │   ├── translation/          base · llm · ctranslate2
│   │   └── llm/                  base · openai_compat
│   ├── store/                    db · models · search · audio
│   └── plugins/
│       ├── api.py  host.py
│       └── builtin/              summary · action_items
├── eval/            run.py · wer.py · der.py · corpus/
├── plugins/         user drop-in directory
└── web/
    └── src/
        ├── capture/              worklet · resampler · buffer · wakelock
        ├── transport/            ws client · retransmit
        ├── views/                Record · History · SessionDetail · Settings
        ├── components/
        └── state/
```

Every `backends/*/base.py` defines the Protocol; every sibling implements it. `NFR-MNT-1` requires at least two implementations each, which is what keeps the abstractions honest.

---

## 13. Stack

| Layer | Choice |
|---|---|
| Server | Python 3.12, FastAPI, uvicorn, `uv` |
| Validation / config | pydantic v2, TOML |
| VAD | Silero (ONNX Runtime) |
| ASR | `faster-whisper` (CTranslate2); cloud backend per D-2 |
| Diarization | `sherpa-onnx`; `pyannote` optional |
| LLM | `openai` SDK against OpenAI or any OpenAI-compatible endpoint |
| Storage | SQLite WAL + FTS5 + `sqlite-vec`; Opus audio on disk |
| Client | React 19, Vite, TypeScript, Tailwind |
| Client audio | `getUserMedia` + `AudioWorklet` + IndexedDB + Wake Lock |
| Quality | ruff, mypy, pytest, Playwright, `tsc`, eslint |
| Packaging | Docker Compose, systemd, `uv` |

---

## 14. Testing strategy

| Level | What | When |
|---|---|---|
| Unit | Ring buffer, resampler, segmenter, retransmit logic, plugin supervision | Continuously |
| Contract | Every backend Protocol tested against all its implementations with one shared suite | From M3 |
| Integration | Synthetic audio file → ingest → full pipeline → expected utterances, no browser | From M1 |
| Browser | Playwright: permission flow, reconnect, autoscroll pin, wake lock, pause | From M2 |
| Accuracy | The `eval/` harness in CI against the corpus, tracking WER and DER over time | From M0 |
| Soak | 4-hour session, memory and drop counters | Before M8 exit |

The accuracy harness is the important one. Without it, "did that change make transcription better?" is unanswerable, and every model decision becomes taste.

---

## 15. If you run out of time

A useful subset, in priority order. **M0 → M1 → M2 → M4 → a slice of M7** gives you a tool that records, transcribes, translates, and lets you find things again — roughly **20–28 focused days**, or half the full plan.

What that leaves out, and what it costs:

| Deferred | Consequence |
|---|---|
| M3 speakers | Transcripts are unattributed walls of text. Painful for meetings, fine for 1:1 and dictation |
| M5 plugins | No summaries or action items — you read the transcript yourself |
| M6 Live mode | Balanced only, 2–10 s latency. Honestly adequate for most use |
| M8 offline capture | A server reboot during a meeting loses that meeting |

If you defer M3, **still store embeddings** — it is a column and a function call, and adding it later means never being able to attribute historical sessions.

---

## 16. Scope discipline

Things that will look tempting mid-build and should be refused:

- **A native mobile app.** Permanently out of scope ([SRS §7](./SRS.md#7-decision-record-where-does-the-computation-run)). The evidence is in the spec; re-litigating it costs weeks
- **On-device ASR.** Same
- **Text-to-speech.** Explicitly excluded from the product
- **Multi-user accounts.** Tailscale is the access control for v1
- **A second frontend framework** for the "recording view specifically"
- **Rewriting the plugin API before three plugins exist.** You do not know its shape yet
- **Postgres.** SQLite is correct until there is a second concurrent user

And one to accept rather than fight: **transcription will be imperfect**, especially for Serbian. Build the correction affordances — transcript editing, plugin re-run, custom vocabulary — rather than chasing a WER that the models cannot currently deliver.
