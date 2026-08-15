# Drop-in plugins

Any `*.py` file in this directory that defines a `Plugin` subclass is loaded at
server start (FR-PLG-1). Files beginning with `_` are ignored.

```python
from droid_assistant.plugins import Artifact, Context, Event, Plugin

class HelloPlugin(Plugin):
    name = "hello"
    version = "1.0.0"
    api_version = 1
    subscribes = {Event.SESSION_END}

    async def on_session_end(self, ctx: Context) -> Artifact | None:
        text = await ctx.store.transcript(ctx.session_id)
        return Artifact(kind="hello", content=f"{len(text.splitlines())} lines")
```

**Plugins run in-process with full server privileges.** Installing one is
equivalent to running arbitrary code on this machine (NFR-SEC-6). Read the code
before you drop it in here.

See `docs/PLUGINS.md` for the full API.
