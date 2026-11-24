"""Abuse protection for the tool-calling agent.

Domain-agnostic on purpose, like the rest of engine/core. The layers, in the
order a message meets them:

1. sanitize_user_text: strip characters and chat-template tokens that have no
   business in a customer message.
2. is_injection_attempt: cheap phrase heuristics. Easy to get around, so it is
   only a first filter; the checks below do not depend on it.
3. parse_tool_args (engine/core/tools.py): every tool argument the model produces
   is checked against that tool's own argument model before anything touches Odoo.
   The limits it applies (MAX_STR, NOTES_MAX, NUM_MAX) are defined here.
4. filter_reply: the final reply cannot carry links or leak the system prompt.

Text that comes back from a tool (catalog names, descriptions) is untrusted too, so
sanitize_tool_result cleans it before the model reads it. None of this depends on the
model behaving: the limits are enforced here, in code.
"""
import re
import unicodedata

MAX_MESSAGE = 2000
MAX_STR = 500
NOTES_MAX = 1000
NUM_MAX = 1_000_000_000
TOOL_CALL_CAP = 3
# One message can change one thing. A hijacked model, or a customer who slips several
# requests into one message, cannot then chain a lead, a hold and a viewing in a single turn.
WRITES_PER_TURN = 1
TOOL_TEXT_MAX = 400
REPLY_MAX = 1500

INJECTION_REFUSAL = (
    "I can only help you browse the catalog and connect you with our team. "
    "What are you looking for?"
)
EMPTY_REPLY = "Sorry, I didn't catch that. Could you tell me what you're looking for?"
NO_MATCH_REPLY = (
    "I couldn't find anything in the catalog that matches that right now. "
    "Tell me a little more about what you're looking for and I'll search again."
)
FALLBACK_REPLY = (
    "Sorry, I can't help with that. I can search the catalog for you or "
    "connect you with our team."
)

_TEMPLATE_TOKENS = re.compile(
    r"<\|[^|>]{0,40}\|>|\[/?INST\]|<</?SYS>>|</?s>|^#{1,4}\s*(system|assistant)\b:?",
    re.IGNORECASE | re.MULTILINE,
)

_INJECTION_PATTERNS = [
    r"\b(ignore|disregard|forget|override)\b.{0,30}\b(instructions?|rules|prompt|guidelines|directives)\b",
    r"\b(reveal|show|print|repeat|display|leak|output|tell me)\b.{0,30}\b(system prompt|your (initial |original )?(instructions|prompt|rules)|hidden (prompt|instructions))",
    r"\bwhat (is|are) your (system prompt|instructions|rules)\b",
    r"\byou are now (?:an? |the )?(?:dan|unrestricted|unfiltered|jailbroken|free of|no longer|in (?:developer|debug|god) mode)\b",
    r"\bfrom now on,? you\b",
    r"\bpretend (to be|you are|you're)\b",
    r"\bact as (an? )?(unrestricted|unfiltered|jailbroken|dan|developer|admin|root|different)\b",
    r"\b(developer|debug|god|dan|jailbreak) mode\b",
    r"\bdo anything now\b",
    r"\bbypass\b.{0,30}\b(safety|filters?|rules|guardrails)\b",
    r"\bnew (system )?instructions?\s*:",
]
_INJECTION_RE = [re.compile(p, re.IGNORECASE) for p in _INJECTION_PATTERNS]

# Country-code TLDs plus the generic ones a scam link is likely to use. A bare host ("evil.shop")
# is only a link if its last label is on this list, which is what keeps "e.g.", "U.S." and
# "v2.1.0" out of it. Words like "cars" and "property" below are real TLDs (.cars, .property)
# that link detection needs, not catalog vocabulary.
_TLDS = (
    "ac ad ae af ag ai al am ao aq ar as at au aw ax az ba bb bd be bf bg bh bi bj bm bn bo br bs bt "
    "bw by bz ca cc cd cf cg ch ci ck cl cm cn co cr cu cv cw cx cy cz de dj dk dm do dz ec ee eg er "
    "es et eu fi fj fk fm fo fr ga gd ge gf gg gh gi gl gm gn gp gq gr gs gt gu gw gy hk hm hn hr ht "
    "hu id ie il im in io iq ir is it je jm jo jp ke kg kh ki km kn kp kr kw ky kz la lb lc li lk lr "
    "ls lt lu lv ly ma mc md me mg mh mk ml mm mn mo mp mq mr ms mt mu mv mw mx my mz na nc ne nf ng "
    "ni nl no np nr nu nz om pa pe pf pg ph pk pl pm pn pr ps pt pw py qa re ro rs ru rw sa sb sc sd "
    "se sg sh si sk sl sm sn so sr ss st su sv sx sy sz tc td tf tg th tj tk tl tm tn to tr tt tv tw "
    "tz ua ug uk us uy uz va vc ve vg vi vn vu wf ws ye yt za zm zw "
    "com net org info biz edu gov mil int mobi name pro tel asia cat jobs travel xxx aero coop museum "
    "app dev xyz online site shop store top club vip live link click cloud tech website space fun icu "
    "work world today life email page blog news media digital network systems solutions services "
    "agency company group center zone city bid win loan men date party review trade webcam stream "
    "download racing cricket science accountant gdn xin ink lol wtf ninja rocks bet casino poker game "
    "games bingo lotto sex porn adult sexy cam chat dating love support help guru expert tips tools "
    "software host hosting domains art design studio fashion style boutique shopping sale deal deals "
    "discount cheap buy market money cash loans bank credit finance capital fund investments exchange "
    "tax insurance health care clinic fitness yoga pet golf food pizza coffee cafe restaurant beer "
    "wine cars car auto autos boats bike homes house realty estate properties property rent "
    "apartments land builders repair tires wiki zip mov cfd sbs bond lat cyou buzz rest quest monster bar "
    "best uno kim dog mom"
).split()
_TLD = "|".join(sorted(_TLDS, key=len, reverse=True))
# A host starts at the start of a word: not after a word character, "@" or "-", and not after a
# "label." (so the domain half of an email address is skipped) but fine after "..." or ",".
_HOST_START = r"(?<![\w@-])(?<![\w-]\.)"
_HOST = rf"{_HOST_START}(?:[\w-]+\.)+(?:{_TLD})"

