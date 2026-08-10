"""Standard stdio MCP client bridge for the controlled harness."""

from __future__ import annotations

from contextlib import AsyncExitStack
from typing import Any

from harn_agent.types import AgentTool, AgentToolResult
from harn_ai.types import ImageContent as HarnImageContent
from harn_ai.types import TextContent as HarnTextContent
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


class McpToolError(RuntimeError):
    """Raised when an MCP tool returns an error result."""


PARALLEL_MCP_TOOLS = frozenset(
    {
        # Schema-v1 and schema-v2 public names for the same independent,
        # read-only paid-research operation.  Other MCP tools remain sequential
        # unless their concurrency and accounting semantics are reviewed
        # separately.
        "research_current_web",
        "research_web",
    }
)


class StdioMcpBridge:
    """Own one initialized MCP subprocess and expose its tools to Harn."""

    def __init__(self, parameters: StdioServerParameters) -> None:
        self.parameters = parameters
        self._stack: AsyncExitStack | None = None
        self._session: ClientSession | None = None

    async def __aenter__(self) -> StdioMcpBridge:
        stack = AsyncExitStack()
        streams = await stack.enter_async_context(stdio_client(self.parameters))
        session = await stack.enter_async_context(ClientSession(*streams))
        await session.initialize()
        self._stack = stack
        self._session = session
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        if self._stack is not None:
            await self._stack.aclose()
        self._stack = None
        self._session = None

    def _require_session(self) -> ClientSession:
        if self._session is None:
            raise RuntimeError("MCP bridge is not connected")
        return self._session

    async def agent_tools(self) -> list[AgentTool]:
        result = await self._require_session().list_tools()
        return [self._to_agent_tool(tool) for tool in result.tools]

    async def read_text_resource(self, uri: str) -> str:
        result = await self._require_session().read_resource(uri)
        text: list[str] = []
        for content in result.contents:
            if content.mimeType and content.mimeType not in {"text/plain", "text/markdown", "application/json"}:
                raise McpToolError(f"MCP resource {uri} returned unsupported media type {content.mimeType!r}")
            if not hasattr(content, "text"):
                raise McpToolError(f"MCP resource {uri} did not return text")
            text.append(content.text)
        return "\n".join(text)

    def _to_agent_tool(self, tool: Any) -> AgentTool:
        async def execute(
            _tool_call_id: str,
            arguments: Any,
            _signal: Any = None,
            _on_update: Any = None,
        ) -> AgentToolResult:
            result = await self._require_session().call_tool(tool.name, dict(arguments or {}))
            converted_content: list[HarnTextContent | HarnImageContent] = []
            error_text: list[str] = []
            for content in result.content:
                if content.type == "text":
                    converted_content.append(HarnTextContent(text=content.text))
                    error_text.append(content.text)
                elif content.type == "image":
                    converted_content.append(HarnImageContent(data=content.data, mimeType=content.mimeType))
                else:
                    raise McpToolError(f"MCP tool {tool.name} returned unsupported content type {content.type!r}")
            if result.isError:
                raise McpToolError("\n".join(error_text) or f"MCP tool {tool.name} failed")
            return AgentToolResult(
                content=converted_content,
                details=result.structuredContent,
            )

        return AgentTool(
            name=tool.name,
            label=tool.title or tool.name,
            description=tool.description or "",
            parameters=tool.inputSchema,
            execute=execute,
            executionMode="parallel" if tool.name in PARALLEL_MCP_TOOLS else "sequential",
        )
