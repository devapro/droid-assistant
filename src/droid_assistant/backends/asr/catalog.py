"""What speech models exist, which are on disk, and how to fetch one.

Choosing a model is the single most consequential configuration decision in this
system, and until now it could only be made by editing `config.toml` and
restarting. That is the wrong shape for the decision it is: the right model
depends on the *language being spoken*, which changes between one recording and
the next, and the operator has no way of knowing from the config file which
weights are actually on disk.

So one module answers three questions that were previously scattered:

* **What could I use?** `CATALOG` — the models worth offering, with their
  language coverage and rough download size.
* **What do I have?** `state()` — whether the weights are present, checked
  against each backend's own on-disk layout rather than guessed.
* **How do I get the rest?** `download()` — the same code path the CLI and the
  web UI both use, so "fetch it" means one thing.

An entry here is a *suggestion*, not a constraint: `asr.model` accepts any
CTranslate2 model id or local path, and `spec_for` synthesises an entry for one
it has never heard of so the UI can still show what is selected.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from ...config import Settings
from .deepgram import NOVA_2_LANGUAGES, NOVA_3_LANGUAGES
from .faster_whisper import WHISPER_LANGUAGES
from .gigaam import MODELS as GIGAAM_MODELS

log = logging.getLogger(__name__)

#: `faster-whisper` publishes CTranslate2 conversions under these repositories.
#: Copied rather than imported: the upstream mapping is a private name, and this
#: module has to answer "is it downloaded?" without importing an optional extra.
#: `_verify_repo_map` in the test suite fails if the two ever disagree.
WHISPER_REPOS = {
    "tiny": "Systran/faster-whisper-tiny",
    "tiny.en": "Systran/faster-whisper-tiny.en",
    "base": "Systran/faster-whisper-base",
    "base.en": "Systran/faster-whisper-base.en",
    "small": "Systran/faster-whisper-small",
    "small.en": "Systran/faster-whisper-small.en",
    "medium": "Systran/faster-whisper-medium",
    "medium.en": "Systran/faster-whisper-medium.en",
    "large-v1": "Systran/faster-whisper-large-v1",
    "large-v2": "Systran/faster-whisper-large-v2",
    "large-v3": "Systran/faster-whisper-large-v3",
    "large": "Systran/faster-whisper-large-v3",
    "large-v3-turbo": "mobiuslabsgmbh/faster-whisper-large-v3-turbo",
    "turbo": "mobiuslabsgmbh/faster-whisper-large-v3-turbo",
    "distil-large-v2": "Systran/faster-distil-whisper-large-v2",
    "distil-large-v3": "Systran/faster-distil-whisper-large-v3",
    "distil-large-v3.5": "distil-whisper/distil-large-v3.5-ct2",
    "distil-medium.en": "Systran/faster-distil-whisper-medium.en",
    "distil-small.en": "Systran/faster-distil-whisper-small.en",
}


@dataclass(frozen=True, slots=True)
class ModelSpec:
    """One offerable `(backend, model)` pair."""

    backend: str
    model: str
    #: Languages the weights actually cover. `None` means broadly multilingual —
    #: the distinction matters, because a single-language model handed other
    #: speech returns confident nonsense rather than an error.
    languages: frozenset[str] | None
    #: Approximate download, MB. Reported only while the model is absent; once it
    #: is on disk the measured size is used instead.
    size_mb: int
    #: False for cloud backends, which are "available" rather than "downloaded".
    local: bool = True
    note: str = ""

    @property
    def id(self) -> str:
        return f"{self.backend}:{self.model}"

    def covers(self, language: str) -> bool:
        return self.languages is None or language.split("-")[0].lower() in self.languages


RU = frozenset({"ru"})
SR = frozenset({"sr"})

#: The models worth putting in front of an operator. Deliberately shorter than
#: everything that would work: a list of forty entries is not a choice, it is a
#: search problem. Measured numbers for the Whisper sizes are in
#: `docs/CONFIGURATION.md`.
CATALOG: tuple[ModelSpec, ...] = (
    ModelSpec("faster_whisper", "tiny", WHISPER_LANGUAGES, 75, note="fastest; test use only"),
    ModelSpec("faster_whisper", "base", WHISPER_LANGUAGES, 145),
    ModelSpec("faster_whisper", "small", WHISPER_LANGUAGES, 484, note="good CPU compromise"),
    ModelSpec("faster_whisper", "medium", WHISPER_LANGUAGES, 1530),
    ModelSpec(
        "faster_whisper",
        "large-v3-turbo",
        WHISPER_LANGUAGES,
        2300,
        note="the default: best accuracy per second of compute",
    ),
    ModelSpec("faster_whisper", "large-v3", WHISPER_LANGUAGES, 3090, note="slowest, marginal gain"),
    ModelSpec(
        "faster_whisper",
        "dvislobokov/faster-whisper-large-v3-turbo-russian",
        RU,
        1600,
        note="Russian fine-tune of turbo; already CTranslate2, so no conversion",
    ),
    ModelSpec(
        "gigaam",
        "v3-rnnt",
        RU,
        220,
        note="Russian-only, and far faster than Whisper at comparable accuracy",
    ),
    ModelSpec("gigaam", "v3-ctc", RU, 215, note="Russian-only; faster than v3-rnnt, less accurate"),
    ModelSpec("gigaam", "v2-rnnt", RU, 220, note="Russian-only, previous generation"),
    ModelSpec("gigaam", "v2-ctc", RU, 215, note="Russian-only, previous generation"),
    # Serbian has no equivalent of GigaAM — no specialised architecture, no
    # maintained conversion, nobody's benchmark. What exists is community
    # Whisper fine-tunes, and these two are the ones published in CTranslate2
    # already, so they cost no conversion step. Their own cards report figures
    # on read speech from their training distribution; that is a reason to
    # measure them (R1), not a reason to trust them.
    ModelSpec(
        "faster_whisper",
        "Sagicc/faster-whisper-large-v3-sr",
        SR,
        3090,
        note="Serbian fine-tune of large-v3; already CTranslate2, so no conversion",
    ),
    ModelSpec(
        "faster_whisper",
        "Sagicc/faster-whisper-medium-sr",
        SR,
        1530,
        note="Serbian fine-tune of medium, where large will not fit",
    ),
    # Cloud entries carry no weights. They are here because routing one language
    # to a cloud backend is a legitimate answer — Serbian is the case this
    # project actually has — and the routing UI would be lying by omission.
    ModelSpec("openai", "gpt-4o-transcribe", None, 0, local=False, note="OpenAI, no timestamps"),
    ModelSpec(
        "openai", "gpt-4o-transcribe-diarize", None, 0, local=False, note="OpenAI, speaker labels"
    ),
    ModelSpec("openai", "whisper-1", None, 0, local=False, note="OpenAI, word timestamps"),
    ModelSpec(
        "deepgram",
        "nova-2",
        NOVA_2_LANGUAGES,
        0,
        local=False,
        note="Deepgram; fast and accurate, but no Serbian",
    ),
    ModelSpec(
        "deepgram",
        "nova-3",
        NOVA_3_LANGUAGES,
        0,
        local=False,
        note="Deepgram; the one cloud model that has Serbian",
    ),
)

_BY_ID = {spec.id: spec for spec in CATALOG}


def parse(model_id: str, default_backend: str) -> tuple[str, str]:
    """Split `"gigaam:v3-rnnt"` into its parts.

    A bare name means "the current backend", so `"small"` keeps working where a
    model was always named without one. Hugging Face ids contain a slash but
    never a colon before it, which is what makes this unambiguous.
    """
    backend, sep, model = model_id.partition(":")
    if not sep or "/" in backend:
        return default_backend, model_id
    return backend, model


def spec_for(backend: str, model: str) -> ModelSpec:
    """The catalogued entry, or a synthesised one for a custom model.

    A custom CTranslate2 path or an unlisted Hugging Face fine-tune is a
    perfectly good configuration; the UI still has to be able to display it.
    """
    known = _BY_ID.get(f"{backend}:{model}")
    if known is not None:
        return known
    return ModelSpec(
        backend=backend,
        model=model,
        languages=WHISPER_LANGUAGES if backend in {"faster_whisper", "whisper_cpp"} else None,
        size_mb=0,
        local=backend not in {"openai", "deepgram"},
        note="not in the catalogue — a custom model id or path",
    )


# --- what is on disk --------------------------------------------------------


def location(spec: ModelSpec, models_dir: Path) -> Path | None:
    """Where the weights live, or None for a backend that keeps none locally."""
    match spec.backend:
        case "faster_whisper":
            if "/" in spec.model and Path(spec.model).is_absolute():
                return Path(spec.model)
            repo = WHISPER_REPOS.get(spec.model, spec.model)
            return models_dir / f"models--{repo.replace('/', '--')}"
        case "gigaam":
            directory, _url = GIGAAM_MODELS.get(spec.model, (spec.model, ""))
            return models_dir / directory
        case "whisper_cpp":
            return models_dir / f"ggml-{spec.model}.bin"
        case _:
            return None


def _present(spec: ModelSpec, path: Path) -> bool:
    """Whether `path` holds usable weights, by each backend's own layout.

    Checked against a file that only exists after a *complete* download, so a
    directory left behind by an interrupted one reads as absent rather than as
    a model that mysteriously fails to load.
    """
    match spec.backend:
        case "faster_whisper":
            if path.is_file():  # an explicit local file path
                return True
            return any(path.glob("snapshots/*/model.bin")) or (path / "model.bin").exists()
        case "gigaam":
            return (path / "tokens.txt").exists()
        case "whisper_cpp":
            return path.exists()
        case _:
            return False


def _size_mb(path: Path) -> int:
    """Bytes on disk, in MB.

    Symlinks are skipped rather than followed: the Hugging Face cache stores one
    copy under `blobs/` and symlinks it into `snapshots/`, so following them
    reports every model at twice its real size.
    """
    if path.is_file():
        return round(path.stat().st_size / 1_048_576)
    total = sum(f.stat().st_size for f in path.rglob("*") if f.is_file() and not f.is_symlink())
    return round(total / 1_048_576)


def state(spec: ModelSpec, settings: Settings) -> tuple[str, int]:
    """`(state, size_mb)` where state is present | absent | ready | unavailable.

    Cloud backends get `ready`/`unavailable` rather than present/absent: there is
    nothing to download, and what decides whether they can be used is a
    credential.
    """
    if not spec.local:
        env = (
            settings.asr.openai.api_key_env
            if spec.backend == "openai"
            else settings.asr.deepgram.api_key_env
        )
        return ("ready" if settings.secret(env) else "unavailable"), 0
    path = location(spec, settings.models_dir)
    if path is None or not path.exists() or not _present(spec, path):
        return "absent", spec.size_mb
    return "present", _size_mb(path)


# --- what this configuration needs ------------------------------------------


def required(settings: Settings) -> list[ModelSpec]:
    """Every model this configuration can route to, plus `asr.preload`.

    This is what `models download` fetches, and it is derived rather than
    listed: a per-language route to a model nobody remembered to download is a
    session that fails at the moment of recording, which is the worst possible
    time to discover it.
    """
    wanted: dict[str, ModelSpec] = {}

    def add(backend: str, model: str) -> None:
        spec = spec_for(backend, model)
        if spec.local:
            wanted.setdefault(spec.id, spec)

    add(settings.asr.backend, settings.asr.model)
    for language in settings.capture.languages:
        add(*settings.asr.for_language(language))
    for entry in settings.asr.preload:
        add(*parse(entry, settings.asr.backend))
    return list(wanted.values())


def row(spec: ModelSpec, settings: Settings) -> dict[str, object]:
    """One model as the API reports it."""
    model_state, size = state(spec, settings)
    return {
        "id": spec.id,
        "backend": spec.backend,
        "model": spec.model,
        "state": model_state,
        "local": spec.local,
        "size_mb": size,
        "note": spec.note,
        # `null` reads as "no restriction" in the client, which is what a
        # multilingual model means. An explicit list means exactly those, and
        # the client refuses to offer the model for anything else.
        "languages": sorted(spec.languages) if spec.languages else None,
    }


def inventory(settings: Settings) -> list[dict[str, object]]:
    """The catalogue plus whatever this configuration references, with state.

    Sorted so the models you can actually use come first — an operator scanning
    this list is looking for something to switch *to*.
    """
    specs = {spec.id: spec for spec in CATALOG}
    for backend, model in _configured(settings):
        specs.setdefault(f"{backend}:{model}", spec_for(backend, model))

    rows = [row(spec, settings) for spec in specs.values()]
    order = {"present": 0, "ready": 1, "absent": 2, "unavailable": 3}
    rows.sort(key=lambda r: (order.get(str(r["state"]), 9), str(r["id"])))
    return rows


def _configured(settings: Settings) -> list[tuple[str, str]]:
    pairs = [(settings.asr.backend, settings.asr.model)]
    pairs += [settings.asr.for_language(code) for code in settings.capture.languages]
    pairs += [parse(entry, settings.asr.backend) for entry in settings.asr.preload]
    return pairs


# --- fetching ---------------------------------------------------------------


def download(spec: ModelSpec, models_dir: Path) -> Path:
    """Fetch the weights. Blocking and slow — call it in a thread.

    Idempotent: an already-present model returns immediately, so this is safe to
    call on every start.
    """
    if not spec.local:
        raise ValueError(f"{spec.id} is a cloud backend; there is nothing to download")

    models_dir.mkdir(parents=True, exist_ok=True)
    path = location(spec, models_dir)
    if path is not None and path.exists() and _present(spec, path):
        return path

    match spec.backend:
        case "faster_whisper":
            _download_whisper(spec, models_dir)
        case "gigaam":
            _download_gigaam(spec, models_dir)
        case "whisper_cpp":
            raise ValueError(
                "whisper.cpp downloads its own weights on first load; start a session "
                "with it selected, or place ggml-*.bin in the models directory by hand"
            )
        case _:
            raise ValueError(f"don't know how to download {spec.id!r}")

    resolved = location(spec, models_dir)
    if resolved is None or not _present(spec, resolved):
        raise RuntimeError(f"{spec.id} still looks incomplete after downloading; try again")
    return resolved


def _download_whisper(spec: ModelSpec, models_dir: Path) -> None:
    try:
        from faster_whisper.utils import download_model
    except ImportError as exc:
        raise RuntimeError("faster-whisper is not installed: uv sync --extra local") from exc
    log.info("downloading ASR model", extra={"model": spec.model})
    # Downloads without constructing a model, so this costs no RAM and no load
    # time — which matters when it runs on a request thread.
    download_model(spec.model, cache_dir=str(models_dir))


def _download_gigaam(spec: ModelSpec, models_dir: Path) -> None:
    import tarfile
    import urllib.request

    entry = GIGAAM_MODELS.get(spec.model)
    if entry is None:
        raise ValueError(
            f"unknown GigaAM variant {spec.model!r}; one of: {', '.join(GIGAAM_MODELS)}"
        )
    directory, url = entry
    archive = models_dir / f"{directory}.tar.bz2"
    log.info("downloading GigaAM", extra={"variant": spec.model, "url": url})
    # The URL comes from this module's own catalogue, never from a request.
    with urllib.request.urlopen(url, timeout=300) as response:
        partial = archive.with_suffix(".part")
        partial.write_bytes(response.read())
        partial.rename(archive)
    with tarfile.open(archive) as tar:
        tar.extractall(models_dir, filter="data")
    archive.unlink(missing_ok=True)
