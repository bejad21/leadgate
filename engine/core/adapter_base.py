from abc import ABC, abstractmethod


class DomainAdapter(ABC):
    @abstractmethod
    def tool_schemas(self) -> list[dict]:
        """Return OpenRouter/OpenAI-format function-calling schemas for this domain's tools."""

    @abstractmethod
    def execute_tool(self, name: str, args: dict) -> dict:
        """Execute a named tool call and return a JSON-serializable result."""
