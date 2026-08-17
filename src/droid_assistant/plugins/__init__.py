"""The plugin system (SRS §5.5).

`api` is the public contract a plugin author imports; `host` is the machinery
that discovers, supervises, and isolates them.
"""

from .api import Artifact, Context, Event, Plugin, Prompt, format_transcript
from .host import LoadedPlugin, PluginHost, discover

__all__ = [
    "Artifact",
    "Context",
    "Event",
    "LoadedPlugin",
    "Plugin",
    "PluginHost",
    "Prompt",
    "discover",
    "format_transcript",
]