# Links in every form a chat client would make tappable: schemes (including the "hxxp" defanging
# trick), mailto:, t.me, www., any host-shaped token followed by a path ("evil.cfd/pay"; an unlisted
# TLD right after a "/" is skipped, since "$2/sq.ft/month" is a unit), a host on a listed TLD with a
# port or on its own, and an IPv4 address with a path or port. A bare host is not a link when more
# text of an email address follows it ("sam.it+deals@gmail.com").
_URL_RE = re.compile(
    r"(?:(?:https?|hxxps?|ftp|tg)://\S+"
    r"|mailto:\S+"
    r"|www\.\S+"
    r"|t\.me/\S*"
    rf"|{_HOST}\b(?::\d{{1,5}})?/\S*"
    rf"|(?<!/){_HOST_START}(?:[\w-]+\.)+[a-z]{{2,63}}(?![\w-])(?::\d{{1,5}})?/\S*"
    rf"|{_HOST}\b:\d{{1,5}}\b"
    rf"|(?P<bare>{_HOST}(?![\w@-]|\.\w|[\w.+%-]*@))"
    r"|(?<![\w.:-])(?:\d{1,3}\.){3}\d{1,3}(?:(?::\d{1,5})?/\S*|:\d{1,5}\b))",
    re.IGNORECASE,
)


def _link_or_text(match: re.Match) -> str:
    """Replacement for a link match. A bare host that is only digits before the TLD, right after a
    currency symbol or a thousands comma ("$20,000.Today", "$20.Today"), is a price followed by a
    word and stays as written. "888.com" or "24.shop" with nothing like that before it is a host."""
    bare = match.group("bare")
    if bare is not None and re.fullmatch(r"\d+", bare.rpartition(".")[0]):
        before = match.string[max(0, match.start() - 2):match.start()]
        if before[-1:] in ("$", "€", "£") or re.fullmatch(r"\d,", before):
            return match.group(0)
    return "[link removed]"


def _remove_links(text: str) -> str:
    return _URL_RE.sub(_link_or_text, text)


def _drop_invisible(text: str) -> str:
    """NFKC-normalise and drop control/format characters (zero-width ones included), keeping
    newlines and tabs."""
    text = unicodedata.normalize("NFKC", text)
    return "".join(ch for ch in text if ch in "\n\r\t" or unicodedata.category(ch) not in ("Cc", "Cf"))


def _clean(text: str) -> str:
    """NFKC-normalise, drop control/format characters, collapse whitespace."""
    return re.sub(r"\s+", " ", _drop_invisible(text)).strip()


def _strip_template_tokens(text: str) -> str:
    """Tokens are matched after invisible characters are gone (so "[IN<zero-width>ST]" is caught)
    and before whitespace collapses (the "### System:" form is anchored to the start of a line)."""
    return _clean(_TEMPLATE_TOKENS.sub(" ", _drop_invisible(text)))


def sanitize_user_text(text: str) -> str:
    return _strip_template_tokens(text)[:MAX_MESSAGE]


def is_injection_attempt(text: str) -> bool:
    return any(pattern.search(text) for pattern in _INJECTION_RE)


def _leaks_prompt(reply: str, system_prompt: str) -> bool:
    window = 40
    normalised_reply = re.sub(r"\s+", " ", reply.lower())
    normalised_prompt = re.sub(r"\s+", " ", system_prompt.lower())
    for start in range(0, max(1, len(normalised_prompt) - window), 10):
        if normalised_prompt[start:start + window] in normalised_reply:
            return True
    return False


def filter_reply(reply: str, system_prompt: str) -> str:
    if not reply.strip():
        return EMPTY_REPLY
    if _leaks_prompt(reply, system_prompt):
        return FALLBACK_REPLY
    reply = _remove_links(reply)
    if len(reply) > REPLY_MAX:
        reply = reply[:REPLY_MAX - 3].rstrip() + "..."
    return reply


def _defuse(text: str) -> str:
    text = _remove_links(_strip_template_tokens(text))
    for pattern in _INJECTION_RE:
        text = pattern.sub("[removed]", text)
    return text[:TOOL_TEXT_MAX]


def sanitize_tool_result(value):
    """A copy of a tool result that is safe to show the model.

    A catalog entry is data someone typed, so it gets the same treatment as a customer's
    message: control characters, chat-template tokens, links and known injection phrasings
    are removed and long text is cut. Numbers, flags and keys are left alone, so the result
    still means what it meant."""
    if isinstance(value, str):
        return _defuse(value)
    if isinstance(value, dict):
        return {key: sanitize_tool_result(item) for key, item in value.items()}
    if isinstance(value, list):
        return [sanitize_tool_result(item) for item in value]
    return value
