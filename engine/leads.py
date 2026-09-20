"""The facts about a lead that the alert and the dashboard mirror both need."""
from dataclasses import dataclass

from engine.core.agent_loop import AgentTurnResult
from engine.core.crm_contacts import parse_contact

LEAD_TOOL = "create_lead"
# Every tool whose success opens a lead, and which kind of lead that is.
LEAD_TOOLS = {"create_lead": "lead", "reserve_item": "reservation", "book_viewing": "viewing"}


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
    kind: str = "lead"  # lead, reservation or viewing
    detail: str | None = None  # "Held for 24 hours", "Sat 26 Sep, afternoon"
    username: str | None = None  # the customer's public Telegram username, if they have one


def extract_leads(turn: AgentTurnResult, domain_type: str) -> list[LeadInfo]:
    """Leads that were actually created in this turn.

    A tool call only counts if it opens a lead and its result carries a lead id; a
    rejected call (rate limit, bad arguments, a refused hold) returns an error result
    and is skipped.
    """
    leads: list[LeadInfo] = []
    for call, result in zip(turn.tool_calls_made, turn.tool_results):
        kind = LEAD_TOOLS.get(call.name)
        lead_id = result.get("lead_id") if isinstance(result, dict) else None
        if kind is None or not isinstance(lead_id, int) or isinstance(lead_id, bool):
            continue
        if result.get("duplicate") is True:
            continue  # the customer's earlier lead came back; it was alerted and mirrored then
        args = call.arguments or {}
        email, phone = parse_contact(args.get("customer_contact"))
        leads.append(
            LeadInfo(
                lead_id=lead_id,
                domain_type=domain_type,
                # A reservation or viewing names its item in the result, from the catalog.
                item_name=str(result.get("item_name") or args.get("name") or "Unnamed item"),
                customer_name=str(args.get("customer_name") or ""),
                email=email,
                phone=phone,
                price=result.get("price", args.get("price")),
                price_verified=result.get("price_verified", True) is not False,
                kind=kind,
                detail=result.get("detail"),
            )
        )
    return leads
