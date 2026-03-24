"""MCP command helpers for AgentLoop."""

from __future__ import annotations

from typing import TYPE_CHECKING

from nanobot.agent.i18n import text
from nanobot.bus.events import InboundMessage, OutboundMessage

if TYPE_CHECKING:
    from nanobot.agent.loop import AgentLoop


class MCPCommandHandler:
    """Encapsulates `/mcp` subcommand behavior for AgentLoop."""

    def __init__(self, loop: AgentLoop) -> None:
        self.loop = loop

    @staticmethod
    def _response(msg: InboundMessage, content: str) -> OutboundMessage:
        return OutboundMessage(channel=msg.channel, chat_id=msg.chat_id, content=content)

    def _group_mcp_tool_names(self) -> dict[str, list[str]]:
        """Group registered MCP tool names by configured server name."""
        grouped = {name: [] for name in self.loop._mcp_servers}
        server_names = sorted(self.loop._mcp_servers, key=len, reverse=True)

        for tool_name in self.loop.tools.tool_names:
            if not tool_name.startswith("mcp_"):
                continue

            for server_name in server_names:
                prefix = f"mcp_{server_name}_"
                if tool_name.startswith(prefix):
                    grouped[server_name].append(tool_name.removeprefix(prefix))
                    break

        return {name: sorted(tools) for name, tools in grouped.items()}

    async def list(self, msg: InboundMessage, language: str) -> OutboundMessage:
        await self.loop._reload_mcp_servers_if_needed()

        if not self.loop._mcp_servers:
            return self._response(msg, text(language, "mcp_no_servers"))

        await self.loop._connect_mcp()

        server_lines = "\n".join(f"- {name}" for name in self.loop._mcp_servers)
        sections = [text(language, "mcp_servers_list", items=server_lines)]

        grouped_tools = self._group_mcp_tool_names()
        tool_lines = "\n".join(
            f"- {server}: {', '.join(tools)}"
            for server, tools in grouped_tools.items()
            if tools
        )
        sections.append(
            text(language, "mcp_tools_list", items=tool_lines)
            if tool_lines
            else text(language, "mcp_no_tools")
        )

        return self._response(msg, "\n\n".join(sections))
