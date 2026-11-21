"""boe-insights-mcp: MCP server with analytical tools over Spain's BOE open data.

Provides law version history, amendment analysis and legislative trends for the
Boletín Oficial del Estado (BOE). Informational only; not an official source.

This file only exposes ``__version__``, read from the installed package metadata (it is also
what the client puts in its User-Agent). The code lives in the sibling modules: tools in
``server``, use cases in ``legislation``, HTTP access in ``client``.
"""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__: str = version("boe-insights-mcp")
except PackageNotFoundError:  # pragma: no cover - package not installed
    __version__ = "0.0.0+unknown"

__all__ = ["__version__"]
