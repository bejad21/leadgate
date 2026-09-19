"""The facts about a lead that the alert and the dashboard mirror both need."""
from dataclasses import dataclass

from engine.core.agent_loop import AgentTurnResult
from engine.core.crm_contacts import parse_contact

LEAD_TOOL = "create_lead"


@dataclass
class LeadInfo:
    lead_id: int
    domain_type: str
    item_name: str
    customer_name: str
    email: str | None
    phone: str | None
    price: float | None
    price_verified: bool


def extract_leads(turn: AgentTurnResult, domain_type: str) -> list[LeadInfo]:
    """Leads that were actually created in this turn.

    A tool call only counts if it was the lead tool and its result carries a
    lead id; a rejected call (rate limit, bad arguments) returns an error result
    and is skipped.
    """
    leads: list[LeadInfo] = []
    for call, result in zip(turn.tool_calls_made, turn.tool_results):
        lead_id = result.get("lead_id") if isinstance(result, dict) else None
        if call.name != LEAD_TOOL or not isinstance(lead_id, int) or isinstance(lead_id, bool):
            continue
        if result.get("duplicate") is True:
            continue  # the customer's earlier lead came back; it was alerted and mirrored then
        args = call.arguments or {}
        email, phone = parse_contact(args.get("customer_contact"))
        leads.append(
            LeadInfo(
                lead_id=lead_id,
                domain_type=domain_type,
                item_name=str(args.get("name") or "Unnamed item"),
                customer_name=str(args.get("customer_name") or ""),
                email=email,
                phone=phone,
                price=args.get("price"),
                price_verified=result.get("price_verified", True) is not False,
            )
        )
    return leads
