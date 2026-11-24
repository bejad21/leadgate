"""Tool argument models: the one definition of what each tool accepts.

Each tool's arguments are a Pydantic model. The same model produces the JSON schema the LLM
is shown and validates what the LLM sends back, so the two cannot drift apart. Validation
sanitises rather than trusts: strings are cleaned and capped, numbers range-checked, unknown
keys ignored, and None or an empty string counts as not given.

Field descriptions may contain "{noun}", filled in with what the adapter's catalog calls
an item when the schema is built.
"""
import math
from dataclasses import dataclass
from typing import Annotated, Any, Literal, get_args

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, ValidationError
from pydantic_core import PydanticCustomError

from engine.core.guardrails import MAX_STR, NOTES_MAX, NUM_MAX, _clean

ViewingSlot = Literal["morning", "afternoon", "evening"]
VIEWING_SLOTS: tuple[str, ...] = get_args(ViewingSlot)


def _missing() -> PydanticCustomError:
    return PydanticCustomError("missing", "Field required")


def _text(limit: int, *, required: bool):
    def clean(value):
        text = None
        if value is not None:
            if not isinstance(value, str):
                raise ValueError("must be text")
            text = _clean(value)[:limit] or None
        if text is None and required:
            raise _missing()
        return text
    return BeforeValidator(clean)


def _as_number(value) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError("must be a number")
    try:
        number = float(value)
    except ValueError:
        raise ValueError("must be a number") from None
    except OverflowError:
        # An int too large for a float (JSON allows a 400-digit literal). Not a ValueError, so
        # without this it would escape validation and fail the whole turn.
        raise ValueError("number out of range") from None
    if not math.isfinite(number) or number < 0 or number > NUM_MAX:
        raise ValueError("number out of range")
    return number


def _number(*, whole: bool, required: bool):
    def check(value):
        if value is None:
            if required:
                raise _missing()
            return None
        number = _as_number(value)
        if not whole:
            return number
        # A fraction for an id or a year is a mistake to report, not a value to round.
        if not number.is_integer():
            raise ValueError("must be a whole number")
        return int(number)
    return BeforeValidator(check)


def optional_choice(options):
    """For an optional enum: a value outside `options` is dropped, as if it was not given."""
    def pick(value):
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError("must be text")
        text = _clean(value)[:MAX_STR]
        return text if text in options else None
    return BeforeValidator(pick)


Text = Annotated[str, _text(MAX_STR, required=True)]
OptionalText = Annotated[str | None, _text(MAX_STR, required=False)]
Notes = Annotated[str | None, _text(NOTES_MAX, required=False)]
OptionalNumber = Annotated[float | None, _number(whole=False, required=False)]
WholeNumber = Annotated[int, _number(whole=True, required=True)]
OptionalWholeNumber = Annotated[int | None, _number(whole=True, required=False)]


class ToolArgs(BaseModel):
    """Base for every tool's arguments. Keys the model does not declare are ignored."""

    model_config = ConfigDict(extra="ignore")


CONTACT_DESCRIPTION = "Customer's email or phone number"
ACTION_NOTES_DESCRIPTION = "Anything the customer said that a person should know"


class CreateLeadArgs(ToolArgs):
    name: Text = Field(description="Title for the lead, e.g. the {noun}'s name")
    customer_name: Text
    customer_contact: OptionalText = Field(None, description=CONTACT_DESCRIPTION)
    price: OptionalNumber = Field(None, description="Price of the {noun}, used as expected revenue")
    notes: Notes = Field(None, description="Additional context about the customer's interest")


class ReserveItemArgs(ToolArgs):
    item_id: WholeNumber = Field(description="The {noun}'s id from the search results")
    customer_name: Text
    customer_contact: OptionalText = Field(None, description=CONTACT_DESCRIPTION)
    notes: Notes = Field(None, description=ACTION_NOTES_DESCRIPTION)


class BookViewingArgs(ToolArgs):
    item_id: WholeNumber = Field(description="The {noun}'s id from the search results")
    customer_name: Text
    customer_contact: OptionalText = Field(None, description=CONTACT_DESCRIPTION)
    date: Text = Field(description="The day, as YYYY-MM-DD, today or later")
    slot: Annotated[ViewingSlot, _text(MAX_STR, required=True)]
    notes: Notes = Field(None, description=ACTION_NOTES_DESCRIPTION)


def _describe(exc: ValidationError) -> str:
    """Pydantic's errors as one short line per field. The input is never echoed: it can be
    long, and it can be an attack."""
    parts = []
    for error in exc.errors(include_url=False, include_input=False, include_context=False):
        field = str(error["loc"][0]) if error["loc"] else "arguments"
        if error["type"] == "missing":
            parts.append(f"missing required argument: {field}")
        else:
            parts.append(f"{field}: {error['msg'].removeprefix('Value error, ')}")
    return "; ".join(parts)


def parse_tool_args(model: type[ToolArgs], args: Any) -> dict:
    """Return the cleaned arguments as a dict, or raise ValueError saying what was wrong."""
    if not isinstance(args, dict):
        raise ValueError("arguments must be an object")
    try:
        parsed = model.model_validate(args)
    except ValidationError as exc:
        raise ValueError(_describe(exc)) from None
    return parsed.model_dump(exclude_none=True)


def _inline(node: Any, defs: dict) -> Any:
    """Replace each `$ref` with the definition it points to."""
    if isinstance(node, list):
        return [_inline(item, defs) for item in node]
    if not isinstance(node, dict):
        return node
    if "$ref" in node:
        target = defs[node["$ref"].rsplit("/", 1)[-1]]
        return _inline({**target, **{k: v for k, v in node.items() if k != "$ref"}}, defs)
    return {key: _inline(value, defs) for key, value in node.items()}


def _field_schema(spec: dict, noun: str) -> dict:
    """One field's schema as the LLM should see it: an optional field's `anyOf [X, null]` becomes X,
    and Pydantic's title and null default are dropped."""
    variants = [v for v in spec.get("anyOf", []) if v.get("type") != "null"]
    base = variants[0] if len(variants) == 1 else {}
    merged = {**base, **{k: v for k, v in spec.items() if k != "anyOf"}}
    clean = {k: v for k, v in merged.items() if k not in ("title", "default")}
    if "description" in clean:
        clean["description"] = clean["description"].replace("{noun}", noun)
    return clean


def function_schema(name: str, description: str, model: type[ToolArgs], noun: str = "") -> dict:
    """The OpenAI-style function-calling schema for a tool whose arguments are `model`."""
    raw = model.model_json_schema()
    raw = _inline({k: v for k, v in raw.items() if k != "$defs"}, raw.get("$defs", {}))
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": {key: _field_schema(spec, noun) for key, spec in raw.get("properties", {}).items()},
                "required": list(raw.get("required", [])),
            },
        },
    }


@dataclass(frozen=True)
class Tool:
    """A tool an adapter offers: its name, what the LLM is told it does, and its arguments."""

    name: str
    description: str
    args: type[ToolArgs]
    noun: str = ""

    def schema(self) -> dict:
        return function_schema(self.name, self.description, self.args, self.noun)
