# Writing a plugin

> **Plugins run in-process with full server privileges.** Installing one is
> equivalent to running arbitrary code on the server. Read the code before you
> drop it in. This is a documented trust boundary, not an oversight — process
> isolation is a v3 consideration (NFR-SEC-6, R11).

## The shortest possible plugin

Drop this in `plugins/wordcount.py` and restart the server.

```python
from droid_assistant.plugins import Artifact, Context, Event, Plugin


class WordCountPlugin(Plugin):
    name = "wordcount"
    version = "1.0.0"
    api_version = 1
    subscribes = {Event.SESSION_END}

    async def on_session_end(self, ctx: Context) -> Artifact | None:
        utterances = await ctx.utterances()
        words = sum(len(u.text.split()) for u in utterances)
        return Artifact(kind="wordcount", content=f"{words} words in {len(utterances)} utterances")
```

It appears in Settings → Plugins, produces an artifact on every session, and
shows up as a tab in the session view.

## The contract

| Member | What it is |
|---|---|
| `name` | Unique. Two plugins with the same name: the second is refused |
| `version` | Yours, recorded on every artifact so you can tell which run produced what |
| `api_version` | The host refuses a mismatched major version (NFR-MNT-2) |
| `config_schema` | A pydantic model. The settings form is generated from it |
| `subscribes` | The events you want. You are not called for anything else |
| `requires_llm` | Declares the dependency, so the host can report you as unavailable rather than failing you |

### Events

`SESSION_START`, `SESSION_END`, `UTTERANCE_FINAL`, `TRANSLATION_FINAL`,
`SPEAKER_CHANGED`, `TRANSCRIPT_EDITED`.

`utterance.partial` is deliberately not offered. Acting on text that may still
be rewritten is a bug generator.

Each event has a matching handler: `on_session_end`, `on_utterance_final`, and
so on. Return an `Artifact` to emit one, or `None` for nothing.

### The context

```python
async def on_session_end(self, ctx: Context) -> Artifact | None:
    text = await ctx.store.transcript(ctx.session_id, include_translation=True)
    speakers = await ctx.store.speakers(ctx.session_id)
    meta = await ctx.store.session_metadata(ctx.session_id)

    summary = await ctx.llm.complete("Summarise:\n" + text, system="You summarise meetings.")

    await ctx.emit_artifact(Artifact(kind="extra", content="emitted mid-handler"))
    ctx.logger.info("done", extra={"speakers": len(speakers)})
    return Artifact(kind="summary", content=summary)
```

- `ctx.utterances()` — the lines this run covers. **Prefer it to
  `ctx.store.utterances()`**: see *Running over part of a session* below.
- `ctx.store` — read-only. A plugin cannot mutate the transcript.
- `ctx.llm` — **pre-configured**. You never see a credential, never construct a
  client, and the global budget and local-only switch are already applied
  (FR-PLG-8). Check `ctx.llm.available` if your plugin can degrade.
- `ctx.config` — a validated instance of your own `config_schema`.
- `ctx.emit_artifact(...)` — emit at any point, not only from a return.
- `ctx.previous(kind)` — your own current artifact of that kind, or `None`.
  What a scoped run adds to.
- `ctx.utterance_ids` — the scope, or `None` for the whole session.
- `ctx.logger` — namespaced; use `extra=` for structured fields.

### Running over part of a session

The session view has a control on each line — "make an action item out of
this". It runs one plugin over one utterance, through the same handler:

```
POST /api/sessions/{id}/plugins/{name}/run
{"utterance_ids": ["utt_abc"]}
```

The event is still `session.end`. What changes is how much of the session the
handler can see, and the only thing a plugin has to do to honour it is read
`ctx.utterances()` rather than `ctx.store.utterances()`. A plugin that reaches
past it answers a question about one line with the whole conversation, which is
a bug the host cannot catch for you.

Three things are worth deciding deliberately if your plugin supports being
scoped. All three were learned by getting them wrong first:

- **Ask a different question.** Instructions written for an unattended pass over
  a whole transcript are written against fabrication — "extract only what was
  actually committed to". Applied to a line somebody deliberately picked, they
  answer the wrong question: they weigh whether it qualifies, decide it is only
  a remark, and return nothing, so the button appears to do nothing. The person
  clicking has already decided. `action_items` keeps a second system prompt for
  this, and it is the difference between the feature working and not.
