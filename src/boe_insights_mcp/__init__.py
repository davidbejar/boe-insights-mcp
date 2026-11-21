"""boe-insights-mcp: MCP server with analytical tools over Spain's BOE open data.

Provides law version history, amendment analysis and legislative trends for the
Boletín Oficial del Estado (BOE). Informational only; not an official source.
"""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__: str = version("boe-insights-mcp")
except PackageNotFoundError:  # pragma: no cover - package not installed
    __version__ = "0.0.0+unknown"

__all__ = ["__version__"]
