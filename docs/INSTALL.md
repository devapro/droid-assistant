# Install

The goal of this page: a clean machine to a working recording in under thirty
minutes, following only these steps.

## 1. Pick the machine

The server is expected to be a small always-on box. See
[SRS §6.3](./SRS.md#63-server-hardware-tiers) for the tiers; the short version
is that an **Intel N100/N150 mini PC with 16 GB (~$170)** is the cheapest tier
where the accuracy targets are reachable without sending audio off your network.

A Raspberry Pi 4 is an excellent host and a poor inference machine. It will run
everything except the models, so it implies cloud ASR. That is supported and
documented — it is simply a different trade than "local-first" was chosen for.

## 2. Install

### Docker (recommended)

```bash
git clone https://github.com/arsenii/droid-assistant
cd droid-assistant
cp .env.example .env       # set OPENAI_API_KEY for translation and plugins
docker compose up -d
docker compose exec droid-assistant droid-assistant models download
```

`models download` fetches everything the configuration routes to — the default
model plus anything `asr.by_language` points at — so the list never has to be
maintained by hand. `droid-assistant models list` shows what is on disk and what
uses it. Both are also in the browser, under Settings → Speech models.

With an NVIDIA GPU, use `docker compose -f compose.cuda.yml up -d` instead. It
needs the NVIDIA Container Toolkit on the host.

### Native

```bash
uv sync --extra local --extra cloud
uv run droid-assistant models download
uv run droid-assistant serve
```

`uv` installs its own Python, so the system Python version does not matter.

On macOS, install `ffmpeg` (`brew install ffmpeg`) unless you want session audio
stored as WAV — roughly eight times the size of the configured Opus.

## 3. HTTPS — not optional

Browsers refuse microphone access outside a secure context. `http://localhost`
qualifies, so a browser **on the server itself** works immediately. Every other
device needs real HTTPS.

### Tailscale Serve — the primary path

```bash
tailscale up
tailscale serve --bg 8000
# → https://<machine>.<tailnet>.ts.net
```

Ten minutes, a genuine certificate, no port forwarding, works from outside your
LAN, and it provides the access control that v1 deliberately does not implement
itself (NFR-SEC-2).

Make it survive a reboot:

```bash
sudo cp deploy/tailscale-serve.service /etc/systemd/system/
sudo systemctl enable --now tailscale-serve
```

### Alternatives

| Option | Trade-off |
|---|---|
| Caddy with a DNS-01 certificate for a domain pointing at the LAN IP | Needs a domain you control. The certificate is valid for 90 days fully offline once issued |
| `chrome://flags/#unsafely-treat-insecure-origin-as-secure` | Per-device manual allowlist. Adequate for one personal device; unsuitable as a documented default |
| `mkcert` with an installed root CA | Per-device CA installation — more friction than Tailscale, for less |

**Do not expose this to the public internet.** There is no authentication. If
you must, put a reverse proxy that authenticates in front of it.

## 4. Always-on

Three properties make a server actually always-on, and the install should verify
each ([SRS §8.5](./SRS.md#85-running-it-always-on)).

**Starts on power.**

```bash
sudo useradd --system --home /opt/droid-assistant droid
sudo cp deploy/droid-assistant.service /etc/systemd/system/
sudo systemctl enable --now droid-assistant
```

On a mini PC, also enable *restore on AC loss* in the BIOS. systemd cannot start
a machine that is switched off.

**Reachable without intervention.** `tailscale up` persists across reboots; the
unit above re-applies `tailscale serve`. The hostname never changes, so client
bookmarks keep working.

**Reports its own health.** `GET /api/health` returns model status, disk
headroom, and backend reachability. `droid-assistant doctor` checks the same
from a shell.

## 5. Storage

All mutable state lives under one directory ([SRS §5.9](./SRS.md#59-storage-layout)):

```
$DROID_DATA/
├── droid.db          sessions, utterances, speakers, artifacts, the FTS index
├── droid.db-wal      must be copied together with droid.db
├── audio/2026/08/14/ one file per session
├── models/           downloaded weights — regenerable, skip in backups
└── config.toml       your configuration
```

**On Pi-class hardware, put this on a USB SSD.**

```bash
sudo mkdir -p /mnt/ssd/droid
echo 'DROID_DATA_DIR=/mnt/ssd/droid' | sudo tee /etc/droid-assistant/env
```

SD cards fail under sustained write, and they take your entire history with
them. This is the single most consequential line on this page.

## 6. Backups

A backup you have to remember is not a backup. Schedule it:

```bash
sudo cp deploy/droid-backup.{service,timer} /etc/systemd/system/
sudo systemctl enable --now droid-backup.timer
```

That runs `droid-assistant backup`, which checkpoints the WAL and copies
everything except `models/`. Restoring is copying the directory back with the
server stopped.

## 7. Verify

```bash
uv run droid-assistant doctor
```

It checks disk space, model presence, Python extras, GPU, credentials, ffmpeg,
and HTTPS reachability — and each failure prints the command that fixes it.

Then, from your phone:

1. Open the HTTPS URL.
2. Press Record and grant microphone permission.
3. Speak for thirty seconds.
4. Press Stop and read the transcript.

If the transcript is empty, run `doctor` again — the usual causes are a missing
VAD model and a muted microphone, and both are reported there.

## Troubleshooting

**Russian (or any non-English language) transcribes badly.** Model size matters
far more away from English, and a language-specialised model can beat Whisper
outright on its own language. Open Settings → Speech models, download one, and
route just that language to it — English keeps the multilingual model. See
[CONFIGURATION.md](./CONFIGURATION.md#asrby_language--a-different-engine-per-language).

**The first start takes ages and Settings says the model is loading.** That is
a first run downloading the speech model — `large-v3-turbo` is about 1.5 GB. The
server answers throughout and reports progress in `GET /api/health`; recording
is refused until it finishes, with that reason. Set `HF_TOKEN` in `.env` to lift
Hugging Face's anonymous rate limit, which is usually what makes it slow.

**"Record" does nothing, or there is no permission prompt.** The page is not on
a secure origin. Check for `https://` in the address bar. See step 3.

**Recording stops when the screen locks.** Wake Lock was unavailable or was
denied. The app warns when it cannot acquire one; keep the screen on and the tab
foregrounded. This is worst on iOS.

**The transcript is empty but the level meter moved.** Usually the Silero VAD
model is missing — `droid-assistant models download`. Failing that, set
`vad.backend = "energy"` temporarily to confirm, and check `doctor`.

**Everything is very slow.** Check the tier in `doctor` output. A `large-v3` on
a Pi is below realtime by a wide margin; use Batch mode, a smaller model, or
cloud ASR.

**Sessions stop after about ninety seconds when I switch apps.** That is the
disconnect grace period finalising a session whose client vanished. Increase
`server.disconnect_grace_s`, or keep the tab foregrounded.
