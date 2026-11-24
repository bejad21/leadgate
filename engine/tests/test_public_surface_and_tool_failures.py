"""The engine sits behind a public tunnel, so it serves no API docs; and a tool that crashes is logged."""
import logging

import pytest
from fastapi.testclient import TestClient

from engine.core.agent_loop import run_turn
from engine.llm_client import LLMResponse, ToolCall
from engine.main import app
from engine.tests.test_agent_loop import FakeCarsAdapter, FakeLLMClient


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json"])
def test_api_docs_are_not_served(path):
    assert TestClient(app).get(path).status_code == 404


def test_health_still_answers():
    assert TestClient(app).get("/health").json() == {"status": "ok"}


class CrashingAdapter(FakeCarsAdapter):
    def execute_tool(self, name, args):
        raise RuntimeError("odoo went away")


def test_a_crashing_tool_is_logged_by_name_without_the_customers_details(caplog):
    llm = FakeLLMClient(
        [
            LLMResponse(content=None, tool_calls=[ToolCall(name="search_inventory", arguments={"make": "jane.doe@example.com"})]),
            LLMResponse(content="Sorry, that search failed.", tool_calls=[]),
        ]
    )
    history = [{"role": "user", "content": "anything from jane.doe@example.com?"}]

    with caplog.at_level(logging.ERROR):
        result = run_turn(history, CrashingAdapter({}), llm)

    assert result.reply == "Sorry, that search failed."
    assert any(record.exc_info for record in caplog.records)
    assert "search_inventory" in caplog.text
    assert "odoo went away" in caplog.text
    assert "jane.doe@example.com" not in caplog.text
    tool_message = next(m for m in llm.calls[1]["messages"] if m["role"] == "tool")
    assert tool_message["content"] == '{"error": "the tool failed"}'
