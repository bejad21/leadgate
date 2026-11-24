"""Switch guardrails off, all together (the red-team negative control) or one at a time (the ablation).

Kept apart from run_redteam.py so a test can import it without that script's start-up work. Every switch
goes through `_Patcher`, so `disable_guardrails` returns a function that puts everything back.
"""
from collections.abc import Callable, Iterable

from engine.rate_limiter import FixedWindowRateLimiter

# The ten guardrails, each switched off on its own for the ablation.
GUARDRAILS = (
    "injection_screen",  # the regex screen on customer text, before the model
    "argument_validation",  # Pydantic checks on the model's tool arguments
    "tool_call_cap",  # at most 3 tool calls per turn
    "write_limits",  # one action per turn and the per-chat write limit
    "hold_guard",  # holds and viewings only for items already shown in the chat
    "reply_filter",  # links and prompt text removed from replies
    "price_verification",  # lead prices checked against the catalog
    "prompt_rules",  # the Boundaries section of the system prompt
    "tool_result_sanitizing",  # catalog text defused before the model reads it
    "contact_provenance",  # contact details must come from the customer's own words
)

_UNSET = object()


def naive_lead(odoo, domain_type, args, **_options):
    values = {"name": args["name"], "description": args.get("notes", "")}
    if args.get("price"):
        values["expected_revenue"] = args["price"]
    return {"lead_id": odoo.create("crm.lead", values)}


class _Patcher:
    def __init__(self) -> None:
        self._saved: list[tuple[object, str, object]] = []

    def set(self, module: object, name: str, value: object) -> None:
        self._saved.append((module, name, getattr(module, name, _UNSET)))
        setattr(module, name, value)

    def restore(self) -> None:
        for module, name, value in reversed(self._saved):
            if value is _UNSET:
                delattr(module, name)
            else:
                setattr(module, name, value)
        self._saved.clear()


def _raw_tool(call, models, adapter, position, chat_id, write_limiter, seen_writes, shown_ids=None):
    # Same contract as the real _run_guarded_tool: (result, outcome).
    try:
        return adapter.execute_tool(call.name, call.arguments), "ok"
    except Exception as exc:
        return {"error": str(exc)}, "execution_error"


def disable_guardrails(main_module, only: Iterable[str] | None = None) -> Callable[[], None]:
    """Switch off the named guardrails (all of them when `only` is None) and return a function that undoes it.

    With every guardrail named, the tool runner is replaced by a bare one, as the negative control always did.
    With a subset, the real tool runner stays and only the named checks are removed.
    """
    import engine.adapters.cars as cars_module
    import engine.adapters.real_estate as real_estate_module
    import engine.core.actions as actions_module
    import engine.core.agent_loop as loop

    names = set(GUARDRAILS if only is None else only)
    unknown = names - set(GUARDRAILS)
    if unknown:
        raise ValueError(f"unknown guardrail(s): {sorted(unknown)}; choose from {list(GUARDRAILS)}")
    patch = _Patcher()

    if "injection_screen" in names:
        patch.set(main_module, "is_injection_attempt", lambda text: False)
    if "write_limits" in names:
        patch.set(main_module, "_write_limiter", FixedWindowRateLimiter(10**6, 60))
        patch.set(loop, "WRITES_PER_TURN", 10**6)
    if "argument_validation" in names:
        patch.set(loop, "parse_tool_args", lambda model, args: args)
    if "tool_call_cap" in names:
        patch.set(loop, "TOOL_CALL_CAP", 10**6)
    if "reply_filter" in names:
        patch.set(loop, "filter_reply", lambda reply, prompt: reply)
        patch.set(main_module, "filter_reply", lambda reply, prompt: reply)
    if "prompt_rules" in names:
        patch.set(loop, "SYSTEM_PROMPT", loop.SYSTEM_PROMPT.split("Boundaries:")[0])
    if "tool_result_sanitizing" in names:
        patch.set(loop, "sanitize_tool_result", lambda value: value)
    if "contact_provenance" in names:
        patch.set(loop, "_with_customer_contact", lambda call, *args, **kwargs: call)
    if "price_verification" in names:
        for module in (cars_module, real_estate_module, actions_module):
            patch.set(module, "create_verified_lead", naive_lead)
    if "hold_guard" in names:
        guarded = loop._run_guarded_tool

        def without_shown_check(call, models, adapter, position, chat_id, write_limiter, seen_writes, shown_ids=None):
            return guarded(call, models, adapter, position, chat_id, write_limiter, seen_writes, None)

        patch.set(loop, "_run_guarded_tool", without_shown_check)
    if names == set(GUARDRAILS):
        patch.set(loop, "_run_guarded_tool", _raw_tool)
    return patch.restore
