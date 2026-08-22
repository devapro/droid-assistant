# Software Requirements Specification — droid-assistant

**Version:** 1.4 (draft)
**Date:** 2026-08-14
**Status:** For review
**Project:** `droid-assistant` — a self-hosted conversation intelligence agent with a browser-based client

> **Changes in 1.4.** The server is now specified as a **dedicated always-on machine** ([§6.3](#63-server-hardware-tiers), [§8.5](#85-running-it-always-on)), which closes R12. Hardware tiers restructured around how much of the pipeline runs locally, with Raspberry Pi 4 documented honestly as a host that implies cloud ASR. Adds pause/resume (FR-CAP-16), offline capture with deferred sync (FR-CAP-17), moment marking (FR-CAP-18), session presets (FR-SES-14), search over artifacts (FR-SES-10), completion notifications (FR-UI-17), a pre-flight check (FR-UI-18), clipboard/share (FR-UI-19), string externalisation (FR-UI-20), cost tracking (FR-CFG-7), backend hot-swap (FR-CFG-8), and translation batching (FR-TRA-9). Fixes an internal contradiction: partial hypotheses cannot carry a speaker label (FR-DIA-10), and the §5.1 wireframe is corrected. New risk R13 covers Russian–English code-switching.
>
> **Changes in 1.3.** Added [§5.1](#51-user-interface), a screen-level user-interface specification covering the recording view, history, session detail, and settings, with wireframes, states, and interaction rules. Former §5.1–5.8 shift down by one. Five interface requirements added (FR-UI-12 … FR-UI-16).
>
> **Changes in 1.2.** The LLM provider is OpenAI, via the `openai` SDK; the OpenAI-compatible request shape doubles as the local-model path (Ollama, vLLM, llama.cpp). Privacy and data-handling features are out of scope for now: retention policies, cloud-egress redaction, at-rest encryption documentation, per-session data-egress display, and per-person deletion are removed. Client buffer cleanup survives as FR-CAP-15 because it is a storage-correctness requirement, not a privacy one. [§4.5](#45-consent-and-legal) is reduced to the three items that cost no implementation work.
>
> **Changes in 1.1.** Capture moved from the server to the browser client. Native Android is out of scope permanently. HTTPS is now a v1 requirement rather than a phase-2 option. Accuracy targets are split by microphone condition and revised downward for near-field capture. See [§7](#7-decision-record-where-does-the-computation-run) for the reasoning.

---

## Table of contents

1. [Introduction](#1-introduction)
2. [Overall description](#2-overall-description)
3. [Functional requirements](#3-functional-requirements)
4. [Non-functional requirements](#4-non-functional-requirements)
5. [External interfaces](#5-external-interfaces)
6. [Architecture and technology](#6-architecture-and-technology)
7. [Decision record: where does the computation run?](#7-decision-record-where-does-the-computation-run)
8. [Deployment](#8-deployment)
9. [Roadmap](#9-roadmap)
10. [Risks and open questions](#10-risks-and-open-questions)
11. [Appendices](#11-appendices)

---

## 1. Introduction

### 1.1 Purpose

This document specifies the requirements for **droid-assistant**, a self-hosted system in which a user opens a web page on any device, presses record, and watches a speaker-attributed, optionally translated transcript appear live. An extensible plugin system runs over the resulting conversation to produce summaries, action items, and anything else a plugin author writes.

It is written for the project maintainer, prospective contributors, and anyone deciding whether to deploy the system.

### 1.2 Scope

**In scope**

- Audio capture **in the browser**, on phone or desktop, streamed to a self-hosted server
- Speech-to-text with configurable or automatic language detection
- Speaker diarization (segmenting *who spoke when*)
- Translation of transcribed text into a target language, near-live
- Session persistence, browsing, editing, and search
- A plugin system that receives conversation events and produces artifacts
- A single web application serving as recorder, live viewer, session browser, and settings UI
- Deployment as an open-source project other people can install and run

**Out of scope — permanently**

- **A native mobile application.** The web client is the only client. See [§7](#7-decision-record-where-does-the-computation-run)
- **On-device speech recognition.** All ASR happens on the server
- Text-to-speech or spoken translation output
- Server-side audio device capture (no USB microphone attached to the server)
- Video capture or analysis
- Multi-tenant SaaS hosting, billing, or account management

**Out of scope for v1, planned later**

- Speaker identification by name via voice enrollment (v2)
- Semantic search across sessions (v2)
- Meeting-platform bot integration

### 1.3 Definitions

| Term | Meaning |
|---|---|
| **ASR** | Automatic Speech Recognition — audio to text |
| **Diarization** | Segmenting audio by speaker; produces anonymous labels (`Speaker 1`, `Speaker 2`) |
| **Speaker identification** | Mapping a diarized segment to a *named* person via an enrolled voiceprint |
| **VAD** | Voice Activity Detection — deciding whether a frame contains speech |
| **Utterance** | A contiguous speech segment from one speaker, bounded by VAD endpoints |
| **WER** | Word Error Rate — ASR accuracy metric (lower is better) |
| **DER** | Diarization Error Rate — speaker-attribution accuracy metric (lower is better) |
| **Session** | One recording from start to stop, with its transcript, speakers, and artifacts |
| **Artifact** | A plugin output attached to a session (summary, action item list, …) |
| **Latency mode** | One of three pipeline profiles trading latency against accuracy and cost |
| **Secure context** | A browser origin permitted to use privileged APIs — HTTPS, `localhost`, or `127.0.0.1` |
| **Wake Lock** | A browser API that prevents the screen from sleeping, keeping a recording tab alive |
| **Desk condition** | Client device within ~0.5 m of the speaker — a laptop or a phone held close |
| **Table condition** | Client device ~1.5 m from several speakers — a phone lying on a meeting table |

### 1.4 References

- W3C Secure Contexts, Media Capture and Streams, Web Audio (AudioWorklet), and Screen Wake Lock specifications
- Macháček, Dabre & Bojar, *Turning Whisper into Real-Time Transcription System* (2023) — the LocalAgreement-2 streaming policy
- Bredin et al., `pyannote.audio` speaker diarization pipelines
- IEEE 830-1998 (structure, loosely followed)

---

## 2. Overall description

### 2.1 Product perspective

droid-assistant is a **thin client / self-hosted server** application. The browser captures audio and renders results; a server the operator controls performs all recognition, diarization, translation, and plugin execution, and stores everything.

The division is deliberate and is the central architectural finding of this specification: **no phone can currently run good-quality live speech recognition for Russian and Serbian with speaker diarization**, so the phone is a microphone and a screen, not a brain ([§7](#7-decision-record-where-does-the-computation-run)).

It is not a meeting-platform bot. It hears what a person in the room hears, which is why it works for an in-person meeting, a face-to-face translated conversation, a dictated note, and an online call played through room speakers, with no per-platform integration.

### 2.2 User classes

| Class | Description | Technical level | Priority |
|---|---|---|---|
| **Operator** | Installs, configures, and runs the server. Owns the hardware and the data | Comfortable with Docker or a Python toolchain | Primary |
| **Recorder** | Opens the web page and records. May be the operator or someone they've given access to | None | Primary |
| **Participant** | Anyone whose speech is captured. Has no account, but has consent rights | None | Primary (legally) |
| **Plugin author** | Writes Python plugins against the event API | Python developer | Secondary |
| **Contributor** | Extends the core: ASR backends, UI, pipeline stages | Python + TypeScript developer | Secondary |

### 2.3 Operating environment

**Server**

- Linux (x86-64, arm64) or macOS (Apple Silicon, Intel). Windows best-effort
- Python 3.12+
- Reachable from client devices over HTTPS (see [§8.2](#82-https-is-mandatory))
- Internet access required only when cloud ASR or cloud LLM features are enabled

**Client**

| Browser | Capture | Wake Lock | Verdict |
|---|---|---|---|
| Chrome / Edge, desktop | Yes | Yes | Fully supported |
| Chrome, Android | Yes | Yes | Fully supported |
| Firefox, desktop | Yes | Yes | Fully supported |
| Safari, macOS 14+ | Yes | Yes | Supported |
| Safari, iOS 16.4+ | Yes | Yes | **Degraded** — aggressive background suspension; recording requires the tab to stay foregrounded |
| Any browser over plain HTTP on a LAN IP | **No** | — | `getUserMedia` is unavailable outside a secure context |

### 2.4 Design constraints

| ID | Constraint |
|---|---|
| C-1 | Audio capture happens in the browser. The server has no audio device |
| C-2 | The server must be reachable over HTTPS, or via `localhost` when the browser runs on the server itself |
| C-3 | The system must function with no internet connection — server and client on the same LAN, local ASR, no cloud LLM — at reduced capability and with no code changes |
| C-4 | Raw audio must never leave the operator's server unless they explicitly enable a cloud ASR backend |
| C-5 | The plugin API must be stable enough that a third-party plugin survives a minor version bump |
| C-6 | Default dependencies must carry licenses compatible with an open-source release. Non-commercial models (e.g. NLLB-200) ship only as clearly-labelled opt-ins |
| C-7 | Everything must install and run without a GPU, at reduced capability |
| C-8 | No native mobile application will be built. Browser limitations are mitigated, not escaped |

### 2.5 Assumptions

| ID | Assumption | If false |
|---|---|---|
| A-1 | The server is a dedicated always-on machine that boots on power and starts the service automatically ([§6.3](#63-server-hardware-tiers)) | Recording becomes unreliable in exactly the moments it matters; FR-CAP-17 (offline capture) moves from *should* to *must* |
| A-2 | Sessions are typically 15–120 minutes | Client-side buffering and storage budgets need revisiting |
| A-3 | Typical group size is 2–6 speakers | Diarization targets in [§4.2](#42-accuracy) do not hold beyond ~8 speakers |
| A-4 | Recording usually happens on wifi, not metered mobile data | Bandwidth becomes a primary constraint; the Opus transport mode ([§5.7](#57-audio-transport)) becomes mandatory rather than optional |
| A-5 | The operator is legally permitted to record the conversations they capture | See [§4.5](#45-consent-and-legal) — the system assists but cannot guarantee this |

---

## 3. Functional requirements

Priority follows MoSCoW: **M**ust, **S**hould, **C**ould, **W**on't (this release). Milestone indicates the target version.

### 3.1 Client capture

| ID | Priority | Milestone | Requirement | Acceptance criteria |
|---|---|---|---|---|
| FR-CAP-1 | M | v1 | The client shall capture microphone audio via `getUserMedia` after an explicit user gesture | Pressing Record prompts for permission on first use; audio frames reach the server within 1 s |
| FR-CAP-2 | M | v1 | The client shall let the user choose among available input devices where the browser exposes them | A laptop with a built-in mic and a USB mic offers both; the selection persists across sessions |
| FR-CAP-3 | M | v1 | The client shall resample captured audio to 16 kHz mono before transmission | A 48 kHz stereo device produces a 16 kHz mono stream, verified server-side |
| FR-CAP-4 | M | v1 | Audio processing shall use an `AudioWorklet`, not the deprecated `ScriptProcessorNode` | No `ScriptProcessorNode` appears in the shipped bundle; capture does not block the main thread |
| FR-CAP-5 | M | v1 | The client shall stream audio to the server over WebSocket with sequence numbers | The server detects a gap in sequence numbers and requests retransmission |
| FR-CAP-6 | M | v1 | The client shall buffer audio locally when the connection drops, and resume without losing the gap | Disabling the network for 30 s mid-session and restoring it produces a transcript with no missing audio |
| FR-CAP-7 | M | v1 | The client shall acquire a Screen Wake Lock while recording, and release it on stop | On Android Chrome, a 30-minute recording with no interaction completes without the screen locking or capture stopping |
| FR-CAP-8 | M | v1 | The client shall warn the user when Wake Lock is unavailable and explain the consequence | On a browser without Wake Lock support, a notice states that the screen must stay on |
| FR-CAP-9 | M | v1 | The client shall detect that capture has stopped unexpectedly and surface it prominently | Revoking mic permission mid-session shows an unmissable error, and the session is preserved up to that point |
| FR-CAP-10 | M | v1 | The client shall display live input level metering | A silent room shows a resting meter; speech moves it within 200 ms |
| FR-CAP-11 | S | v1 | The client shall let the user disable browser-native echo cancellation, noise suppression, and auto-gain per session | All three constraints are settable; the chosen values are recorded in session metadata |
| FR-CAP-12 | S | v1 | The client shall warn before starting a long session on a metered connection | On a connection reporting as metered, a notice shows the estimated data use per hour |
| FR-CAP-13 | C | v2 | The system shall accept an uploaded audio file and process it as a batch session | Uploading WAV/MP3/M4A produces a complete session with transcript and artifacts |
| FR-CAP-14 | W | — | Server-side audio device capture | Withdrawn in v1.1 — the server has no microphone (C-1) |
| FR-CAP-15 | M | v1 | Locally buffered audio shall be released once the server acknowledges it, and cleared entirely when a session ends or is abandoned | After a session ends, no audio for it remains in browser storage; a 4-hour session never exceeds the buffer cap (NFR-RES-5) |
| FR-CAP-16 | M | v1 | Recording shall be pausable and resumable within one session, without ending it | Pausing for 5 minutes and resuming produces one session whose transcript contains no utterances from the paused interval and whose timeline reflects the gap |
| FR-CAP-17 | S | v1 | When the server is unreachable, the client shall still record, storing audio locally and uploading it once the server returns | With the server stopped, pressing Record starts a local session; restarting the server uploads it and produces a complete transcript. Requires the app shell to be service-worker cached so it loads without the server |
| FR-CAP-18 | S | v1 | The user shall be able to mark the current moment during recording with one interaction | Tapping Mark flags the utterance in progress; flagged utterances are visually distinct and filterable in the session view |

### 3.2 Server-side signal processing

| ID | Priority | Milestone | Requirement | Acceptance criteria |
|---|---|---|---|---|
| FR-SIG-1 | M | v1 | The server shall buffer incoming audio in a bounded ring buffer that drops oldest data rather than growing without limit | Under a stalled consumer, memory stays within the configured buffer size; drops are counted and logged |
| FR-SIG-2 | M | v1 | The server shall apply VAD and forward only speech regions to ASR, with configurable pre/post padding | On a 10-minute stream containing 2 minutes of speech, ASR runs on ≤ 3 minutes of audio |
| FR-SIG-3 | S | v1 | The server shall persist session audio to disk in a configurable codec | A completed session yields a playable file whose duration matches the session ± 1 s |
| FR-SIG-4 | S | v1 | The server shall report per-stage processing latency per utterance | Every utterance carries timings for VAD, ASR, diarization, and translation |

### 3.3 Speech recognition

| ID | Priority | Milestone | Requirement | Acceptance criteria |
|---|---|---|---|---|
| FR-ASR-1 | M | v1 | ASR shall run behind a pluggable backend interface selected by configuration | Changing backend in config and restarting changes the engine with no code change; `GET /api/health` reports the active backend |
| FR-ASR-2 | M | v1 | At least one fully local backend shall ship by default and require no network access | With the server's internet disabled, a session transcribes successfully |
| FR-ASR-3 | M | v1 | At least one cloud streaming backend shall be supported | With credentials configured, a session transcribes via cloud and the UI indicates audio is leaving the server |
| FR-ASR-4 | M | v1 | Each utterance shall carry start and end timestamps relative to session start | Every utterance has `start_ms < end_ms`, monotonically non-decreasing across the session |
| FR-ASR-5 | S | v1 | Word-level timestamps shall be produced where the backend supports them | Clicking a word seeks playback to within 300 ms of it |
| FR-ASR-6 | M | v1 | Each utterance shall carry a language code (BCP-47), detected or configured | A session mixing Russian and English yields utterances tagged `ru` and `en` |
| FR-ASR-7 | M | v1 | The user shall be able to pin expected languages per session | A session pinned to `sr` never emits utterances tagged as another language |
| FR-ASR-8 | S | v1 | The system shall accept a per-session custom vocabulary and pass it to the backend | With "Arsenii" in the vocabulary, that name transcribes correctly in ≥ 80% of occurrences on the eval clip, against a measured baseline |
| FR-ASR-9 | S | v1 | Known hallucination patterns on silence and non-speech shall be suppressed | On 5 minutes of room tone, zero utterances are emitted |
| FR-ASR-10 | M | v1 | Serbian output shall be normalised to a configured script (Cyrillic or Latin) | A Serbian session configured for Latin contains no Cyrillic characters |
| FR-ASR-11 | S | v2 | A per-utterance confidence score shall be reported | Every utterance carries a confidence in `[0,1]`; the UI marks those below a configurable threshold |

### 3.4 Latency modes

| ID | Priority | Milestone | Requirement | Acceptance criteria |
|---|---|---|---|---|
| FR-LAT-1 | M | v1 | Three latency modes shall be supported: **Live**, **Balanced**, and **Batch** | All three produce a complete transcript |
| FR-LAT-2 | M | v1 | The mode shall be selectable before a session starts | The chosen mode is applied and recorded in session metadata |
| FR-LAT-3 | S | v1 | The mode shall be changeable during a running session | Switching completes within 5 s, loses no committed utterance, and records the transition point |
| FR-LAT-4 | M | v1 | In Live mode, partial hypotheses shall be emitted and may be revised before finalisation | The UI shows text updating in place; `utterance.final` supersedes prior `utterance.partial` with the same ID |
| FR-LAT-5 | M | v1 | In Balanced mode, utterances shall be emitted once, on VAD endpoint, and never revised by ASR | No `utterance.partial` events are emitted |
| FR-LAT-6 | M | v1 | In Batch mode, no transcription shall occur until the session is stopped | Only elapsed time and metering appear during recording; the transcript appears after stopping |
| FR-LAT-7 | S | v1 | The UI shall display the active mode and its cost and quality implication | Each mode shows its latency target and whether it uses cloud services under current configuration |

### 3.5 Speaker diarization

| ID | Priority | Milestone | Requirement | Acceptance criteria |
|---|---|---|---|---|
| FR-DIA-1 | M | v1 | Every utterance shall be attributed to a speaker label stable within the session | In a 3-person conversation, the same person keeps the same label throughout |
| FR-DIA-2 | M | v1 | Diarization shall require no prior enrollment and shall infer the speaker count automatically | A session with an unknown speaker count produces a plausible count without configuration |
| FR-DIA-3 | S | v1 | The user shall be able to set a known speaker count or a min/max range per session | Setting "exactly 2" on a 2-person recording never produces a third label |
| FR-DIA-4 | M | v1 | Speaker labels shall be renamable in the UI, retroactively across the session | Renaming `Speaker 2` to `Anna` updates every utterance and artifact reference |
| FR-DIA-5 | M | v1 | A speaker embedding shall be stored for every utterance from v1 onward | ≥ 95% of utterances longer than 1 s carry a non-null embedding |
| FR-DIA-6 | S | v1 | The diarization backend shall be pluggable | A second backend is selectable by configuration alone |
| FR-DIA-7 | C | v2 | Voice enrollment shall map stored embeddings to named people | After enrolling a person, new sessions label them by name without manual renaming |
| FR-DIA-8 | C | v2 | Enrollment shall apply retroactively on operator request | A re-attribution job labels the enrolled person in sessions recorded before enrollment |
| FR-DIA-9 | C | v2 | Voiceprints shall be deletable per person, purging all their stored embeddings | After deletion, no embedding attributable to that person remains |
| FR-DIA-10 | M | v1 | Speaker attribution shall be defined per latency mode: partial hypotheses carry **no** speaker, because diarization requires a completed segment; attribution is assigned on finalisation and never changes thereafter | In Live mode a partial renders with an explicit unknown-speaker state, never a guess that later flips; in Balanced and Batch modes every utterance carries a speaker on first appearance |

### 3.6 Translation

| ID | Priority | Milestone | Requirement | Acceptance criteria |
|---|---|---|---|---|
| FR-TRA-1 | M | v1 | Utterances shall be translated into a configured target language | With target `en`, Russian utterances carry an English translation |
| FR-TRA-2 | M | v1 | The original transcript shall always be retained; translation shall never overwrite it | Every translated utterance exposes both `text` and `translation` |
| FR-TRA-3 | M | v1 | Translation shall use rolling conversational context of at least the previous *N* utterances | Pronoun and gender agreement is correct in a reviewed 20-utterance RU→EN sample where the referent is in a prior utterance |
| FR-TRA-4 | M | v1 | Translation shall be streamed to the client as it completes, per utterance, not batched to session end | Median translation appears within the [NFR-PERF-4](#41-performance) budget after its utterance |
| FR-TRA-5 | S | v1 | Utterances already in the target language shall not be translated | An English utterance in an EN-target session incurs no translation call |
| FR-TRA-6 | S | v1 | A fully local translation backend shall be available | With the server's internet disabled, translation still produces output |
| FR-TRA-7 | S | v1 | The UI shall support original-only, translation-only, and side-by-side views | All three render correctly on desktop and mobile widths |
| FR-TRA-8 | C | v2 | A per-speaker target language shall be configurable | In a bilingual conversation, each participant's view shows the other's speech in their own language |
| FR-TRA-9 | S | v1 | Consecutive short utterances shall be batched into a single translation request, within a configurable latency ceiling | A rapid exchange of one-word utterances issues fewer requests than utterances, without any translation exceeding the NFR-PERF-4 budget |

### 3.7 Session management

| ID | Priority | Milestone | Requirement | Acceptance criteria |
|---|---|---|---|---|
| FR-SES-1 | M | v1 | Sessions shall be startable and stoppable from the client and the API | Both paths create a session record with start and end timestamps |
| FR-SES-2 | M | v1 | Sessions shall persist across server restarts | Killing and restarting the server preserves all completed sessions and artifacts |
| FR-SES-3 | M | v1 | An interrupted session shall be recoverable with all utterances committed before the interruption | Force-killing the server mid-session leaves a readable session containing all finalised utterances |
| FR-SES-4 | M | v1 | A session whose client disconnects shall be finalised automatically after a configurable grace period | Closing the browser tab mid-session ends the session cleanly after the grace period, not indefinitely later |
| FR-SES-5 | M | v1 | Multiple clients shall be able to view a live session while one records | A laptop watching a session recorded by a phone sees utterances within the same latency budget |
| FR-SES-6 | M | v1 | Sessions shall carry editable metadata: title, participants, tags, language configuration | All fields are editable post-hoc and persist |
| FR-SES-7 | S | v1 | A session title shall be auto-generated if none is supplied | Every session has a non-empty title after it ends |
| FR-SES-8 | M | v1 | Transcript text shall be editable, persisted, and marked as edited | An edited utterance is flagged; the original remains retrievable |
| FR-SES-9 | M | v1 | Editing a transcript shall allow re-running plugins over the corrected text | Re-running produces a new artifact version; prior versions remain accessible |
| FR-SES-10 | M | v1 | Full-text search across all sessions shall cover **both transcripts and artifacts** | Searching a phrase that appears only in a generated summary returns that summary; searching one that appears only in speech returns the utterance. Both are highlighted |
| FR-SES-11 | C | v2 | Semantic search across all sessions shall be supported | A conceptual query returns relevant utterances sharing no literal terms with the query |
| FR-SES-12 | M | v1 | Sessions shall be deletable, purging transcript, artifacts, audio, and embeddings | After deletion, no residual row or file for that session remains |
| FR-SES-13 | M | v1 | Any past session shall be openable from the history list and shall present its full transcript with speaker labels, its metadata, its artifacts, and its audio | Opening a session recorded a month earlier renders the complete speaker-labelled transcript, its summary and action items, and a working player (FR-UI-8) |
| FR-SES-14 | M | v1 | Named presets shall capture language pair, latency mode, expected speaker count, vocabulary, and plugin selection, and start a session in one interaction | Selecting a saved preset and pressing Record starts a correctly configured session with no further choices. The most recently used configuration is the default when no preset is chosen |

### 3.8 Plugin system

| ID | Priority | Milestone | Requirement | Acceptance criteria |
|---|---|---|---|---|
| FR-PLG-1 | M | v1 | Plugins shall be discovered from a `plugins/` directory and from installed packages declaring an entry point | A plugin file dropped into `plugins/` loads on restart with no other change |
| FR-PLG-2 | M | v1 | Plugins shall subscribe to documented events and receive typed payloads | A plugin subscribing only to `session.end` is not invoked for other events |
| FR-PLG-3 | M | v1 | A plugin raising an exception shall be disabled for the session without affecting the pipeline or other plugins | With a deliberately crashing plugin installed, the session completes and the UI shows the plugin as failed |
| FR-PLG-4 | M | v1 | Each handler invocation shall be bounded by a configurable timeout | A plugin sleeping past its timeout is cancelled and reported; the session is unaffected |
| FR-PLG-5 | M | v1 | Plugins shall declare a configuration schema, rendered automatically by the UI as a settings form | A plugin with three typed config fields shows three correctly-typed inputs with validation |
| FR-PLG-6 | M | v1 | Plugins shall be individually enabled or disabled without restarting the server | Toggling takes effect on the next session |
| FR-PLG-7 | M | v1 | Plugins shall emit artifacts attached to the session | An emitted artifact appears in the UI and via `GET /api/sessions/{id}/artifacts` |
| FR-PLG-8 | M | v1 | Plugins shall receive a configured LLM client rather than constructing their own | A plugin performs an LLM call with no credential handling of its own, respecting global budget and local-only settings |
| FR-PLG-9 | M | v1 | Plugin execution shall not block the live transcript path | With a plugin running for 60 s, live utterances continue to appear within the normal latency budget |
| FR-PLG-10 | M | v1 | A **meeting summary** plugin shall ship as a reference implementation | On a 30-minute recording it produces a structured summary within the configured timeout |
| FR-PLG-11 | M | v1 | An **action items** plugin shall ship as a reference implementation | It produces action items with text and, where identifiable, owner and due date |
| FR-PLG-12 | S | v1 | Artifacts shall be versioned; re-running shall not destroy prior output | After two runs both versions are retrievable and the current one is indicated |
| FR-PLG-13 | C | v2 | Outbound webhooks shall let non-Python consumers subscribe to events | A configured webhook receives a signed POST for each subscribed event |
| FR-PLG-14 | S | v1 | Named prompts shall be saved by the operator and chosen per run, replacing a plugin's own instructions where the plugin declares it accepts one | A prompt written in Settings is offered beside the Generate button; a run naming it produces a summary following it, records the prompt's name on the artifact, and leaves the anti-fabrication system prompt and the length ceiling in force |

### 3.9 User interface

Screen-by-screen layout, states, and interaction behaviour are specified in [§5.1](#51-user-interface). The requirements below are the testable obligations that section must satisfy.

| ID | Priority | Milestone | Requirement | Acceptance criteria |
|---|---|---|---|---|
| FR-UI-1 | M | v1 | The UI shall display a live transcript updating without a page refresh | In Balanced mode, new utterances appear within 1 s of being finalised server-side |
| FR-UI-2 | M | v1 | The UI shall visually distinguish speakers | Each speaker has a stable, distinguishable colour and label |
| FR-UI-3 | M | v1 | The UI shall show a persistent, unmistakable recording indicator whenever capture is active | The indicator is visible on every view while recording and cannot be dismissed |
| FR-UI-4 | M | v1 | Record/stop, device selection, language configuration, and latency mode shall be reachable within two interactions from the main view | Verified on a 375 px viewport |
| FR-UI-5 | M | v1 | The recording view shall be usable one-handed on a phone | Primary controls sit within thumb reach at 375 × 667 px; no horizontal scrolling anywhere |
| FR-UI-6 | M | v1 | The UI shall show connection state and buffered-audio backlog during recording | Disconnecting the network shows a reconnecting state and a growing buffered count; reconnection clears both |
| FR-UI-7 | M | v1 | A session list with search and filtering by date, tag, and language shall be provided | Filtering narrows correctly on a corpus of ≥ 100 sessions |
| FR-UI-8 | S | v1 | An audio player synchronised with the transcript shall be provided | Clicking an utterance seeks to it; playback highlights the current utterance |
| FR-UI-9 | S | v1 | Errors from capture, ASR, and plugins shall surface as notifications with actionable text | Each error class names the component and suggests a remedy |
| FR-UI-10 | S | v1 | The client shall be installable as a PWA | On the HTTPS origin, the install prompt appears and the app launches standalone |
| FR-UI-11 | S | v1 | The UI shall render correctly in both light and dark themes | Both themes meet WCAG AA contrast for all text |
| FR-UI-12 | M | v1 | Record, History, and Settings shall each be reachable in one interaction from any screen, and Record shall be the landing view | Cold-loading the app leaves Stop/Record one tap away; each destination is one tap from each other |
| FR-UI-13 | M | v1 | The live transcript shall stay pinned to the newest utterance until the user scrolls away, then offer an explicit return | Scrolling up during recording holds position while new utterances arrive; a jump-to-live control restores the pin |
| FR-UI-14 | M | v1 | A translation that has not arrived yet shall render as pending and be visually distinct from one that failed | With translation deliberately delayed, the row shows a pending state; with it erroring, the row shows a distinguishable failed state |
| FR-UI-15 | S | v1 | Every list and search view shall define an empty state that says what to do next | Empty history and a zero-result search each render purpose-written copy, not a blank area |
| FR-UI-16 | S | v1 | Speaker colours shall come from a fixed colourblind-safe ordered palette, stable per speaker index across views, and shall never be the only carrier of speaker identity | The same speaker index renders the same colour in live, history, and detail views; every utterance also shows a textual label |
| FR-UI-17 | S | v1 | The client shall notify the user when post-processing completes for a session they are no longer watching | Stopping a Batch session and closing the tab produces a notification when the transcript and artifacts are ready, where the browser permits it |
| FR-UI-18 | M | v1 | Before recording starts the client shall confirm server reachability and microphone level, and block with an actionable message if either fails | With the server stopped, Record reports it and offers local recording (FR-CAP-17); with a muted or absent microphone, Record reports that instead of starting a silent session |
| FR-UI-19 | S | v1 | Artifacts shall be copyable to the clipboard and shareable via the platform share sheet, in one interaction | Copy places the rendered summary on the clipboard; on a platform exposing `navigator.share`, Share opens the native sheet |
| FR-UI-20 | S | v1 | All interface strings shall be externalised for translation. v1 ships English only | No user-facing string is hard-coded in a component; adding a locale file changes the interface language with no code change |
| FR-UI-21 | S | v1 | The recording indicator shall name the recogniser transcribing the session and state whether it runs on this machine, taking both from the running pipeline rather than from configuration | While recording against a local model the indicator reads *local · faster-whisper*; against a cloud one it reads *cloud · deepgram* and is visually distinct. A session routed to a per-language model names that model, and a session whose recogniser is swapped for a local one at its cost ceiling updates without a reload |

### 3.10 Configuration

| ID | Priority | Milestone | Requirement | Acceptance criteria |
|---|---|---|---|---|
| FR-CFG-1 | M | v1 | Configuration shall be file-based (TOML), overridable by environment variables | Both work; environment variables take precedence |
| FR-CFG-2 | M | v1 | Configuration shall be validated at startup with field-level error messages | An invalid value fails startup naming the field, the value, and the accepted range |
| FR-CFG-3 | M | v1 | The language list offered in the UI shall be operator-editable, not hard-coded | Adding a language code to config makes it selectable without a code change |
| FR-CFG-4 | M | v1 | Credentials shall never appear in logs, artifacts, or API responses | No key material appears at any log level |
| FR-CFG-5 | M | v1 | A global "local only" switch shall disable every outbound network call | With it enabled no outbound connection is attempted; cloud-dependent plugins report as unavailable |
| FR-CFG-6 | S | v1 | Cloud usage shall be switchable per session as well as globally | A session marked local-only performs no cloud calls even when cloud is globally enabled |
| FR-CFG-7 | S | v1 | Cloud spend shall be tracked per session and displayed, with a configurable ceiling that falls back to local processing when reached | A session shows its accumulated cost; on exceeding the ceiling it continues locally and reports the switch rather than failing |
| FR-CFG-8 | S | v1 | Changing the ASR, translation, or LLM backend shall take effect on the next session without restarting the server | Switching ASR backend in Settings and starting a new session uses the new backend; the running session is unaffected |

### 3.11 Import / export

| ID | Priority | Milestone | Requirement | Acceptance criteria |
|---|---|---|---|---|
| FR-EXP-1 | M | v1 | Sessions shall be exportable as Markdown | Export contains speaker-labelled transcript, metadata, and artifacts |
| FR-EXP-2 | M | v1 | Sessions shall be exportable as JSON with full fidelity | Round-tripping export → import reproduces the session |
| FR-EXP-3 | S | v1 | Sessions shall be exportable as subtitle files (SRT/VTT) | The exported file plays in sync with the exported audio |
| FR-EXP-4 | C | v2 | Artifacts shall be pushable to external destinations via plugins | A reference file-writer plugin writes a Markdown artifact to a configured directory |

---

## 4. Non-functional requirements

### 4.1 Performance

Latency is measured **from end of spoken word to text visible in the client browser**, and therefore includes network transit. Measured on Tier A server hardware ([§6.3](#63-server-hardware-tiers)) with client and server on the same LAN.

| ID | Requirement |
|---|---|
| NFR-PERF-1 | **Live mode:** median partial-hypothesis latency ≤ 2.5 s; p95 ≤ 5 s |
| NFR-PERF-2 | **Balanced mode:** median utterance latency ≤ 4 s from VAD endpoint; p95 ≤ 10 s |
| NFR-PERF-3 | **Batch mode:** total processing ≤ 0.3× session duration on Tier A; ≤ 1.0× on Tier B |
| NFR-PERF-4 | Translation shall appear within 2 s of its utterance (median) and 5 s (p95) |
| NFR-PERF-5 | Client-to-server audio transit on LAN shall add ≤ 150 ms; over Tailscale on the same continent, ≤ 400 ms |
| NFR-PERF-6 | Client capture shall drop no audio while the tab is foregrounded and the connection is healthy; drops are counted and shown |
| NFR-PERF-7 | The client shall recover from a 60 s network outage with no audio loss, given sufficient local buffer |
| NFR-PERF-8 | The UI shall stay responsive with a 2000-utterance session loaded (virtualised rendering; no interaction blocked > 100 ms) |
| NFR-PERF-9 | Full-text search across 500 sessions shall return in ≤ 500 ms |
| NFR-PERF-10 | Mode switching mid-session shall complete within 5 s and lose no finalised utterance |
| NFR-PERF-11 | Client CPU use during capture shall stay low enough to avoid thermal throttling over a 60-minute session on a mid-range phone |

### 4.2 Accuracy

Targets are split by microphone condition, because a phone on a meeting table and a laptop at arm's length are very different acoustic problems. All figures are **targets to be validated against the project's evaluation corpus** ([§4.9](#49-evaluation)), not commitments — Serbian in particular is an open question ([R1](#10-risks-and-open-questions)).

| ID | Metric | Desk condition | Table condition |
|---|---|---|---|
| NFR-ACC-1 | WER, English, Balanced mode | ≤ 10% | ≤ 16% |
| NFR-ACC-2 | WER, Russian, Balanced mode | ≤ 15% | ≤ 22% |
| NFR-ACC-3 | WER, Serbian, Balanced mode — **provisional pending R1** | ≤ 25% | ≤ 35% |
| NFR-ACC-4 | DER, 2–3 speakers, minimal overlap | ≤ 15% | ≤ 22% |
| NFR-ACC-5 | DER, 4–6 speakers, including overlap | ≤ 25% | ≤ 35% |

| ID | Requirement |
|---|---|
| NFR-ACC-6 | VAD false positives on room tone: ≤ 1 spurious utterance per hour |
| NFR-ACC-7 | Live mode WER may exceed Balanced mode by no more than 5 percentage points absolute |
| NFR-ACC-8 | The UI shall not present accuracy as certain: low-confidence utterances are marked, and the documentation states expected accuracy per condition |

> **Note on the table condition.** These figures reflect a single near-field microphone at ~1.5 m. This is an accepted consequence of the browser-only capture decision ([§7](#7-decision-record-where-does-the-computation-run)). Users who need better results for large meetings can plug a good USB microphone into the *client* device — the browser will offer it via `getUserMedia` (FR-CAP-2) — which recovers most of the desk-condition accuracy without any change to the system.

### 4.3 Resource limits

| ID | Requirement |
|---|---|
| NFR-RES-1 | Tier B (CPU-only server) shall run Balanced mode within 8 GB RAM |
| NFR-RES-2 | Server idle CPU (running, no session) shall be < 2% of one core |
| NFR-RES-3 | Session audio storage shall be ≤ 30 MB per hour at default settings |
| NFR-RES-4 | Client-to-server bandwidth shall be ≤ 40 kbps per session in Opus mode, ≤ 260 kbps in raw PCM mode |
| NFR-RES-5 | Client local buffer shall hold ≥ 5 minutes of audio and shall not exceed a configurable cap |
| NFR-RES-6 | The client bundle shall be ≤ 500 KB gzipped, so the page loads quickly on mobile data |
| NFR-RES-7 | The server shall refuse to start a session when free disk space is below a configurable threshold, and shall warn in the UI while approaching it. It shall never fail a session part-way by running out of disk |

### 4.4 Security

| ID | Requirement |
|---|---|
| NFR-SEC-1 | The server shall bind to a configurable address, defaulting to loopback. LAN exposure shall be deliberate |
| NFR-SEC-2 | v1 has no user authentication. Access control is delegated to the network layer (Tailscale). This shall be documented prominently, and the instance must not be exposed to the public internet without a reverse proxy providing authentication |
| NFR-SEC-3 | The WebSocket ingest endpoint shall require a session token issued by the server, not accept arbitrary connections |
| NFR-SEC-4 | The WebSocket endpoint shall validate its origin when bound to a non-loopback address |
| NFR-SEC-5 | Credentials shall be read from environment or a secrets file, never committed and never logged |
| NFR-SEC-6 | Plugins execute in-process with full server privileges. This is a documented trust boundary: **installing a plugin is equivalent to running arbitrary code on the server** |
| NFR-SEC-7 | Uploaded files and configured paths shall be validated against directory traversal |
| NFR-SEC-8 | Dependencies shall be pinned with a lockfile and scanned in CI |
| NFR-SEC-9 | The client shall serve a Content-Security-Policy that forbids inline script and restricts connections to its own origin |

### 4.5 Consent and legal

> **Deferred in v1.2.** Privacy and data-handling *features* — retention policies, cloud-egress redaction, at-rest encryption, per-person deletion — are out of scope for now and were removed from [§3](#3-functional-requirements). What remains below costs no implementation work: the browser enforces its own microphone indicator, and the rest is a paragraph in the README. Revisit before any release intended for other people to run.

| ID | Requirement |
|---|---|
| NFR-LEG-1 | An unmistakable recording indicator shall be visible whenever capture is active (FR-UI-3). The browser's own microphone indicator satisfies most of this for free |
| NFR-LEG-4 | The recording indicator shall also say whether the recogniser transcribing the session runs on this machine (FR-UI-21). The *per-session data-egress display* deferred in v1.2 stays deferred; this is the one line of it that costs nothing — the pipeline already knows which engine it is calling, and "is the room's audio leaving this machine right now" is not a question anybody should have to answer by reading configuration |
| NFR-LEG-2 | Documentation shall state plainly that recording law varies by jurisdiction — roughly a dozen US states require all-party consent, Germany criminalises recording confidential speech under §201 StGB, and Serbia, Russia, and the EU each impose their own constraints — and that **the operator, not the software, is responsible for lawful use** |
| NFR-LEG-3 | The project shall not ship any feature designed to conceal that recording is taking place |

### 4.6 Reliability

| ID | Requirement |
|---|---|
| NFR-REL-1 | A 4-hour continuous session shall complete without crash, unbounded memory growth, or audio loss |
| NFR-REL-2 | Loss of internet mid-session shall degrade to local processing rather than failing the session |
| NFR-REL-3 | Loss of connectivity between client and server shall buffer and resume, not terminate the session (FR-CAP-6) |
| NFR-REL-4 | Cloud API errors shall be retried with exponential backoff and then fall back to local processing where a local path exists |
| NFR-REL-5 | Database writes shall be transactional; an abrupt kill shall not corrupt the database |
| NFR-REL-6 | Model loading failures shall produce an actionable error naming the model, the path searched, and the download command |
| NFR-REL-7 | All mutable instance state shall live under a single configurable data directory ([§5.9](#59-storage-layout)), so that copying that directory with the server stopped constitutes a complete, restorable backup |

### 4.7 Portability and installability

| ID | Requirement |
|---|---|
| NFR-PORT-1 | Installation shall be possible via Docker Compose in a single documented command |
| NFR-PORT-2 | Installation shall also be possible natively via `uv` without Docker |
| NFR-PORT-3 | HTTPS setup shall be documented as a required step, with Tailscale Serve as the primary path and a working command sequence |
| NFR-PORT-4 | First run shall download required models automatically with progress reported, or fail with the exact command to fetch them |
| NFR-PORT-5 | The server shall run on Linux x86-64, Linux arm64, and macOS arm64. Windows is best-effort |
| NFR-PORT-6 | No feature shall require a GPU; GPU shall be detected and used automatically when present |

### 4.8 Maintainability

| ID | Requirement |
|---|---|
| NFR-MNT-1 | ASR, diarization, translation, and LLM shall each sit behind an explicit interface with at least two implementations, proving the abstraction. For the LLM, a cloud endpoint and a local OpenAI-compatible endpoint count as two — they differ in context length, streaming behaviour, and tool-call support, which is what exercises the abstraction |
| NFR-MNT-2 | The plugin API shall be versioned; breaking changes require a major version bump and a migration note |
| NFR-MNT-3 | Structured logging with per-component levels shall be configurable at runtime |
| NFR-MNT-4 | Public interfaces shall carry type annotations and pass a static type check in CI |
| NFR-MNT-5 | The client shall pass a type check and lint in CI alongside the server |

### 4.9 Evaluation

| ID | Requirement |
|---|---|
| NFR-EVAL-1 | The repository shall include an evaluation harness measuring WER per language and DER per scenario against a labelled corpus |
| NFR-EVAL-2 | The corpus shall include ≥ 10 minutes of labelled audio per supported language, and **both** a desk-condition and a table-condition multi-speaker recording |
| NFR-EVAL-3 | The harness shall compare configured backends side by side and emit a comparison table |
| NFR-EVAL-4 | Model-selection decisions in this document shall be revisited against harness output before v1 release |
| NFR-EVAL-5 | Corpus recordings shall be captured through the actual browser client, not a studio path, so measurements reflect the real signal chain |

---

## 5. External interfaces

### 5.1 User interface

Three top-level destinations, flat. Nothing sits more than two levels deep.

```
   Record            History              Settings
      │                 │                    │
      │                 ▼                    ├─ Capture   device, languages, mode
      │          Session detail              ├─ Backends  ASR, translation, LLM
      │                 ├─ Transcript + audio├─ Plugins   enable, configure
      │                 ├─ Artifacts         └─ Server    storage, disk, health
      ▼                 └─ Export
  live transcript,                    md · json · srt
   updating in place
```

**Record is the landing view.** Opening the app leaves the user one tap from recording, because it is the only time-critical action in the product — a conversation does not wait. History and Settings are places you go deliberately.

On phones the three destinations are a bottom tab bar, inside thumb reach. On desktop they are a persistent left rail and the transcript takes the remaining width.

#### Screen 1 — Record

States: `idle` → `permission` → `recording` → `stopping` → `done`, with `reconnecting` and `error` as overlays that never replace the transcript.

```
┌──────────────────────────────────┐
│ ● REC  12:04       Live · RU→EN  │  persistent, undismissable (FR-UI-3)
│ ⌂ local · faster-whisper         │  which recogniser, and whether audio
│                                  │  leaves this machine (FR-UI-21)
├──────────────────────────────────┤
│ ▁▃▅▇▅▃▁              ● connected │  level + link state (FR-UI-10, FR-UI-6)
├──────────────────────────────────┤
│                                  │
│ ▌Speaker 1               11:58   │
│ ▌Нам нужно закончить это к…      │  original
│ ▌We need to finish this by…      │  translation, reduced emphasis
│                                  │
│ ▌Speaker 2               12:01   │
│ ▌Da, do petka.                   │
│ ▌Yes, by Friday.                 │
│                                  │
│ ▌· · ·                   12:04   │  speaker unknown until finalised
│ ▌Хорошо, тогда я▍                │  partial — italic + caret, may rewrite
│                                  │
│            ↓ jump to live        │  only while scrolled away
├──────────────────────────────────┤
│  RU → EN        Balanced     ⚙   │
│            ( ■  Stop )           │  thumb reach (FR-UI-5)
└──────────────────────────────────┘
```

Behaviour:

- **Autoscroll is pinned to the newest utterance** until the user scrolls up, which releases the pin and reveals *jump to live*. Arriving text must never yank the viewport out from under someone reading back (FR-UI-13).
- **Partials are visibly provisional** — italic with a caret — and may rewrite in place. On finalisation they settle to normal weight and never change again (FR-LAT-4, FR-LAT-5). In Balanced mode no partial ever appears, so text arriving later but settled is the expected rhythm.
- **A partial carries no speaker.** Diarization needs a completed segment, so Live mode shows an explicit unknown-speaker placeholder that resolves on finalisation (FR-DIA-10). Guessing and then flipping the label would be worse than showing nothing.
- **Translation is a second line under its original**, at reduced emphasis, and arrives independently and later (FR-TRA-4). While absent it renders as pending, never as empty or failed (FR-UI-14).
- **Speaker colour comes from a fixed, colourblind-safe ordered palette**, assigned on first appearance and stable for that index everywhere in the app. The label is always present — colour is never the sole carrier of identity (FR-UI-16).
- **Batch mode shows no transcript at all** while recording (FR-LAT-6), so the view reduces to timer, level meter, and elapsed duration, with copy that sets the expectation that text appears after stopping.
- **Reconnecting is non-modal**: a bar reports the state and the buffered backlog draining, while capture continues (FR-CAP-6). Recording must never be interrupted to show a network message.

#### Screen 2 — History

```
┌──────────────────────────────────┐
│ History                          │
├──────────────────────────────────┤
│ [ Search transcripts…          ] │
│ All time ▾   All langs ▾  Tags ▾ │
├──────────────────────────────────┤
│ Standup — infra                  │
│ Today 09:12 · 18 min · 4 speakers│
│ RU→EN · summary · 3 action items │
├──────────────────────────────────┤
│ Call with Marko                  │
│ Yesterday 16:40 · 42 min · 2 spk │
│ SR→EN · summary                  │
├──────────────────────────────────┤
│ Voice note                       │
│ 12 Aug · 3 min · 1 speaker       │
│ EN · no artifacts                │
└──────────────────────────────────┘
```

Each row carries what distinguishes one conversation from another: title, when, how long, how many speakers, language pair, and which artifacts exist. Duration and speaker count do more work than the title here, because auto-generated titles are often weak.

Searching switches the list to matches rather than sessions — the transcript line, its speaker, its timestamp, and the session it belongs to, with the hit highlighted (FR-SES-10):

```
│ Standup — infra            Today │
│ …finish the ⟦migration⟧ by Friday│
│ Speaker 2 · 11:58            ▸   │
```

Selecting a match opens the session **scrolled to that utterance**, not to the top.

#### Screen 3 — Session detail

```
┌──────────────────────────────────┐
│ ‹ History   Standup — infra   ⋯  │
├──────────────────────────────────┤
│ Today 09:12 · 18 min · RU→EN     │
│ #infra #weekly                ✎  │
├──────────────────────────────────┤
│ Transcript │ Summary │ Actions   │
├──────────────────────────────────┤
│ ▌Anna                    09:12 ✎ │
│ ▌Нам нужно закончить это к…      │
│ ▌We need to finish this by…      │
│                                  │
│ ▌Marko                   09:13   │
│ ▌Da, do petka.                   │
│ ▌Yes, by Friday.                 │
├──────────────────────────────────┤
│ ▶ ──●──────────── 04:12 / 18:04  │
└──────────────────────────────────┘
```

- **Tabs, not scrolling**, separate transcript from artifacts: they answer different questions and one should never bury the other.
- **The player follows the transcript and the transcript follows the player.** Tapping an utterance seeks to it; playback highlights the line currently being spoken (FR-UI-8).
- **Editing is inline** on any line. A corrected line is marked as edited, and the original stays retrievable (FR-SES-8). After an edit the artifact tabs offer to re-run, since the summary they hold is now stale (FR-SES-9).
- **Renaming a speaker is done by tapping the name** on any of their lines, and applies across the whole session at once (FR-DIA-4).
- The overflow menu holds export, delete, and the record of whether anything left the server.

#### Screen 4 — Settings

Grouped by what the operator is actually deciding: **Capture** (default device, language list, target language, default latency mode, browser audio processing), **Backends** (ASR, translation, LLM, with the active model and reachability shown), **Plugins** (enable, configure — forms rendered from each plugin's declared schema, FR-PLG-5), **Prompts** (named instructions to generate an artifact with, FR-PLG-14), and **Server** (storage location and usage, disk headroom, model status, health).

Prompts are a section rather than a plugin setting because they are not one value: several are correct at once — a customer call and a standup want different summaries — and which one applies is a decision about the recording in front of the operator. So they are written here and chosen beside the button that spends the call, and the plugin's built-in instructions remain what an unnamed run uses.

Settings that cannot take effect until the next session say so rather than appearing to apply immediately.

#### Cross-cutting

| Concern | Rule |
|---|---|
| **Empty states** | Every list defines one. No history reads as an invitation to record, not an error. No search results names the query and offers to clear the filters (FR-UI-15) |
| **Errors** | Non-blocking, they name the component, and they say what to do — "microphone permission was denied; enable it in site settings and press Record again", never "an error occurred" (FR-UI-9) |
| **Destructive actions** | Deleting a session requires confirmation naming the session's title, because nothing is recoverable afterwards (FR-SES-12) |
| **Responsive** | One column below 640 px, transcript plus rail above. All primary actions reachable at 375 px with no horizontal scroll (FR-UI-4) |
| **Long sessions** | The transcript is virtualised; a 2000-utterance session scrolls without blocking interaction (NFR-PERF-8) |
| **Accessibility** | Speaker identity carried by label as well as colour; visible focus states throughout; WCAG AA contrast in both themes (FR-UI-11) |
| **Theming** | Light and dark both designed, following the device preference with a manual override |

### 5.2 HTTP API (sketch)

```
GET    /api/health                      → version, active backends, model status, GPU presence
GET    /api/config                      → effective configuration (secrets redacted)
PATCH  /api/config                      → update mutable configuration

POST   /api/sessions                    → create a session {languages, target_language, mode, vocabulary[]}
                                          returns {session_id, ingest_token, ws_url}
GET    /api/sessions                    → list with pagination, filters (date, tag, language)
GET    /api/sessions/{id}               → session detail with utterances
PATCH  /api/sessions/{id}               → update metadata (title, tags, participants)
POST   /api/sessions/{id}/stop          → finalise the session
PATCH  /api/sessions/{id}/mode          → change latency mode mid-session
DELETE /api/sessions/{id}               → purge session and all associated data

PATCH  /api/utterances/{id}             → edit transcript text
GET    /api/sessions/{id}/audio         → audio stream, supports HTTP range requests

GET    /api/sessions/{id}/speakers      → speaker labels
PATCH  /api/speakers/{id}               → rename a speaker

GET    /api/sessions/{id}/artifacts     → plugin outputs, all versions
POST   /api/sessions/{id}/plugins/{name}/run → re-run a plugin
                                          {utterance_ids[]?, prompt_id?} — part of the
                                          session, and which saved prompt to use

GET    /api/plugins                     → installed plugins, status, config schema
PATCH  /api/plugins/{name}              → enable/disable, update config

GET    /api/prompts                     → saved prompts, most recently used first
PUT    /api/prompts                     → create or rewrite one {id?, name, instructions}
DELETE /api/prompts/{id}                → remove one; artifacts it produced are kept

GET    /api/search?q=...&mode=fts|semantic → cross-session search
GET    /api/sessions/{id}/export?format=md|json|srt|vtt

WS     /ws/ingest?token=...             → client → server audio upload
WS     /ws/sessions/{id}                → server → client live event stream
```

### 5.3 Audio ingest protocol

The client opens `/ws/ingest` with the token issued at session creation, then alternates a JSON control frame and a binary payload frame:

```jsonc
// Control frame — precedes each audio chunk
{ "seq": 412, "t_ms": 8240, "codec": "pcm_s16le_16k", "samples": 3200 }
```

followed by the binary chunk. Chunks are 200 ms by default.

**Server → client acknowledgements** carry the highest contiguous sequence received:

```jsonc
{ "type": "ack", "through_seq": 412, "buffered_ms": 0 }
```

The client retains unacknowledged chunks in memory, spilling to IndexedDB beyond a threshold, and retransmits from the first gap on reconnection. This is what makes FR-CAP-6 and NFR-REL-3 achievable.

### 5.4 Live event stream

```jsonc
{
  "type": "utterance.final",
  "session_id": "sess_01J...",
  "seq": 142,                    // monotonic per session; clients detect gaps and refetch
  "ts": "2026-08-14T11:32:07.412Z",
  "data": {
    "utterance_id": "utt_01J...",
    "start_ms": 184320,
    "end_ms": 187940,
    "speaker_id": "spk_02",
    "language": "ru",
    "text": "Нам нужно закончить это к пятнице.",
    "translation": "We need to finish this by Friday.",
    "confidence": 0.94,
    "words": [{ "w": "Нам", "start_ms": 184320, "end_ms": 184580 }]
  }
}
```

**Event types:** `session.start`, `session.end`, `session.mode_changed`, `utterance.partial`, `utterance.final`, `speaker.changed`, `translation.final`, `transcript.edited`, `artifact.created`, `capture.error`, `plugin.error`.

Viewers reconnect by supplying the last `seq` they received; the server replays from there or instructs a full refetch. Recording and viewing are separate connections, so a phone can record while a laptop watches (FR-SES-5).

### 5.5 Plugin API contract

```python
from droid_assistant.plugin import Plugin, Context, Event, Artifact
from pydantic import BaseModel

class SummaryConfig(BaseModel):
    style: str = "bullets"          # rendered as a form field automatically
    max_words: int = 400
    include_decisions: bool = True

class SummaryPlugin(Plugin):
    name = "summary"
    version = "1.0.0"
    api_version = 1                 # host refuses to load a mismatched major version
    config_schema = SummaryConfig
    subscribes = {Event.SESSION_END}

    async def on_session_end(self, ctx: Context) -> Artifact | None:
        transcript = await ctx.store.transcript(ctx.session_id, include_speakers=True)
        text = await ctx.llm.complete(
            system="You summarise meeting transcripts.",
            prompt=f"Summarise in at most {ctx.config.max_words} words:\n\n{transcript}",
        )
        return Artifact(kind="summary", mime="text/markdown", content=text)
```

| Member | Contract |
|---|---|
| `ctx.store` | Read-only access to the session, its utterances, and speakers |
| `ctx.llm` | Pre-configured client. Respects global budget and local-only settings. Plugins never handle credentials |
| `ctx.config` | Validated instance of the plugin's own `config_schema` |
| `ctx.emit_artifact(...)` | Emit an artifact at any point, not only from `on_session_end` |
| `ctx.logger` | Namespaced logger |

**Host guarantees:** every handler runs in a supervised task with a timeout, off the live transcript path (FR-PLG-9); exceptions are caught, logged, and disable that plugin for the session only. **Non-guarantee:** plugins run in-process with full server privileges (NFR-SEC-6).

### 5.6 Backend interfaces

```python
class ASRBackend(Protocol):
    async def start_stream(self, config: StreamConfig) -> ASRStream: ...
    async def transcribe(self, audio: AudioBuffer, config: StreamConfig) -> list[Utterance]: ...
    @property
    def capabilities(self) -> ASRCapabilities: ...   # streaming?, languages, word timestamps?

class DiarizationBackend(Protocol):
    async def diarize(self, audio: AudioBuffer) -> list[SpeakerSegment]: ...
    async def embed(self, audio: AudioBuffer) -> Embedding: ...

class TranslationBackend(Protocol):
    async def translate(self, text: str, src: str, dst: str, context: list[str]) -> str: ...
```

`capabilities` is what lets the server refuse an impossible configuration at startup rather than failing mid-session — for example, cloud ASR while `privacy.local_only` is on, or a single-language model offered a language it will confidently mistranscribe rather than reject.

Live mode is deliberately *not* among those refusals. Every mode is built on `transcribe`; Live re-runs it over a sliding window (§5.4, LocalAgreement-2), so a backend needs no `start_stream` to serve it — only enough speed, which is a matter of degree and belongs in a warning rather than a veto. Reading `streaming` as a Live prerequisite locked every local backend out of the mode the sliding window exists to give them.

### 5.7 Audio transport

| Mode | Encoding | Bandwidth | When |
|---|---|---|---|
| **Raw** (default) | 16 kHz mono `s16le`, 200 ms chunks | ~256 kbps | LAN and Tailscale on wifi. Lowest latency, no client-side encoding cost, trivial server handling |
| **Compressed** | Opus via `MediaRecorder` (`audio/webm;codecs=opus`) at 24–32 kbps | ~28 kbps | Mobile data, or a slow link. Server demuxes the WebM container. Adds ~100–200 ms latency |

The client selects a mode automatically from the Network Information API where available, and the user can override it (FR-CAP-12).

### 5.8 Data model

```
sessions      (id, title, started_at, ended_at, mode, source_languages, target_language,
               client_user_agent, mic_label, audio_constraints, cloud_used, providers_used,
               audio_path, tags, participants, metadata)

utterances    (id, session_id, seq, start_ms, end_ms, speaker_id, language, text,
               text_original, translation, confidence, words_json, edited_at, embedding)

speakers      (id, session_id, label, display_name, person_id, centroid_embedding)

persons       (id, name, created_at)                                      -- v2
voiceprints   (id, person_id, embedding, source_session_id, created_at)   -- v2

artifacts     (id, session_id, plugin_name, plugin_version, kind, mime,
               content, version, created_at, superseded_by)

plugin_state  (plugin_name, enabled, config_json, last_error, last_error_at)

prompts       (id, name, instructions, created_at, updated_at, last_used_at)  -- FR-PLG-14

ingest_tokens (token, session_id, issued_at, expires_at, revoked_at)

events_log    (id, session_id, type, payload_json, created_at)            -- diagnostics, prunable
```

Indexes: `utterances(session_id, seq)`, FTS5 virtual tables over `utterances(text, translation)` **and** `artifacts(content)` (FR-SES-10), vector index over `utterances(embedding)`.

All absolute timestamps are stored as UTC and rendered in the viewer's local timezone. Utterance offsets (`start_ms`, `end_ms`) are relative to session start and are timezone-free.

### 5.9 Storage layout

All mutable state lives under one directory, so that backing up the instance is copying a folder.

```
$DROID_DATA/                      # default ./data — override with DROID_DATA_DIR
├── droid.db                      # SQLite: sessions, utterances, speakers, artifacts, FTS index
├── droid.db-wal                  # write-ahead log — must be copied together with droid.db
├── droid.db-shm
├── audio/
│   └── 2026/08/14/
│       └── sess_01J8XK7Q.opus    # one file per session; the path is recorded in sessions.audio_path
├── models/                       # downloaded ASR / VAD / diarization weights — regenerable, skip in backups
└── config.toml                   # operator configuration
```

**Backup** is `cp -r $DROID_DATA` with the server stopped, excluding `models/`. Restoring that directory onto a clean install reproduces every session, transcript, and artifact (NFR-REL-7).

**Growth is unbounded.** Retention and auto-purge were removed in v1.2, so nothing is ever deleted except by hand (FR-SES-12). At the [NFR-RES-3](#43-resource-limits) budget of ~30 MB per hour of audio, one hour of recording per day is roughly 11 GB per year; transcripts themselves are negligible beside that. NFR-RES-7 guards against filling the disk, but the operator owns the cleanup policy.


---

## 6. Architecture and technology

### 6.1 Component diagram

```
  ┌──────────────────── Browser client (phone or desktop) ────────────────────┐
  │                                                                           │
  │  getUserMedia ─► AudioWorklet ─► resample 16 kHz ─► chunk ─► WebSocket ──┐ │
  │       │                                                 ▲               │ │
  │  Wake Lock                                     IndexedDB buffer         │ │
  │                                                (retransmit on reconnect)│ │
  │                                                                          │ │
  │  Live transcript ◄──────────────── WebSocket ◄───────────────────────────┼─┼──┐
  └───────────────────────────────────────────────────────────────────────────┘ │  │
                                                                                 │  │
  ═══════════════════════════ HTTPS / Tailscale ══════════════════════════════════│══│═══
                                                                                 ▼  │
  ┌───────────────────────────── Server (your machine) ──────────────────────────┐  │
  │                                                                              │  │
  │  Ingest ─► Ring buffer ─► VAD ─────────────────────────┐                     │  │
  │                                                        ▼                     │  │
  │                      ┌─────────────────────────────────────────────┐         │  │
  │                      │  Pipeline — parameterised by latency mode   │         │  │
  │                      │    Live     : sliding window + partials     │         │  │
  │                      │    Balanced : commit on VAD endpoint        │         │  │
  │                      │    Batch    : buffer whole session          │         │  │
  │                      │    ASR ─► Diarize ─► Translate              │         │  │
  │                      └──────────────────┬──────────────────────────┘         │  │
  │                                         ▼                                    │  │
  │                                   ┌───────────┐                              │  │
  │                                   │ Event bus │──────────────────────────────┼──┘
  │                                   └─────┬─────┘                              │
  │                          ┌──────────────┴──────────────┐                     │
  │                    ┌─────▼──────┐              ┌────────▼────────┐            │
  │                    │  SQLite    │              │  Plugin host    │            │
  │                    │  + audio   │              │  (off the live  │            │
  │                    └────────────┘              │   path)         │            │
  │                                                └────────┬────────┘            │
  └─────────────────────────────────────────────────────────┼─────────────────────┘
                                                            ▼
                                                     LLM API (cloud)
```

### 6.2 Technology choices and rejected alternatives

| Layer | Choice | Why | Rejected |
|---|---|---|---|
| Client | React 19 + Vite + TypeScript + Tailwind, built to static assets served by the API | One deployable, no separate Node runtime; matches the maintainer's stack | **Svelte** — smaller, smaller ecosystem. **Server-rendered templates** — poor fit for live-updating transcript |
| Client audio | `getUserMedia` + `AudioWorklet` + manual resample | Worklet runs off the main thread, giving stable capture while the UI renders. Full control over chunk size and latency | **`ScriptProcessorNode`** — deprecated, main-thread, causes glitches. **`MediaRecorder` alone** — coarse chunking, container overhead, less latency control (retained as the compressed transport mode) |
| Screen keep-alive | Screen Wake Lock API | The single most important mitigation for browser-only capture. Without it, phone recording stops at screen lock | **Playing silent audio** — a common hack; unreliable and burns battery |
| Client buffering | In-memory ring, spilling to IndexedDB | Makes reconnection lossless, which is what makes a browser client trustworthy for a 60-minute meeting | **No buffering** — any network blip silently loses audio |
| Transport | WebSocket, raw PCM by default, Opus for metered links | Simplest thing that meets the latency budget; compression only where it's needed | **WebRTC** — designed for this, but a SFU/ICE stack is heavy for a single-hop self-hosted app. **HTTP chunked upload** — no back-pressure or acknowledgement path |
| Backend runtime | Python 3.12, FastAPI, uvicorn, `uv` | The entire speech-ML ecosystem is Python; matches the maintainer's stack | **Node/TypeScript** — would shell out to Python for every model. **Go/Rust** — great for the audio path, thin ML ecosystem |
| VAD | Silero VAD (ONNX, MIT) | ~1 MB, sub-millisecond per frame, no PyTorch, strong in noise | **WebRTC VAD** — much weaker in noise. **Client-side VAD** — would save bandwidth but makes server-side re-segmentation impossible; revisit only if bandwidth becomes the binding constraint |
| ASR (local) | `faster-whisper` (CTranslate2, MIT) — `large-v3-turbo` on Tier A, `medium`/`small` int8 on Tier B | Best multilingual coverage including Russian and Serbian; 4–8× faster than reference Whisper at equal quality | **Parakeet-TDT-v3** — faster and excellent for its 25 European languages, but Serbian is very likely absent ([R1](#10-risks-and-open-questions)). **`whisper.cpp`** — retained as an Apple Silicon alternative backend |
| ASR (streaming) | LocalAgreement-2 sliding window over `faster-whisper` | Whisper is **not** a streaming model. This policy emits stable prefixes from overlapping windows and is what makes Live mode possible for Russian and Serbian at all | **Streaming Zipformer (sherpa-onnx)** — genuinely streaming and fast, but no Serbian. **Cloud streaming** — retained as the high-quality Live-mode option |
| ASR (cloud) | Pluggable: Deepgram, Speechmatics, Google STT | Native streaming, strong Russian coverage, and the practical answer if local Serbian quality fails R1 | **OpenAI Whisper API** — batch only, no streaming |
| Diarization | `sherpa-onnx` offline diarization (Apache-2.0) | No PyTorch, permissive license, no gated model downloads — real installation friction matters for an OSS project | **`pyannote.audio` 3.1** — higher accuracy but requires a Hugging Face token and gated-model acceptance. Retained as an **opt-in** backend |
| Translation | LLM per utterance with rolling context, streamed per utterance | Sentence-isolated MT destroys pronoun reference, gender agreement, and register — which matter badly for RU and SR | **Per-sentence NMT** — cheaper, noticeably worse in conversation. **Whisper's `translate` task** — English-target only, degrades transcription |
| Translation (local) | Helsinki-NLP Opus-MT or MADLAD-400 via CTranslate2 | Permissive licensing suitable as an OSS default | **NLLB-200** — better quality but **CC-BY-NC**, so opt-in only (C-6) |
| LLM | OpenAI API via the `openai` SDK, streaming, model set by configuration | Chosen for its API *shape* as much as its models: Azure OpenAI, OpenRouter, Ollama, vLLM, LM Studio, and llama.cpp all expose OpenAI-compatible endpoints, so one client serves a cloud frontier model and a fully local one by changing a base URL. That is what makes C-3 (works with no internet) nearly free | **Provider-locked design** — rejected; the LLM sits behind an interface (NFR-MNT-1). **A bespoke multi-provider abstraction** — unnecessary, since the OpenAI request shape is already the de-facto interop standard |
| Storage | SQLite (WAL) + FTS5 + `sqlite-vec`; audio as Opus on disk | One file, zero operational burden, ideal for local-first distribution | **Postgres + pgvector** — better for multi-user, unjustified for v1. Documented as the migration path |
| Access control | Tailscale, with the app trusting the network | Delegates identity to a tool that does it properly, and simultaneously solves HTTPS. Building auth into v1 would be worse auth | **Built-in accounts** — deferred to v3. **Public exposure with a password** — rejected as a documented default |
| Packaging | Docker Compose (CPU and CUDA) + native `uv` install + systemd unit | Others must be able to run this | Source-only distribution — hostile to adoption |

### 6.3 Server hardware tiers

**The server is a small always-on machine.** This is an architectural decision, not a preference: a laptop that sleeps drops the session you were about to record, which was the single largest day-to-day failure in earlier drafts (formerly R12). Everything below assumes a dedicated box that boots on power and starts the service automatically ([§8.5](#85-running-it-always-on)).

The tiers differ in one thing that matters — **how much of the pipeline can run locally**:

| Tier | Hardware | ≈ Price | Power | Local ASR ceiling | Modes without cloud |
|---|---|---|---|---|---|
| **A** | Apple Silicon Mac, or x86-64 + NVIDIA GPU ≥ 6 GB | $500+ | 10–40 W | `large-v3-turbo` at realtime | All three |
| **B — recommended** | Intel N100/N150-class mini PC, 16 GB | ~$170 | 10–15 W | `small`/`medium` int8 near realtime | Balanced, Batch |
| **C** | Raspberry Pi 5, 8–16 GB | ~$100 | 5–8 W | `small` int8, around or below realtime | Batch, slowly |
| **D** | Raspberry Pi 4, 4–8 GB | ~$60 | 4–6 W | `tiny`/`base` only, below realtime | **None — cloud ASR required** |

**Tier B is the recommendation.** It is the cheapest tier at which the accuracy targets in [§4.2](#42-accuracy) are reachable without sending audio off the box, which is what "hybrid, local-first" was chosen for in the first place.

**On Tier D specifically:** a Raspberry Pi 4 is an excellent always-on host and a poor inference host. It handles ingest, storage, orchestration, the web app, and plugin dispatch comfortably — but `tiny` and `base` are the only Whisper models that will run, and their Russian accuracy is weak and Serbian unusable. A Pi 4 therefore implies **cloud ASR**, which means per-minute cost and audio leaving the network. That is a legitimate configuration and the spec supports it fully; it is simply a different trade than the one C-3 and C-4 were written for. Tier C (Pi 5) narrows the gap but does not close it.

Exact figures per tier are the evaluation harness's job ([§4.9](#49-evaluation)); the ranking above is stable, the absolute numbers are not yet measured.

### 6.4 Why the browser client is enough

The three things a native app would buy — background recording, better microphones, on-device ML — are addressed or accepted:

| Native advantage | Status |
|---|---|
| Recording with the screen off | **Mitigated** by Screen Wake Lock (FR-CAP-7), which keeps the screen on and the tab alive at a battery cost |
| Surviving app switching | **Accepted limitation.** Documented; the recording tab must stay foregrounded, most strictly on iOS |
| Better microphone access | **Not actually an advantage** — `getUserMedia` offers any USB microphone plugged into the client device (FR-CAP-2) |
| On-device ASR | **Not viable** for this project's languages ([§7](#7-decision-record-where-does-the-computation-run)) |
| Reliable networking | **Mitigated** by client-side buffering and retransmission (FR-CAP-6) |

---

## 7. Decision record: where does the computation run?

**Status:** Accepted
**Date:** 2026-08-14
**Supersedes:** the v1.0 "Raspberry Pi vs. Android" record

### Context

The product should let a user open an Android app *or* a web page, press record, and see results. That raised the deciding question: **can a phone run good-quality, live speech recognition locally?** If yes, the app could be self-contained. If no, a server is mandatory and the client is thin.

### Investigation

| Option | Live? | EN | RU | SR | Diarization |
|---|---|---|---|---|---|
| Android `SpeechRecognizer`, on-device (API 33+) | Yes, streaming | Good | Fair–good where a pack exists | **None** | **No** |
| `whisper.cpp` on-device | Only at `tiny`/`base` | Poor at that size | Bad | Unusable | No |
| `sherpa-onnx` streaming Zipformer | Yes, ~300 ms | Strong | Thin model availability | **Effectively none** | Offline only, costly on phone |
| Vosk | Yes | OK | OK | No | No |
| Cloud streaming from the phone | Yes | Excellent | Excellent | Good | Provider-dependent |

Three findings are decisive:

1. **Serbian has no viable on-device model.** Not in Google's on-device packs, not in streaming Zipformer, not in Vosk. This alone settles it.
2. **No phone API performs live diarization.** The "recognise different people" requirement has no on-device answer at all.
3. **There is no portable NPU path.** NNAPI is deprecated as of Android 15, and its replacements are per-vendor SDKs — untenable for an open-source project.

Secondary but real: Whisper is not a streaming model, so live use needs a sliding window costing 2–3× compute, which caps a phone at `tiny`/`base`; and sustained multi-core inference throttles a phone within roughly 10–15 minutes.

### Decision

**The phone is a microphone and a screen. The server is the brain. The client is a web page, and there will be no native application.**

- Capture, buffering, and display happen in the browser via `getUserMedia`, `AudioWorklet`, IndexedDB, and Wake Lock.
- Recognition, diarization, translation, storage, and plugins happen on a server the operator runs — their own machine, reached over Tailscale.
- The `ASRBackend` abstraction keeps a future on-device English-only fast path possible without restructuring anything, but it is not planned.

### Consequences

**Accepted**

- HTTPS becomes a **v1 requirement**, not a later nicety, because browser capture needs a secure context. Tailscale Serve makes this about ten minutes of setup.
- Recording requires a foregrounded tab with the screen awake. This is the real cost of the decision, and it is worst on iOS.
- A phone microphone at 1.5 m is a worse signal than a dedicated array, so [§4.2](#42-accuracy) carries separate, lower targets for the table condition.
- The server must be awake when recording. An always-on machine is recommended, not required.

**Gained**

- One codebase, one language pair, no app store, no Kotlin.
- The client works on every device the user already owns, including desktops, on day one.
- Plugging a good USB microphone into the *client* recovers most of the accuracy loss with no code change.
- Model upgrades ship server-side, instantly, with no client update.

### If this is ever revisited

The trigger would be a genuinely streaming multilingual on-device model covering Serbian, plus an on-device diarizer. Neither exists today. Should both appear, the `ASRBackend` interface is where they would attach.

---

## 8. Deployment

### 8.1 Install

```bash
git clone https://github.com/<owner>/droid-assistant
cd droid-assistant
cp .env.example .env          # set OPENAI_API_KEY if using cloud features
docker compose up -d          # or: docker compose -f compose.cuda.yml up -d
```

All state lives in the data directory ([§5.9](#59-storage-layout)), which the compose file mounts as a named volume. `docker compose down -v` deletes that volume and every recorded session with it — use plain `down` unless you mean it.

Native alternative:

```bash
uv sync
uv run droid-assistant models download
uv run droid-assistant serve
```

### 8.2 HTTPS is mandatory

Browsers refuse microphone access outside a secure context. `http://localhost` qualifies — so a browser on the server machine works immediately — but any other device needs real HTTPS.

**Primary path — Tailscale Serve:**

```bash
tailscale up
tailscale serve --bg 8000
# → https://<machine>.<tailnet>.ts.net
```

This issues a genuine certificate, needs no port forwarding, and works from outside the LAN. It also provides the access control that v1 deliberately does not implement (NFR-SEC-2).

**Alternatives**, documented but not recommended as defaults:

| Option | Trade-off |
|---|---|
| Caddy with a DNS-01 certificate for a domain pointing at the LAN IP | Needs a domain; certificate valid 90 days fully offline once issued |
| `chrome://flags/#unsafely-treat-insecure-origin-as-secure` | Per-device manual allowlist; adequate for one personal device, unsuitable as documented default |
| `mkcert` with an installed root CA | Per-device CA installation; more friction than Tailscale |

### 8.3 First-run checklist

Documentation shall walk through: starting the server, reaching it over HTTPS, granting microphone permission, selecting a device, running a 30-second test recording, and confirming the transcript. A `droid-assistant doctor` command shall verify model presence, GPU detection, disk space, and HTTPS reachability.

### 8.4 Recording well

Guidance shipped in the documentation, since it affects results more than any setting:

- Place the phone as close to the speakers as practical; 1.5 m is much worse than 0.5 m
- Prefer a laptop with a USB microphone for meetings that matter
- Keep the screen on, or confirm Wake Lock is active
- Disable browser noise suppression for multi-speaker recordings — it is tuned for a single near voice and can suppress quieter participants
- Record a 30-second test before a session that cannot be repeated

### 8.5 Running it always-on

The server is expected to be a dedicated machine that is simply always up ([§6.3](#63-server-hardware-tiers)). Three properties make that true, and the documentation shall verify each during install:

1. **Starts on power.** The service runs under systemd with `Restart=always` and `WantedBy=multi-user.target`, so a power cut is a reboot rather than an outage. On a mini PC, enable *restore on AC loss* in the BIOS.
2. **Reachable without intervention.** `tailscale up` persists across reboots; `tailscale serve` is re-applied by the unit file. The hostname never changes, so client bookmarks keep working.
3. **Reports its own health.** `GET /api/health` returns model status, disk headroom, and backend reachability. `droid-assistant doctor` checks the same from the shell.

**Undervolting, throttling, and storage.** A Pi-class board should run from a good power supply and, on Tier C or D, benefit from a heatsink or fan — thermal throttling during a long Batch job is otherwise silent and slow. **Do not keep the database and audio on the boot SD card**: attach USB SSD storage and point `DROID_DATA_DIR` at it. SD cards fail under sustained write, and they take your entire history with them ([§5.9](#59-storage-layout)).

**Backups run themselves or not at all.** A nightly `cp -r` of `$DROID_DATA` — excluding `models/` — to a second disk or another machine is the whole procedure (NFR-REL-7). Schedule it; a backup you have to remember is not a backup.

---

## 9. Roadmap

### v1 — Working system

Browser capture with Wake Lock and resumable buffering · WebSocket ingest · VAD · local and cloud ASR · anonymous diarization with embeddings stored · streamed LLM translation with rolling context · three latency modes with mid-session switching · SQLite persistence · session history and browsing · full-text search over transcripts and artifacts · session presets · pause/resume · transcript editing · plugin system with summary and action-item reference plugins · live transcript UI with multi-viewer support · audio playback · Markdown/JSON/SRT export · Tailscale HTTPS documentation · Docker and native install · evaluation harness covering both microphone conditions.

### v2 — Depth

Voice enrollment and named speakers, applied retroactively · semantic search · per-speaker target languages · file import · webhook plugin transport · artifact push destinations · per-utterance confidence in the UI.

### v3 — Breadth

Optional built-in authentication and a Postgres backend for shared instances · plugin registry · optional on-device English-only fast path *if* the model landscape changes.

### Explicitly never

A native mobile application. Text-to-speech or spoken translation output. Any feature designed to conceal that recording is taking place.

---

## 10. Risks and open questions

| ID | Risk | Impact | Spike / exit criterion |
|---|---|---|---|
| **R1** | **Serbian ASR quality is unverified and may be poor.** Dual script (Cyrillic/Latin) compounds evaluation and normalisation | High — Serbian is a stated requirement | Benchmark `large-v3`, `large-v3-turbo`, and ≥ 1 cloud provider on ≥ 10 minutes of labelled Serbian, per script, recorded through the browser client. **Exit:** NFR-ACC-3 confirmed or replaced by measured figures, and the Serbian path (local vs. cloud-required) decided from data |
| **R2** | **Live mode (< 2.5 s) may not be achievable locally for RU/SR.** Whisper is not streaming; the sliding window may show unacceptable latency or excessive rewriting | Medium — Balanced mode remains viable | Prototype LocalAgreement-2 over `faster-whisper`; measure median/p95 latency and rewrite rate. **Exit:** NFR-PERF-1 met locally, or Live mode documented as cloud-ASR-only for those languages |
| **R3** | **Diarization from a single near-field microphone at 1.5 m** is materially harder than from an array, and is the weak point of the whole system | High — bad attribution undermines every artifact | Record a real 4-person meeting with a phone on the table; measure DER. **Exit:** NFR-ACC-4/5 table-condition figures confirmed or revised from measurement |
| **R4** | **iOS Safari suspends background tabs aggressively**, and may interrupt capture even with Wake Lock held | High for iOS users | Test a 30-minute iOS recording with the screen on and the tab foregrounded. **Exit:** either iOS works within the documented constraint, or iOS is documented as desk-only with an in-app warning |
| **R5** | **Client-side buffering may not survive a long outage** — IndexedDB quota, tab throttling, or memory pressure | Medium | Test a 5-minute disconnection mid-session on a mid-range Android device. **Exit:** NFR-PERF-7 met, and behaviour past the buffer cap is defined and communicated to the user |
| **R6** | **Browser audio preprocessing** (echo cancellation, noise suppression, AGC) is tuned for single-voice calls and may suppress quieter participants | Medium — could silently harm group recordings | Measure WER and DER with each constraint on and off on the table-condition clip. **Exit:** correct defaults chosen from data, and FR-CAP-11 exposes them |
| **R7** | **Mobile data cost.** Raw PCM is ~115 MB/hour | Medium | Implement the Opus transport mode ([§5.7](#57-audio-transport)) and measure real bandwidth. **Exit:** NFR-RES-4 met, with automatic mode selection verified |
| **R8** | **Mid-session mode switching may be unstable** if pipeline stages hold state | Low — FR-LAT-3 is a *should* | Switch only at VAD boundaries. **Exit:** 20 consecutive switches lose no finalised utterance |
| **R9** | **Whisper hallucinates** on silence, music, and noise | Medium | VAD gating plus a no-speech-probability threshold; add silence and noise cases to the corpus. **Exit:** NFR-ACC-6 met |
| **R10** | **Cloud LLM cost** on long sessions with per-utterance translation | Medium | Measure cost on a 60-minute 4-person session; batch short utterances; per-session budget with local fallback. **Exit:** a documented cost-per-hour figure per configuration |
| **R11** | **Plugins run in-process with full server privileges** | Medium — documented, not eliminated in v1 | Document the trust boundary (NFR-SEC-6). **Exit:** plugin documentation opens with the warning. Process isolation is a v3 consideration |
| **R12** | ~~The server must be awake to record~~ — **closed in v1.4** by specifying a dedicated always-on host ([§6.3](#63-server-hardware-tiers)). Residual exposure (reboots, power cuts, being out of Tailscale reach) is handled by FR-CAP-17 and surfaced by FR-UI-18 | Low | — |
| **R13** | **Russian–English code-switching.** Whisper detects one language per window, but bilingual speakers mix languages inside a single sentence. FR-ASR-7 permits pinning several languages, which the model cannot truly honour | **High for this operator's daily use** — likely to bite harder than R1 | Measure WER on a deliberately code-switched RU/EN clip against (a) auto-detect, (b) pinned `ru`, (c) a cloud backend. **Exit:** either a configuration that handles mixing acceptably is identified, or the limitation is documented and surfaced in the UI when several languages are pinned |

---

## 11. Appendices

### 11.1 On-device ASR assessment (detail)

Recorded because it is the load-bearing evidence for [§7](#7-decision-record-where-does-the-computation-run) and should be re-checked when the landscape changes.

| Engine | Streaming | Model size | Phone realtime factor | Language notes |
|---|---|---|---|---|
| Android `SpeechRecognizer` (on-device) | Native | OS-managed | Realtime, NPU | Best on Pixel; availability is OEM-dependent. Endpoints on silence, so long-form needs restart loops that lose audio at boundaries. Voice-command API, not a transcription API |
| `whisper.cpp` `tiny`/`base` | No (needs sliding window) | 75–150 MB | ~2–5× realtime | Quality inadequate for RU; unusable for SR |
| `whisper.cpp` `small` q5 | No | ~470 MB | ~1–2× realtime | Too slow once the sliding window's 2–3× overhead is applied |
| `whisper.cpp` `large-v3-turbo` q5 | No | ~800 MB | well below realtime | Not viable on phone CPU |
| `sherpa-onnx` streaming Zipformer | Native, ~300 ms | 100–300 MB | Faster than realtime | Technically the best on-device option. English strong; Russian model availability thin; **no Serbian** |
| Vosk | Native | 45 MB – 1.5 GB | Realtime | Russian model exists; quality well below Whisper; no Serbian |

Also relevant: NNAPI deprecated in Android 15, leaving Qualcomm QNN and other vendor delegates as the only NPU paths — per-chip integration work an OSS project cannot sustain.

### 11.2 Model comparison matrix (server-side)

| Model | Type | Languages | RU | SR | Realtime factor (Tier A) | License | Notes |
|---|---|---|---|---|---|---|---|
| Whisper `large-v3-turbo` | Batch ASR | 99 | Good | Fair (verify) | ~8× faster than `large-v3` | MIT | v1 default on Tier A |
| Whisper `medium` int8 | Batch ASR | 99 | Fair | Weak | Near realtime on strong CPU | MIT | Tier B default |
| Whisper `small` int8 | Batch ASR | 99 | Weak | Poor | Realtime on strong CPU | MIT | Low-resource only |
| Parakeet-TDT-v3 | Batch ASR | 25 European | Good | **Likely absent — verify** | Very fast | CC-BY-4.0 | Attractive if Serbian is handled separately |
| Deepgram Nova | Streaming ASR | Many | Good | Verify | Realtime (cloud) | Commercial | Live-mode candidate |
| Silero VAD | VAD | Language-agnostic | — | — | Sub-ms per frame | MIT | v1 default |
| sherpa-onnx diarization | Diarization | Language-agnostic | — | — | Faster than realtime | Apache-2.0 | v1 default, no PyTorch |
| pyannote 3.1 | Diarization | Language-agnostic | — | — | ~1–3× realtime on CPU | MIT (gated models) | Opt-in accuracy backend |
| Opus-MT / MADLAD-400 | NMT | Many | Good | Fair | Fast | Permissive | Local translation fallback |
| NLLB-200-distilled-600M | NMT | 200 | Good | Good | Fast | **CC-BY-NC** | Opt-in only — non-commercial |
| OpenAI frontier model | LLM | Multilingual | Excellent | Good | Cloud | Commercial | Default for translation and plugins. The exact model is set via `llm.model` and is deliberately not pinned here — see [§6.2](#62-technology-choices-and-rejected-alternatives) |
| Local model via Ollama / vLLM / llama.cpp | LLM | Multilingual | Varies by model | Weak | Local | Model-dependent | Offline fallback through the same OpenAI-compatible client. Serbian quality is the limiting factor |

Entries marked *verify* are the subject of R1 and must be measured before v1 release (NFR-EVAL-4).

### 11.3 Hardware notes

One purchase is now assumed — an always-on server ([§6.3](#63-server-hardware-tiers)). Everything else is optional.

| Item | Approx. price | Effect |
|---|---|---|
| **Intel N100/N150 mini PC, 16 GB — recommended server** | ~$170 | Tier B. The cheapest box that reaches the [§4.2](#42-accuracy) targets without sending audio off your network. ~12 W, fanless models exist |
| Raspberry Pi 5, 8–16 GB | ~$100 | Tier C. Lower power and cost; local ASR is limited to `small` at around realtime, so Batch is viable and Live is not |
| Raspberry Pi 4, 4–8 GB | ~$60 | Tier D. Fine as a host, insufficient as an inference machine — **implies cloud ASR** for usable Russian and any Serbian |
| USB SSD for the server, 250 GB+ | ~$35 | **Effectively required on Pi-class hardware.** SD cards fail under sustained write and take the history with them |
| Any decent USB microphone plugged into the **client** | $60–100 | Moves a meeting from table to near-desk condition — still the single largest accuracy gain available, and it needs no code |
| Mini PC with NVIDIA GPU, or an Apple Silicon Mac | $500+ | Tier A: `large-v3-turbo` and Live mode entirely local. A Mac you already own is the cheapest route |
| Powerbank for long phone recordings | ~$30 | Wake Lock keeps the screen on, which drains battery faster than usual |

### 11.4 Document conventions

- Requirement IDs are stable. Withdrawn requirements are marked withdrawn rather than renumbered (see FR-CAP-14).
- Every **Must** requirement has a mechanically checkable acceptance criterion.
- Accuracy figures in [§4.2](#42-accuracy) are **targets pending measurement** (NFR-EVAL-4), not commitments.

---

## Open questions for the maintainer

1. **What will the server run on?** This selects a tier in [§6.3](#63-server-hardware-tiers) and pins model choices. On an Apple Silicon Mac, `whisper.cpp` with Metal may beat `faster-whisper` as the default.
2. **Do you have labelled Serbian audio,** or can you record 10 minutes through the browser client? R1 cannot close without it, and it is the largest unknown in this specification.
3. **Which cloud ASR provider,** if any, as the Live-mode and weak-language fallback? It changes the credential and cost model.
4. **Will you record on iOS?** If yes, R4 moves up in priority — it is the harshest browser environment and worth testing before building much else.
