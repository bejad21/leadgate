"""A third catalog defined only here: adding a domain needs no change under engine/core."""
from types import SimpleNamespace
from typing import cast

from engine.core.actions import ACTION_TOOLS, execute_action_tool, shared_tools
from engine.core.adapter_base import DomainAdapter, create_verified_lead
from engine.core.agent_loop import run_turn
from engine.core.tools import OptionalNumber, Tool, ToolArgs
from engine.llm_client import ChatModel, LLMResponse, ToolCall
from engine.tests.fakes import FakeOdoo, item


class BoatSearchArgs(ToolArgs):
    price_max: OptionalNumber = None


class BoatsAdapter(DomainAdapter):
    domain_type = "boats"
    crm_tag = "Boats"

    def tools(self):
        return [Tool("search_boats", "Search available boats by maximum price", BoatSearchArgs), *shared_tools("boat")]

    def execute_tool(self, name, args):
        if name == "search_boats":
            rows = self.odoo.search_read("leadgate.catalog.item", [("domain_type", "=", self.domain_type)], ["name", "price"])
            return {"matches": [r for r in rows if r["price"] <= args.get("price_max", float("inf"))]}
        if name == "create_lead":
            return create_verified_lead(self.odoo, self.domain_type, args, crm_tag=self.crm_tag)
        if name in ACTION_TOOLS:
            return execute_action_tool(self.odoo, self.domain_type, name, args, crm_tag=self.crm_tag)
        raise ValueError(f"Unknown tool: {name}")


def test_a_third_domain_searches_and_creates_a_lead_with_its_own_tag():
    odoo = FakeOdoo(items={3: item(3, name="2019 Bayliner VR5", price=30000.0, domain_type="boats")})
    search = ToolCall(name="search_boats", arguments={"price_max": "40000"}, id="s")
    lead = ToolCall(name="create_lead", arguments={"name": "2019 Bayliner VR5", "customer_name": "Ana"}, id="l")
    replies = [LLMResponse(None, [search, lead]), LLMResponse("Ana, a person will call you about the Bayliner.", [])]
    llm = cast(ChatModel, SimpleNamespace(chat=lambda messages, tools: replies.pop(0)))
    result = run_turn([{"role": "user", "content": "a boat under 40k, I'm Ana"}], BoatsAdapter(odoo), llm)

    assert result.tool_results[0]["matches"][0]["name"] == "2019 Bayliner VR5"
    assert "lead_id" in result.tool_results[1]
    lead_values = next(v for m, v in odoo.created if m == "crm.lead")
    assert lead_values["name"] == "2019 Bayliner VR5"
    boats_tag_id = 500 + odoo.created.index(("crm.tag", {"name": "Boats"})) + 1  # FakeOdoo's id for that create
    assert lead_values["tag_ids"] == [(6, 0, [boats_tag_id])]
