"""`roomba-kit mcp`: serve the kit's MCP tools over stdio for local agent clients (T065)."""

import asyncio
import logging
import sys

import click

from roomba_kit.cli.client import CliState
from roomba_kit.mcp.stdio import serve_stdio


@click.command("mcp")
@click.pass_obj
def mcp(state: CliState) -> None:
    """Run an MCP server on stdin/stdout for local agent clients.

    Every tool is proxied to the running service with the CLI's token (`roomba-kit token
    create cli`), so the service must be running. While it isn't, every tool call fails with
    `service_unavailable`. Point your MCP client's stdio command at `roomba-kit mcp`.
    """
    # stdout carries the MCP protocol: anything else must go to stderr.
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING)
    asyncio.run(serve_stdio(state.url()))


def register(root: click.Group) -> None:
    root.add_command(mcp)
