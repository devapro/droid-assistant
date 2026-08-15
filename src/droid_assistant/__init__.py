"""droid-assistant — self-hosted conversation intelligence with a browser client.

The browser captures; the server recognises, diarizes, translates, stores, and
runs plugins. See docs/SRS.md for the specification this implements.
"""

__version__ = "1.0.0.dev0"

# The plugin host refuses to load a plugin declaring a different major version
# (FR-PLG-1, NFR-MNT-2).
PLUGIN_API_VERSION = 1

__all__ = ["PLUGIN_API_VERSION", "__version__"]
