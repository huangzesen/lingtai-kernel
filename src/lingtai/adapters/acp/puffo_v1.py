"""The fixed Puffo Core MCP ingress for the ``puffo-v1`` ACP profile."""
from __future__ import annotations

from typing import Any

from lingtai.adapters.acp.server import AcpStdioServer, INVALID_PARAMS, _RpcError
from lingtai.services.session_mcp import StdioMCPServerConfig


PUFFO_MCP_NAME = "puffo"
PUFFO_MCP_ARGS = ("-m", "puffo_agent.mcp.puffo_core_server")
_REQUIRED_ENV_NAME = "PUFFO_LOCAL_SERVICE_TOKEN"


def validate_puffo_v1_mcp_servers(value: Any) -> tuple[StdioMCPServerConfig, ...]:
    """Accept precisely the one stdio service projected by the Puffo driver.

    The command remains deployment-specific because it is the local Python
    interpreter chosen by Puffo's installation.  Its generic ACP validation
    still requires a non-empty absolute executable path; the fixed name and
    module arguments prevent this profile from becoming arbitrary MCP ingress.
    Environment values remain opaque credentials/configuration owned by Puffo.
    They are not service identity: the executable and Python environment can
    change the code that runs.  Keep ordinary unique string name/value validation
    from ACP, require the one runtime credential, and preserve all other names
    and values without logging them.  The trusted driver launch boundary plus
    the one service name and exact module arguments identify this integration.
    """

    configs = AcpStdioServer._stdio_mcp_configs(value)
    if len(configs) != 1:
        raise _RpcError(INVALID_PARAMS, "puffo-v1 requires exactly one Puffo MCP server")
    config = configs[0]
    if config.name != PUFFO_MCP_NAME:
        raise _RpcError(INVALID_PARAMS, "puffo-v1 MCP server must be named puffo")
    if config.args != PUFFO_MCP_ARGS:
        raise _RpcError(
            INVALID_PARAMS,
            "puffo-v1 MCP server must run puffo_agent.mcp.puffo_core_server",
        )
    token_value = next(
        (value for name, value in config.env if name == _REQUIRED_ENV_NAME),
        None,
    )
    if not token_value:
        raise _RpcError(
            INVALID_PARAMS,
            "puffo-v1 MCP server requires a non-empty Puffo local service token",
        )
    return configs


__all__ = ["PUFFO_MCP_ARGS", "PUFFO_MCP_NAME", "validate_puffo_v1_mcp_servers"]
