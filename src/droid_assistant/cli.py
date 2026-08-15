"""`droid-assistant` — serve · models · doctor · eval · backup.

`doctor` is the one that earns its place: it checks the things that actually go
wrong on a fresh install — a missing model, no HTTPS, a full disk, an
unreachable backend — and each failure prints the command that fixes it
(SRS §8.3, NFR-REL-6).
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.table import Table

from . import __version__
from .config import ConfigError, Settings
from .config import load as load_settings
from .logging import configure as configure_logging

app = typer.Typer(
    name="droid-assistant",
    help="Self-hosted conversation intelligence with a browser client.",
    no_args_is_help=True,
    add_completion=False,
)
models_app = typer.Typer(help="Download and inspect model weights.")
app.add_typer(models_app, name="models")

console = Console()
err = Console(stderr=True)

ConfigOption = Annotated[
    Path | None, typer.Option("--config", "-c", help="Path to config.toml", envvar="DROID_CONFIG")
]


def _settings(path: Path | None) -> Settings:
    try:
        return load_settings(path)
    except ConfigError as exc:
        err.print(f"[red]{exc}[/red]")
        raise typer.Exit(2) from exc


@app.callback()
def main_callback(
    version: Annotated[bool, typer.Option("--version", help="Print the version and exit.")] = False,
) -> None:
    if version:
        console.print(__version__)
        raise typer.Exit


# ---------------------------------------------------------------------------
# serve
# ---------------------------------------------------------------------------


@app.command()
def serve(
    config: ConfigOption = None,
    host: Annotated[str | None, typer.Option(help="Bind address. Defaults to loopback.")] = None,
    port: Annotated[int | None, typer.Option(help="Bind port.")] = None,
    reload: Annotated[bool, typer.Option(help="Reload on source changes (development).")] = False,
    log_level: Annotated[str | None, typer.Option(help="debug | info | warning | error")] = None,
) -> None:
    """Run the server."""
    import uvicorn

    settings = _settings(config)
    bind_host = host or settings.server.host
    bind_port = port or settings.server.port
    level = log_level or settings.server.log_level
    configure_logging(level)

    if bind_host not in {"127.0.0.1", "::1", "localhost"}:
        # NFR-SEC-2, said where it will actually be read.
        err.print(
            f"[yellow]Binding to {bind_host}: this instance has no authentication. Access "
            "control is the network's job — put it behind Tailscale, or behind a reverse "
            "proxy that authenticates. Do not expose it to the internet.[/yellow]"
        )

    console.print(f"[bold]droid-assistant[/bold] {__version__}")
    console.print(f"  data     {settings.server.data_dir}")
    console.print(f"  asr      {settings.asr.backend}:{settings.asr.model}")
    console.print(f"  listen   http://{bind_host}:{bind_port}")
    if bind_host in {"127.0.0.1", "localhost"}:
        console.print(
            "  [dim]Microphone access needs a secure context. localhost qualifies; any "
            "other device needs HTTPS — see `droid-assistant doctor`.[/dim]"
        )

    uvicorn.run(
        "droid_assistant.api.app:create_app",
        factory=True,
        host=bind_host,
        port=bind_port,
        reload=reload,
        log_config=None,
        ws_max_size=16 * 1024 * 1024,
    )


# ---------------------------------------------------------------------------
# models
# ---------------------------------------------------------------------------

VAD_URL = "https://github.com/snakers4/silero-vad/raw/master/src/silero_vad/data/silero_vad.onnx"
SEGMENTATION_URL = (
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-segmentation-models/"
    "sherpa-onnx-pyannote-segmentation-3-0.tar.bz2"
)
EMBEDDING_URL = (
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/"
    "nemo_en_titanet_small.onnx"
)


def _download(url: str, target: Path, label: str) -> bool:
    if target.exists():
        console.print(f"  [green]✓[/green] {label} (already present)")
        return True
    target.parent.mkdir(parents=True, exist_ok=True)
    console.print(f"  [cyan]↓[/cyan] {label} …")
    try:
        with urllib.request.urlopen(url, timeout=120) as response:
            tmp = target.with_suffix(target.suffix + ".part")
            tmp.write_bytes(response.read())
            tmp.rename(target)
    except Exception as exc:
        err.print(f"  [red]✗[/red] {label}: {exc}")
        err.print(f"    Fetch it by hand:  curl -L -o {target} {url}")
        return False
    console.print(f"  [green]✓[/green] {label}")
    return True


@models_app.command("download")
def models_download(
    config: ConfigOption = None,
    asr: Annotated[
        str | None,
        typer.Option(help="One model to fetch, e.g. gigaam:v3-rnnt. Default: every one in use."),
    ] = None,
    vad: Annotated[bool, typer.Option(help="Fetch the Silero VAD model.")] = True,
    diarization: Annotated[bool, typer.Option(help="Fetch diarization models.")] = True,
) -> None:
    """Download the weights the configured backends need (NFR-PORT-4)."""
    settings = _settings(config)
    settings.ensure_dirs()
    models = settings.models_dir
    console.print(f"Models directory: [bold]{models}[/bold]\n")

    ok = True
    if vad:
        ok &= _download(VAD_URL, models / "silero_vad.onnx", "Silero VAD")

    if diarization and settings.diarization.enabled:
        ok &= _download(
            EMBEDDING_URL,
            models / f"{settings.diarization.embedding_model}.onnx",
            "speaker embedding (TitaNet)",
        )
        archive = models / "segmentation.tar.bz2"
        target_dir = models / settings.diarization.segmentation_model
        if not (target_dir / "model.onnx").exists():
            if _download(SEGMENTATION_URL, archive, "speaker segmentation"):
                import tarfile

                with tarfile.open(archive) as tar:
                    tar.extractall(models, filter="data")
                archive.unlink(missing_ok=True)
        else:
            console.print("  [green]✓[/green] speaker segmentation (already present)")

    # Every model this configuration can route to, not just the default. A
    # per-language route (`asr.by_language`) to a model nobody downloaded is a
    # session that fails at the moment of recording.
    from .backends.asr import catalog

    if asr:
        backend, model = catalog.parse(asr, settings.asr.backend)
        wanted = [catalog.spec_for(backend, model)]
    else:
        wanted = catalog.required(settings)

    for spec in wanted:
        state, size = catalog.state(spec, settings)
        if state == "present":
            console.print(f"  [green]✓[/green] ASR {spec.id} (already present, {size} MB)")
            continue
        console.print(f"  [cyan]↓[/cyan] ASR {spec.id} … [dim](~{spec.size_mb} MB)[/dim]")
        try:
            catalog.download(spec, models)
            console.print(f"  [green]✓[/green] ASR {spec.id}")
        except Exception as exc:
            err.print(f"  [red]✗[/red] ASR {spec.id}: {exc}")
            ok = False

    console.print()
    if not ok:
        raise typer.Exit(1)
    console.print("[green]All models present.[/green]")


#: Whisper fine-tunes worth trying where the stock model is weak. Listed rather
#: than defaulted to, because a fine-tune trades breadth for depth: better on
#: the language it was tuned for, usually worse on everything else, including
#: code-switching. Measure before adopting one (NFR-EVAL-4).
SPECIALISED_MODELS = {
    "ru": [
        ("antony66/whisper-large-v3-russian", "Russian, tuned on telephony and podcast speech"),
        ("bond005/whisper-large-v3-ru-podlodka", "Russian, tuned on spontaneous conversation"),
        ("dvislobokov/faster-whisper-large-v3-turbo-russian", "Russian, already CTranslate2"),
    ],
    # Serbian is R1, the largest open question in the specification, and these
    # are small community fine-tunes rather than a maintained project — closer
    # to leads than to recommendations. They are listed because the alternative
    # was printing "no suggestions recorded" for a language this project claims
    # to support.
    "sr": [
        ("Sagicc/faster-whisper-large-v3-sr", "Serbian, already CTranslate2"),
        ("Sagicc/faster-whisper-medium-sr", "Serbian, medium — where large will not fit"),
        ("Sagicc/whisper-large-v3-turbo-sr-v2", "Serbian tune of turbo; convert it first"),
    ],
}


@models_app.command("suggest")
def models_suggest(
    language: Annotated[str | None, typer.Option(help="BCP-47 code, e.g. ru")] = None,
) -> None:
    """List community models specialised for a language.

    Whisper's multilingual capacity is heavily weighted to English, and the gap
    widens as the model shrinks. A specialised model can close it — but it is a
    trade, not a free win, so measure it against your own audio with
    `droid-assistant eval` before switching.
    """
    codes = [language] if language else sorted(SPECIALISED_MODELS)
    for code in codes:
        entries = SPECIALISED_MODELS.get(code)
        if not entries:
            err.print(f"[yellow]No suggestions recorded for {code!r}.[/yellow]")
            continue
        table = Table(title=f"Specialised models for {code}")
        table.add_column("Model")
        table.add_column("Notes")
        for name, note in entries:
            table.add_row(name, note)
        console.print(table)
    console.print(
        "\n[dim]Already in CTranslate2 format: set asr.model to the name.\n"
        "A plain Hugging Face model: convert it first with "
        "`droid-assistant models convert <name>`.\n"
        "Either way, measure it: droid-assistant eval --backends "
        "faster_whisper:large-v3-turbo,faster_whisper:<the-new-one>[/dim]"
    )


@models_app.command("convert")
def models_convert(
    model: Annotated[str, typer.Argument(help="Hugging Face model id or local path")],
    config: ConfigOption = None,
    quantization: Annotated[
        str, typer.Option(help="int8, int8_float16, float16, float32")
    ] = "int8",
) -> None:
    """Convert a Hugging Face Whisper model to CTranslate2.

    `faster-whisper` runs CTranslate2, not Transformers, so a fine-tune
    published in the usual Hugging Face format has to be converted once before
    it can be used. After this, set `asr.model` to the printed path.
    """
    settings = _settings(config)
    settings.ensure_dirs()
    target = settings.models_dir / f"{model.replace('/', '--')}-ct2"

    if target.exists():
        console.print(f"[green]Already converted:[/green] {target}")
        return

    try:
        from ctranslate2.converters import TransformersConverter
    except ImportError as exc:
        err.print("[red]Conversion needs CTranslate2 and Transformers:[/red] uv sync --extra local")
        raise typer.Exit(2) from exc

    console.print(f"Converting [bold]{model}[/bold] → {target}")
    console.print("[dim]This downloads the full model once; it is slow the first time.[/dim]")
    try:
        TransformersConverter(
            model, copy_files=["tokenizer.json", "preprocessor_config.json"]
        ).convert(str(target), quantization=quantization)
    except Exception as exc:
        err.print(f"[red]Conversion failed:[/red] {exc}")
        raise typer.Exit(1) from exc

    console.print(f'\n[green]Done.[/green] Use it with:\n  asr.model = "{target}"')
    console.print(
        "[dim]Then measure it rather than assuming:\n"
        "  droid-assistant eval --backends "
        f"faster_whisper:large-v3-turbo,faster_whisper:{target}[/dim]"
    )


@models_app.command("gigaam")
def models_gigaam(
    variant: Annotated[str, typer.Argument(help="v3-rnnt | v3-ctc | v2-rnnt | v2-ctc")] = "v3-rnnt",
    config: ConfigOption = None,
) -> None:
    """Download GigaAM, the Russian-specialised local model.

    It runs through sherpa-onnx, which is already a dependency, so this needs no
    PyTorch and no gated download. Russian only — pair it with `asr.by_language`
    so other languages keep using a multilingual model.
    """
    import tarfile

    from .backends.asr.gigaam import MODELS

    settings = _settings(config)
    settings.ensure_dirs()
    entry = MODELS.get(variant)
    if entry is None:
        err.print(f"[red]Unknown variant {variant!r}.[/red] Available: {', '.join(MODELS)}")
        raise typer.Exit(2)

    directory, url = entry
    target = settings.models_dir / directory
    if (target / "tokens.txt").exists():
        console.print(f"  [green]✓[/green] GigaAM {variant} (already present)")
        return

    archive = settings.models_dir / f"{directory}.tar.bz2"
    if not _download(url, archive, f"GigaAM {variant}"):
        raise typer.Exit(1)
    console.print("  [cyan]…[/cyan] extracting")
    with tarfile.open(archive) as tar:
        tar.extractall(settings.models_dir, filter="data")
    archive.unlink(missing_ok=True)

    console.print(
        "\n[green]Ready.[/green] Route Russian to it — leaving every other language on the\n"
        "model you use now — from Settings → Speech models, or in config.toml:\n"
        "\n\n  " + r"\[asr.by_language.ru]"
        '\n  backend = "gigaam"\n'
        "\n[dim]Then compare it against what you use now:\n"
        "  droid-assistant eval --language ru --backends "
        "faster_whisper:large-v3-turbo,gigaam[/dim]"
    )


@models_app.command("list")
def models_list(
    config: ConfigOption = None,
    files: Annotated[bool, typer.Option(help="Also list every file on disk.")] = False,
) -> None:
    """Show which speech models are available and which are in use."""
    from .backends.asr import catalog

    settings = _settings(config)
    routing = {
        code: f"{b}:{m}"
        for code in settings.capture.languages
        for b, m in [settings.asr.for_language(code)]
    }
    default_id = f"{settings.asr.backend}:{settings.asr.model}"

    catalogue = Table(title="Speech models")
    catalogue.add_column("Model")
    catalogue.add_column("State")
    catalogue.add_column("Size", justify="right")
    catalogue.add_column("Languages")
    catalogue.add_column("Used for")
    for row in catalog.inventory(settings):
        used = [code for code, mid in routing.items() if mid == row["id"]]
        if row["id"] == default_id:
            used.append("default")
        languages = row["languages"]
        state = str(row["state"])
        # Printing all 99 of Whisper's languages makes the table unreadable and
        # tells nobody anything; the count is the part that carries meaning.
        if languages is None:
            coverage = "any"
        elif len(languages) > 4:  # type: ignore[arg-type]
            coverage = f"{len(languages)} languages"  # type: ignore[arg-type]
        else:
            coverage = ", ".join(languages)  # type: ignore[arg-type]
        catalogue.add_row(
            str(row["id"]),
            {
                "present": "[green]on disk[/green]",
                "ready": "[green]cloud[/green]",
                "absent": "[dim]not downloaded[/dim]",
                "unavailable": "[yellow]no credential[/yellow]",
            }.get(state, state),
            f"{row['size_mb']} MB" if row["size_mb"] else "—",
            coverage,
            ", ".join(used),
        )
    console.print(catalogue)
    console.print(
        "\n[dim]Fetch one:   droid-assistant models download --asr <model>\n"
        "Route a language to it, from Settings → Speech models or in config.toml:\n"
        r"  \[asr.by_language.ru]"
        '\n  backend = "gigaam"[/dim]'
    )

    if not files:
        return

    table = Table(title=f"Files in {settings.models_dir}")
    table.add_column("File")
    table.add_column("Size", justify="right")
    if not settings.models_dir.exists():
        console.print(
            "[yellow]No models directory yet. Run: droid-assistant models download[/yellow]"
        )
        return
    total = 0
    for path in sorted(settings.models_dir.rglob("*")):
        if path.is_file():
            size = path.stat().st_size
            total += size
            table.add_row(str(path.relative_to(settings.models_dir)), _human(size))
    table.add_section()
    table.add_row("[bold]total[/bold]", f"[bold]{_human(total)}[/bold]")
    console.print(table)


def _human(size: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.0f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------


@app.command()
def doctor(
    config: ConfigOption = None,
    url: Annotated[
        str | None, typer.Option(help="Public URL to test for HTTPS reachability.")
    ] = None,
) -> None:
    """Check models, GPU, disk, HTTPS, and backend reachability (SRS §8.3)."""
    configure_logging("warning")
    settings = _settings(config)
    results: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str) -> None:
        results.append((name, ok, detail))

    # --- storage
    settings.ensure_dirs()
    usage = shutil.disk_usage(settings.server.data_dir)
    free_mb = usage.free // (1024 * 1024)
    check(
        "Disk space",
        free_mb >= settings.server.min_free_disk_mb,
        f"{free_mb} MB free at {settings.server.data_dir} "
        f"(minimum {settings.server.min_free_disk_mb} MB)",
    )
    check(
        "Data directory",
        os.access(settings.server.data_dir, os.W_OK),
        str(settings.server.data_dir),
    )

    # --- models
    vad_ok = (settings.models_dir / "silero_vad.onnx").exists()
    check(
        "Silero VAD",
        vad_ok,
        "present" if vad_ok else "missing → droid-assistant models download",
    )
    if settings.diarization.enabled:
        seg = settings.models_dir / settings.diarization.segmentation_model / "model.onnx"
        emb = settings.models_dir / f"{settings.diarization.embedding_model}.onnx"
        check(
            "Diarization models",
            seg.exists() and emb.exists(),
            "present"
            if seg.exists() and emb.exists()
            else "missing → droid-assistant models download",
        )

    # --- python deps
    for module, extra, needed in (
        ("faster_whisper", "local", settings.asr.backend == "faster_whisper"),
        ("onnxruntime", "local", settings.vad.backend == "silero"),
        (
            "sherpa_onnx",
            "local",
            settings.diarization.enabled and settings.diarization.backend == "sherpa",
        ),
        ("openai", "cloud", settings.translation.backend == "llm"),
        ("websockets", "cloud", settings.asr.backend in {"deepgram", "openai"}),
    ):
        if not needed:
            continue
        present = _importable(module)
        check(
            f"Python package: {module}",
            present,
            "installed" if present else f"missing → uv sync --extra {extra}",
        )

    # --- GPU
    gpu = _cuda_devices()
    check(
        "GPU", True, f"{gpu} CUDA device(s)" if gpu else "none — running on CPU, which is supported"
    )

    # --- credentials
    if (
        settings.translation.enabled
        and settings.translation.backend == "llm"
        and not settings.llm.is_local
    ):
        present = bool(settings.secret(settings.llm.api_key_env))
        check(
            "LLM credential",
            present,
            "set"
            if present
            else f"${settings.llm.api_key_env} is unset → translation and plugins "
            "will be unavailable",
        )
    cloud_key_env = {
        "deepgram": settings.asr.deepgram.api_key_env,
        "openai": settings.asr.openai.api_key_env,
    }.get(settings.asr.backend)
    if cloud_key_env is not None:
        present = bool(settings.secret(cloud_key_env))
        check(
            "Cloud ASR credential",
            present,
            "set" if present else f"${cloud_key_env} is unset",
        )

    # --- ffmpeg
    ffmpeg = shutil.which("ffmpeg") is not None
    check(
        "ffmpeg",
        ffmpeg,
        "present" if ffmpeg else "missing → session audio is stored as WAV instead of Opus",
    )

    # --- HTTPS (FR-CAP-1 gate: no secure context, no microphone)
    target = url or _tailscale_url()
    if target:
        ok, detail = _probe_https(target)
        check("HTTPS reachability", ok, detail)
    else:
        check(
            "HTTPS",
            False,
            "no public URL found. Browsers refuse microphone access outside a secure context, "
            "so every device except this one needs HTTPS. Set it up with:  tailscale serve --bg "
            f"{settings.server.port}",
        )

    table = Table(show_header=True, header_style="bold")
    table.add_column("", width=2)
    table.add_column("Check")
    table.add_column("Detail")
    failures = 0
    for name, ok, detail in results:
        if ok:
            table.add_row("[green]✓[/green]", name, detail)
        else:
            failures += 1
            table.add_row("[red]✗[/red]", name, f"[yellow]{detail}[/yellow]")
    console.print(table)

    if failures:
        err.print(f"\n[yellow]{failures} check(s) need attention.[/yellow]")
        raise typer.Exit(1)
    console.print("\n[green]Ready to record.[/green]")


def _importable(name: str) -> bool:
    import importlib.util

    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def _cuda_devices() -> int:
    try:
        import ctranslate2

        return int(ctranslate2.get_cuda_device_count())
    except Exception:
        return 0


def _tailscale_url() -> str | None:
    binary = shutil.which("tailscale")
    if not binary:
        return None
    try:
        result = subprocess.run(
            [binary, "status", "--json"], capture_output=True, text=True, timeout=5, check=False
        )
        if result.returncode != 0:
            return None
        import json

        status = json.loads(result.stdout)
        name = status.get("Self", {}).get("DNSName", "").rstrip(".")
        return f"https://{name}" if name else None
    except Exception:
        return None


def _probe_https(url: str) -> tuple[bool, str]:
    import httpx

    endpoint = url.rstrip("/") + "/api/health"
    try:
        response = httpx.get(endpoint, timeout=5.0, follow_redirects=True)
    except Exception as exc:
        return False, f"{endpoint} is not reachable ({type(exc).__name__}). Is the server running?"
    if not endpoint.startswith("https://"):
        return False, f"{endpoint} responded, but over plain HTTP — getUserMedia will refuse"
    return response.status_code == 200, f"{endpoint} → {response.status_code}"


# ---------------------------------------------------------------------------
# eval
# ---------------------------------------------------------------------------


@app.command("eval")
def run_eval(
    config: ConfigOption = None,
    corpus: Annotated[
        Path | None, typer.Option(help="Corpus directory. Defaults to eval/corpus.")
    ] = None,
    backends: Annotated[
        str | None, typer.Option(help="Comma-separated backend names to compare.")
    ] = None,
    language: Annotated[str | None, typer.Option(help="Restrict to one language.")] = None,
    json_out: Annotated[Path | None, typer.Option("--json", help="Write results as JSON.")] = None,
) -> None:
    """Measure WER and DER against the labelled corpus (NFR-EVAL-1 … 3)."""
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
    try:
        from eval.run import main as eval_main
    except ImportError as exc:
        err.print(
            f"[red]the eval harness is not importable ({exc}).[/red]\n"
            "It lives in eval/ at the repository root and is not part of the installed package."
        )
        raise typer.Exit(2) from exc

    settings = _settings(config)
    configure_logging("warning")
    code = asyncio.run(
        eval_main(
            settings=settings,
            corpus_dir=corpus,
            backends=[b.strip() for b in backends.split(",")] if backends else None,
            language=language,
            json_out=json_out,
        )
    )
    raise typer.Exit(code)


# ---------------------------------------------------------------------------
# backup
# ---------------------------------------------------------------------------


@app.command()
def backup(
    destination: Annotated[Path, typer.Argument(help="Directory to copy the data into.")],
    config: ConfigOption = None,
) -> None:
    """Copy `$DROID_DATA` minus models — the whole backup procedure (NFR-REL-7).

    Checkpoints the WAL first so the copy is a consistent snapshot even with the
    server running.
    """
    settings = _settings(config)
    source = settings.server.data_dir
    if not source.exists():
        err.print(f"[red]{source} does not exist.[/red]")
        raise typer.Exit(1)

    async def checkpoint() -> None:
        from .store import Database

        db = Database(settings.db_path)
        await db.connect()
        await db.checkpoint()
        await db.close()

    if settings.db_path.exists():
        asyncio.run(checkpoint())

    destination.mkdir(parents=True, exist_ok=True)
    target = destination / source.name
    shutil.copytree(
        source,
        target,
        dirs_exist_ok=True,
        ignore=shutil.ignore_patterns("models", "*.part", "*.pcm"),
    )
    total = sum(f.stat().st_size for f in target.rglob("*") if f.is_file())
    console.print(f"[green]Backed up to {target}[/green] ({_human(total)})")
    console.print(
        "[dim]Restore by copying this directory back over $DROID_DATA with the server "
        "stopped. Models are excluded — they re-download.[/dim]"
    )


def main() -> Any:
    return app()


if __name__ == "__main__":
    main()
