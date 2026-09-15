"""Local browser presentation layer over the repository-owned agent loop.

The package adds no agent behaviour. It exposes the same bounded run that
``proofcoder run`` performs, streams the already sanitized event objects to one
local browser page, and reads existing workspace traces. Every decision about
tools, safety, verification, and termination stays in the core modules.
"""

from proofcoder.web.runs import (
    BrowserRun,
    BrowserRunError,
    BrowserRunManager,
    BrowserRunStatus,
)
from proofcoder.web.server import (
    DEFAULT_HOST,
    DEFAULT_PORT,
    ServerAddressError,
    WebServer,
    create_server,
)

__all__ = [
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "BrowserRun",
    "BrowserRunError",
    "BrowserRunManager",
    "BrowserRunStatus",
    "ServerAddressError",
    "WebServer",
    "create_server",
]
