# Evaluation corpus

Not committed: these are recordings of real conversations, and they are yours.

## Requirements (NFR-EVAL-2)

- ≥ 10 minutes of labelled audio per supported language (`en/`, `ru/`, `sr/`)
- One **desk-condition** multi-speaker recording — device within ~0.5 m
- One **table-condition** multi-speaker recording — a phone ~1.5 m from four people
- Silence and room-tone clips, to keep FR-ASR-9 honest about hallucination

## Recorded through the browser (NFR-EVAL-5)

Record the corpus **with the actual client**, not with a studio recorder or
`ffmpeg` from a file. The browser's capture path — its resampler, and whatever
echo cancellation and noise suppression are enabled — is part of what is being
measured. A corpus recorded around it measures a system that does not exist.

## Layout

```
corpus/
├── en/
│   ├── desk-01.wav          16 kHz mono
│   ├── desk-01.txt          reference transcript, verbatim
│   └── desk-01.json         {"condition": "desk", "speakers": 1}
├── ru/ · sr/                same
└── multi/
    ├── table-4spk.wav
    ├── table-4spk.txt
    ├── table-4spk.json      {"condition": "table", "speakers": 4, "language": "en"}
    └── table-4spk.rttm      reference diarization, NIST RTTM
```

## Producing a reference cheaply

Correcting a machine transcript is several times faster than transcribing from
scratch, and the corrected file is exactly the reference you need:

```bash
curl -s "localhost:8000/api/sessions/<id>/export?format=txt" > en/desk-01.txt
$EDITOR en/desk-01.txt
```

For diarization, export SRT, correct the speaker labels, and convert to RTTM —
or annotate in Audacity's label track and convert.

## Running

```bash
uv run droid-assistant eval
uv run droid-assistant eval --backends faster_whisper:large-v3-turbo,faster_whisper:medium
uv run droid-assistant eval --language sr --json results.json
```
