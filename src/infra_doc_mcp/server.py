"""MCP server entry-point."""

from __future__ import annotations

import os
import sys
from typing import Literal, cast

from mcp.server.transport_security import TransportSecuritySettings

from infra_doc_mcp.tools import mcp


def main() -> None:
    # stdio for local editor use; "streamable-http" for a hosted instance.
    transport = cast(
        Literal["stdio", "sse", "streamable-http"],
        os.environ.get("INFRA_DOC_MCP_TRANSPORT", "stdio"),
    )
    if transport != "stdio":
        mcp.settings.host = "0.0.0.0"
        port = os.environ.get("PORT")
        if port:
            mcp.settings.port = int(port)
        # DNS rebinding protection guards servers bound to a user's localhost
        # against being reached by a browser. A public endpoint serving
        # read-only public docs is not that; leaving it on rejects every
        # request whose Host is not in an allowlist we would have to keep in
        # sync with the deployment hostname.
        mcp.settings.transport_security = TransportSecuritySettings(
            enable_dns_rebinding_protection=False
        )
    try:
        mcp.run(transport=transport)
    except Exception as error:
        print(str(error), file=sys.stderr)
        raise


if __name__ == "__main__":
    main()
