"""MCP server for the Guild Wars 2 API with historical account tracking."""

__version__ = "0.1.0"


def main() -> None:
    from gw2mcp.cli import main as cli_main

    cli_main()