- **Add rather than replace.** Somebody who picks a second line means "and this
  one too". `action_items` reads its previous JSON artifact through
  `ctx.previous`, merges, and treats two items as the same when one's words
  contain the other's — the whole-session pass writes "Finish the migration" and
  a click on the line it came from writes "Finish the migration by Friday", and
  a list with both is worse than either. A whole-session run still replaces,
  because that is what a re-run after an edit has to mean.
- **Say what actually happened.** Report enough in `metadata` for the UI to tell
  "added it", "it was already there", and "found nothing" apart. Those are three
  outcomes a reader acts on differently, and a count alone distinguishes none of
  them. `action_items` returns `added` and `linked` beside `count`, and records
  a `source` on each item so the transcript can mark the lines already covered.

## Running only when asked

Set `on_demand = True` and the host never dispatches the plugin; it produces
something only when somebody runs it. `subscribes` still declares which handler
that run reaches.

```python
class SummaryPlugin(Plugin):
    subscribes = {Event.SESSION_END}   # the handler an on-demand run calls
    on_demand = True                   # …but never dispatched
```

Worth it for anything whose cost is a call over the whole transcript. Most
recordings are never opened twice, so doing that automatically bills for output
nobody asked for and — on a cloud credential — sends every conversation to a
provider as a matter of course. The session view gives such a plugin a tab with
a **Generate** button, and says why it is empty rather than implying a failure.

## Configuration

Declare a pydantic model and the UI renders the form:

```python
from pydantic import BaseModel, Field


class Config(BaseModel):
    style: str = Field(default="bullets", json_schema_extra={"enum": ["bullets", "prose"]})
    max_words: int = Field(default=400, ge=50, le=2000, description="Length ceiling")
    include_quotes: bool = Field(default=True, description="Show the source line")


class MyPlugin(Plugin):
    config_schema = Config
```

Three fields, three correctly-typed inputs with validation, no UI code. Enums
become a select, booleans a toggle, numbers a bounded number input, and
`description` becomes the help text.

## What the host guarantees

- **Off the live path.** Your handler cannot delay a single utterance, however
  long it takes (FR-PLG-9).
- **A timeout.** Configurable, 180 s by default. Past it you are cancelled and
  reported (FR-PLG-4).
- **Failure isolation.** An exception disables you *for that session only*, is
  reported on the event stream, and shows in Settings. Other plugins and the
  pipeline are unaffected (FR-PLG-3).
- **Artifact versioning.** Re-running does not destroy prior output; both
  versions stay retrievable and the current one is indicated (FR-PLG-12).

What it does *not* guarantee is containment. See the warning at the top.

## Installing

Two routes, both discovered at startup (FR-PLG-1):

**A file** in the `plugins/` directory. Files beginning with `_` are ignored.

**A package** declaring an entry point:

```toml
[project.entry-points."droid_assistant.plugins"]
my_plugin = "my_package:MyPlugin"
```

## Re-running after an edit

Correcting a transcript makes every artifact derived from it stale. The session
view detects this and offers a re-run; the API is:

```
POST /api/sessions/{id}/plugins/{name}/run
```

which produces a new version rather than overwriting the old one. The same
endpoint is what the history list's *Summarise* button posts, and what a
session that never ran a plugin uses to run one for the first time — the tab
for an idle plugin offers *Generate* instead of an empty panel.

## Testing

Plugins are ordinary async classes. Test them directly:

```python
async def test_wordcount():
    ctx = Context(
        session_id="sess_1",
        store=FakeStore(),
        llm=FakeLLM(),
        config=WordCountPlugin.config_schema(),
        logger=logging.getLogger("test"),
        event=Event.SESSION_END,
        payload={},
    )
    artifact = await WordCountPlugin().on_session_end(ctx)
    assert "words" in artifact.content
```

`tests/test_plugins.py` has working fakes for `store` and `llm` to copy.

## Reference implementations

`src/droid_assistant/plugins/builtin/` holds two, written the way a third-party
plugin should be:

- **`summary.py`** — config as a model, one handler, an artifact returned, and a
  prompt built around the failure mode that matters (inventing decisions).
- **`action_items.py`** — emits two artifacts from one run, Markdown for reading
  and JSON for anything downstream, with tolerant parsing that refuses to guess.
