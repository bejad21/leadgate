"""Talk mode, the direct-chat link and the phone-number flow, on the real services.

Real: Odoo, Supabase, MongoDB and the engine's webhook routes. Faked at the network edge: both
Telegram bots, so every message and keyboard can be inspected (the customer bot's sends are
captured, the alert bot's are answered by a stub). To keep it deterministic, leads are created
through the real Odoo tools without the language model, except in the last scenario, which uses the
real model. Everything it creates is removed again, even if a check fails.

Needs the stack running (Odoo, n8n) and a filled-in .env. From the repo root:

    python eval/live_talk_check.py
"""
import atexit
import hashlib
import hmac
import json
import os
import sys
import xmlrpc.client

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, ROOT)
sys.stdout.reconfigure(encoding="utf-8")
from dotenv import load_dotenv

load_dotenv(os.path.join(ROOT, ".env"))
os.environ.update(
    TELEGRAM_ALERTS_BOT_TOKEN="TALK-FAKE", TELEGRAM_ALERTS_CHAT_ID="777",
    TELEGRAM_ALERTS_WEBHOOK_SECRET="talk-secret", ODOO_PUBLIC_URL="http://localhost:8069",
)

import httpx
import respx
from fastapi.testclient import TestClient

import engine.main as m
from engine import store as store_module
from engine.core.agent_loop import AgentTurnResult
from engine.llm_client import ToolCall

E = os.environ
SUPA = E["SUPABASE_URL"].rstrip("/")
SVC = {"apikey": E["SUPABASE_SERVICE_KEY"], "Authorization": "Bearer " + E["SUPABASE_SERVICE_KEY"]}
u = E["ODOO_URL"].rstrip("/")
uid = xmlrpc.client.ServerProxy(u + "/xmlrpc/2/common").authenticate(E["ODOO_DB"], E["ODOO_USER"], E["ODOO_PASSWORD"], {})
rpc = xmlrpc.client.ServerProxy(u + "/xmlrpc/2/object")
odoo = lambda model, method, *a, **k: rpc.execute_kw(E["ODOO_DB"], uid, E["ODOO_PASSWORD"], model, method, list(a), k)

CUSTOMERS = {"A": 990501, "B": 990502, "C": 990503, "D": 990504, "E": 990505, "F": 990506, "G": 990507, "H": 990508, "I": 990509, "L": 990510}
EMAILS = [f"talk.{k.lower()}@example.com" for k in CUSTOMERS]
OWNER = 777
ok_all = True
customer_msgs, alerts, toasts = [], [], []
LEADS = {}


def check(name, cond, detail=""):
    global ok_all
    ok_all &= bool(cond)
    print(("PASS " if cond else "FAIL ") + name + (f"  [{str(detail)[:200]}]" if detail and not cond else ""))


def ref(chat):
    return hmac.new(E["CHAT_REF_SECRET"].encode(), str(chat).encode(), hashlib.sha256).hexdigest()[:12]


store = store_module.MongoStore(E["MONGODB_URI"])
store_module.set_store(store)
m.send_message = lambda chat_id, text, reply_markup=None, **_: customer_msgs.append({"chat": chat_id, "text": text, "markup": reply_markup})
client = TestClient(m.app)
counter = [8_500_000]
real_run_turn = m.run_turn


def cleanup():
    chats = list(CUSTOMERS.values())
    for chat in chats:
        for t in ("conversation_turns", "leads"):
            httpx.delete(f"{SUPA}/rest/v1/{t}?chat_ref=eq.{ref(chat)}", headers=SVC)
    db = store._db
    db["conversations"].delete_many({"chat_id": {"$in": chats}})
    db["handoffs"].delete_many({"chat_id": {"$in": chats}})
    db["lead_chats"].delete_many({"chat_id": {"$in": chats}})
    db["chat_flags"].delete_many({"chat_id": {"$in": chats}})
    db["owner_state"].delete_many({})
    leads = odoo("crm.lead", "search", [["email_from", "in", EMAILS], "|", ["active", "=", True], ["active", "=", False]])
    if leads:
        odoo("crm.lead", "unlink", leads)
    partners = odoo("res.partner", "search", [["email", "in", EMAILS]])
    if partners:
        odoo("res.partner", "unlink", partners)
    held = odoo("leadgate.catalog.item", "search", [["reserved_for", "in", EMAILS]])
    for item_id in held:
        odoo("leadgate.catalog.item", "write", [item_id], {"status": "available"})


atexit.register(cleanup)
cleanup()


def post_customer(chat, text=None, *, username=None, contact=None, from_id=None, name="Tester"):
    counter[0] += 1
    message = {"message_id": 1, "chat": {"id": chat, "type": "private"}, "from": {"id": from_id or chat, "first_name": name, **({"username": username} if username else {})}}
    if text is not None:
        message["text"] = text
    if contact is not None:
        message["contact"] = contact
    before = len(customer_msgs)
    r = client.post("/webhook/telegram", json={"update_id": counter[0], "message": message}, headers={"X-Telegram-Bot-Api-Secret-Token": m.config["TELEGRAM_WEBHOOK_SECRET"]})
    assert r.status_code == 200, r.text
    return customer_msgs[before:]


def owner_update(body):
    counter[0] += 1
    r = client.post("/webhook/alerts", json={"update_id": counter[0], **body}, headers={"X-Telegram-Bot-Api-Secret-Token": "talk-secret"})
    assert r.status_code == 200, r.text


def owner_says(text, reply_to=None, sender=OWNER):
    msg = {"message_id": 5, "from": {"id": sender}, "chat": {"id": sender}, "text": text}
    if reply_to:
        msg["reply_to_message"] = {"message_id": reply_to}
    before = len(customer_msgs)
    owner_update({"message": msg})
    return customer_msgs[before:]


def press(code, lead, sender=OWNER):
    owner_update({"callback_query": {"id": f"cb{counter[0]}", "from": {"id": sender}, "data": f"{code}:{lead}", "message": {"message_id": 1, "chat": {"id": sender}}}})


def sent_to_owner(since):
    return alerts[since:]


def buttons(payload):
    return [b for row in (payload.get("reply_markup") or {}).get("inline_keyboard", []) for b in row]


def alert_message_id(lead):
    """The id the stub gave the newest alert about a lead."""
    for index in range(len(alerts) - 1, -1, -1):
        if f"#{lead}" in alerts[index].get("text", "") or f"Lead #{lead}" in alerts[index].get("text", ""):
            return 5000 + index + 1
    return None


def make_lead(key, name, *, username=None, phone=None, email=None):
    """Create a real lead through the real Odoo tool, as the assistant would, and run the webhook's
    after-reply work (alert, store, mirror, and the question about a phone number)."""
    chat = CUSTOMERS[key]
    email = email or f"talk.{key.lower()}@example.com"
    args = {"name": f"Test car {key}", "customer_name": name, "customer_contact": " ".join(x for x in (email, phone) if x), "price": 21950.0}

    def scripted(history, adapter, llm, chat_id=None, write_limiter=None, tool_events=None):
        result = adapter.execute_tool("create_lead", args)
        call = ToolCall(name="create_lead", arguments=args)
        if tool_events is not None:
            tool_events.append((call, result))
        return AgentTurnResult(reply="Thanks, a person will follow up.", tool_calls_made=[call], tool_results=[result])

    m.run_turn = scripted
    try:
        replies = post_customer(chat, f"I'll take test car {key}. I'm {name}, {email}", username=username, name=name)
    finally:
        m.run_turn = real_run_turn
    row = httpx.get(f"{SUPA}/rest/v1/leads?chat_ref=eq.{ref(chat)}&select=odoo_lead_id&order=created_at.desc", headers=SVC).json()
    lead = row[0]["odoo_lead_id"] if row else None
    LEADS[key] = lead
    return lead, replies


def prompts_for(chat):
    return [x for x in customer_msgs if x["chat"] == chat and (x["markup"] or {}).get("keyboard")]


def lead_phone(lead):
    return odoo("crm.lead", "read", [lead], fields=["phone"])[0]["phone"]


with respx.mock(assert_all_mocked=False, assert_all_called=False) as router:
    def alert_handler(request):
        alerts.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 5000 + len(alerts)}})

    router.post("https://api.telegram.org/botTALK-FAKE/sendMessage").mock(side_effect=alert_handler)
    router.post("https://api.telegram.org/botTALK-FAKE/answerCallbackQuery").mock(side_effect=lambda r: (toasts.append(json.loads(r.content)), httpx.Response(200, json={"ok": True}))[1])
    router.route().pass_through()

    # ------------------------------------------------------------------ S1: a customer with a username
    print("== S1 a customer with a public username")
    n0 = len(alerts)
    lead_a, replies = make_lead("A", "Sarah Connor", username="sarah_c")
    check("the lead exists in Odoo and is mirrored", lead_a and odoo("crm.lead", "search_count", [["id", "=", lead_a]]) == 1, lead_a)
    a = [x for x in alerts[n0:] if "New lead" in x.get("text", "")][0]
    bl = {b["text"]: b for b in buttons(a)}
    check("the alert shows the username", "@sarah_c" in a["text"], a["text"])
    check("the alert has an Open chat link to the customer's profile", bl.get("Open chat", {}).get("url") == "https://t.me/sarah_c", bl)
    check("the alert has a Talk here button", bl.get("Talk here", {}).get("callback_data") == f"k:{lead_a}", bl)
    check("a customer with a username is not asked for a number", prompts_for(CUSTOMERS["A"]) == [])
    check("the store remembers the username", (store.get_lead_chat(lead_a) or {}).get("username") == "sarah_c")

    def boom(*a, **k):
        raise AssertionError("the assistant must stay quiet in human mode")

    press("k", lead_a)
    check("Talk here starts a session", store.get_talk() == lead_a and store.get_handoff(CUSTOMERS["A"]) is not None)
    got = owner_says("Hi Sarah, this is the showroom. Are you free at 3pm?")
    check("a plain message reaches the customer with no reply step", [x["text"] for x in got if x["chat"] == CUSTOMERS["A"]] == ["Hi Sarah, this is the showroom. Are you free at 3pm?"], got)
    notes = odoo("mail.message", "search_read", [["model", "=", "crm.lead"], ["res_id", "=", lead_a], ["message_type", "=", "comment"]], fields=["body"])
    check("the message is logged on the lead in Odoo", any("free at 3pm" in (n["body"] or "") for n in notes), notes)
    m.run_turn = boom
    n1 = len(alerts)
    post_customer(CUSTOMERS["A"], "Yes, 3pm works!", username="sarah_c")
    m.run_turn = real_run_turn
    fwd = [x for x in alerts[n1:] if "3pm works" in x.get("text", "")]
    check("the customer's answer is forwarded to the owner, the assistant stays silent", bool(fwd), alerts[n1:])
    check("the forwarded message can be answered with a plain message", [x["text"] for x in owner_says("Great, see you then") if x["chat"] == CUSTOMERS["A"]] == ["Great, see you then"])
    owner_says("/back")
    check("/back ends the session and hands the chat back", store.get_talk() is None and store.get_handoff(CUSTOMERS["A"]) is None)
    n2 = len(alerts)
    check("after /back a plain message goes nowhere and the owner is told how to start", owner_says("anyone there?") == [] and "/talk" in alerts[-1].get("text", ""), alerts[n2:])

    # ------------------------------------------------------------------ S2: two customers
    print("== S2 two customers at once")
    lead_b, _ = make_lead("B", "Omar Haddad", username="omar_h1")
    lead_c, _ = make_lead("C", "Lina Park")  # no username
    check("both leads exist", lead_b and lead_c, (lead_b, lead_c))
    owner_says(f"/talk {lead_b}")
    to_b = owner_says("to Omar")
    owner_says(f"/talk {lead_c}")
    to_c = owner_says("to Lina")
    check("switching customers sends each message to the right one only", [x["chat"] for x in to_b] == [CUSTOMERS["B"]] and [x["chat"] for x in to_c] == [CUSTOMERS["C"]], (to_b, to_c))
    mid = alert_message_id(lead_b)
    explicit = owner_says("explicit reply for Omar", reply_to=mid)
    check("an explicit reply to another customer's alert wins", [x["chat"] for x in explicit] == [CUSTOMERS["B"]], explicit)
    check("and the session stays with the customer being talked to", store.get_talk() == lead_c)
    still = owner_says("still Lina")
    check("the next plain message still goes to Lina", [x["chat"] for x in still] == [CUSTOMERS["C"]])
    owner_says(f"/back {lead_b}")
    check("/back for the other customer leaves the session alone", store.get_talk() == lead_c and store.get_handoff(CUSTOMERS["B"]) is None)
    owner_says("/talk")
    check("/talk alone says who you are talking to", "Lina Park" in alerts[-1]["text"], alerts[-1])
    owner_says("/back")

    # ------------------------------------------------------------------ S3: no username, shares a contact
    print("== S3 no username: asked for a number, shares their contact")
    ask = prompts_for(CUSTOMERS["C"])
    check("the customer with no username was asked, with a share-my-number button", len(ask) == 1 and ask[0]["markup"]["keyboard"][0][0]["request_contact"] is True, ask)
    check("the alert for that customer has no Open chat link", "Open chat" not in [b["text"] for b in buttons([x for x in alerts if f"#{lead_c}" in x.get("text", "") and "New lead" in x.get("text", "")][0])])
    check("the question is remembered so it is asked once", store.has_chat_flag(CUSTOMERS["C"], "phone_asked"))
    make_second = odoo("crm.lead", "search_count", [["email_from", "=", "talk.c@example.com"]])
    lead_c2, _ = make_lead("C", "Lina Park", email="talk.c@example.com")  # a second lead from the same chat
    check("a second lead from the same chat is not asked again", len(prompts_for(CUSTOMERS["C"])) == 1)
    n3 = len(alerts)
    reply = post_customer(CUSTOMERS["C"], contact={"phone_number": "971501234567", "first_name": "Lina", "user_id": CUSTOMERS["C"]}, name="Lina")
    newest = store.latest_lead_for_chat(CUSTOMERS["C"])["lead_id"]
    check("the number is on the customer's latest lead in Odoo", lead_phone(newest) == "+971501234567", lead_phone(newest))
    partner = odoo("crm.lead", "read", [newest], fields=["partner_id"])[0]["partner_id"]
    check("and on their contact", partner and odoo("res.partner", "read", [partner[0]], fields=["phone"])[0]["phone"] == "+971501234567")
    row = httpx.get(f"{SUPA}/rest/v1/leads?odoo_lead_id=eq.{newest}&select=phone", headers=SVC).json()
    check("and on the dashboard's copy of the lead", row and row[0]["phone"] == "+971501234567", row)
    na = [x for x in alerts[n3:] if "shared a phone number" in x.get("text", "")]
    check("the owner is alerted with the number", bool(na) and "+971501234567" in na[0]["text"], alerts[n3:])
    nb = {b["text"]: b for b in buttons(na[0])} if na else {}
    check("with a WhatsApp link and a Talk here button", nb.get("WhatsApp", {}).get("url") == "https://wa.me/971501234567" and "Talk here" in nb, nb)
    check("the customer is thanked and the share button is removed", reply and reply[-1]["markup"] == {"remove_keyboard": True}, reply)
    press("k", newest)
    check("Talk here from that alert reaches the customer", [x["chat"] for x in owner_says("Hi Lina, calling you now")] == [CUSTOMERS["C"]])
    owner_says("/back")
    reply2 = post_customer(CUSTOMERS["C"], contact={"phone_number": "971509999999", "first_name": "Lina", "user_id": CUSTOMERS["C"]}, name="Lina")
    check("sharing again does not overwrite the number or alert again", lead_phone(newest) == "+971501234567" and not [x for x in alerts[n3 + len(na):] if "shared a phone number" in x.get("text", "")])

    # ------------------------------------------------------------------ S4: types a number
    print("== S4 no username: types a number")
    lead_d, _ = make_lead("D", "Dana Lee")
    check("asked first", len(prompts_for(CUSTOMERS["D"])) == 1)
    n4 = len(alerts)
    post_customer(CUSTOMERS["D"], "sure, call me on 050 123 4567 please", name="Dana")
    check("a typed number is attached to the lead", lead_phone(lead_d) == "+971501234567", lead_phone(lead_d))
    check("and the owner is alerted", any("shared a phone number" in x.get("text", "") for x in alerts[n4:]), alerts[n4:])
    n5 = len(alerts)
    post_customer(CUSTOMERS["D"], "and I would pay 1 200 000 for a bigger one", name="Dana")
    check("a price typed later is not mistaken for a number", not any("shared a phone number" in x.get("text", "") for x in alerts[n5:]))

    # ------------------------------------------------------------------ S5: someone else's contact
    print("== S5 a contact card that is not the sender's")
    lead_e, _ = make_lead("E", "Eli Shaw")
    reply = post_customer(CUSTOMERS["E"], contact={"phone_number": "971508888888", "first_name": "Someone", "user_id": 123456}, name="Eli")
    check("someone else's contact card is refused", lead_phone(lead_e) in (False, None, "") and reply and "your own" in reply[-1]["text"].lower(), (lead_phone(lead_e), reply))
    reply = post_customer(CUSTOMERS["E"], contact={"phone_number": "971508888888", "first_name": "Eli"}, name="Eli")
    check("a contact with no proof of ownership is refused too", lead_phone(lead_e) in (False, None, ""))
    reply = post_customer(CUSTOMERS["E"], contact={"phone_number": "hello", "first_name": "Eli", "user_id": CUSTOMERS["E"]}, name="Eli")
    check("a shared 'number' that is not one is refused", lead_phone(lead_e) in (False, None, "") and reply and "phone number" in reply[-1]["text"].lower())

    # ------------------------------------------------------------------ S6 and S7: not asked, hostile username
    print("== S6/S7 already has a number; hostile username; typing before being asked")
    lead_f, _ = make_lead("F", "Fay Wong", phone="+971501112222")
    check("a customer who already gave a number is not asked", prompts_for(CUSTOMERS["F"]) == [])
    n6 = len(alerts)
    post_customer(CUSTOMERS["F"], "my other number is 050 123 4567", name="Fay")
    check("a number typed when we never asked is ignored", lead_phone(lead_f) == "+971501112222" and not any("shared a phone" in x.get("text", "") for x in alerts[n6:]))
    n7 = len(alerts)
    lead_g, _ = make_lead("G", "Gus Hill", username="x/../evil")
    g_alert = [x for x in alerts[n7:] if "New lead" in x.get("text", "")][0]
    check("a hostile username never becomes a link", all("url" not in b for b in buttons(g_alert)) and "evil" not in g_alert["text"], g_alert)
    check("a hostile username counts as none, so they are asked for a number", len(prompts_for(CUSTOMERS["G"])) == 1)
    check("and it is not stored", (store.get_lead_chat(lead_g) or {}).get("username") in (None, ""))

    # ------------------------------------------------------------------ S8: human mode and shared contact
    print("== S8 shares a contact while the owner is talking to them")
    lead_h, _ = make_lead("H", "Hana Roy")
    owner_says(f"/talk {lead_h}")
    n8 = len(alerts)
    post_customer(CUSTOMERS["H"], contact={"phone_number": "971507654321", "first_name": "Hana", "user_id": CUSTOMERS["H"]}, name="Hana")
    check("a number shared during human mode is still recorded", lead_phone(lead_h) == "+971507654321", lead_phone(lead_h))
    check("and the session is untouched", store.get_talk() == lead_h)
    owner_says("/back")

    # ------------------------------------------------------------------ S9: strangers
    print("== S9 nobody else can use it")
    owner_says(f"/talk {lead_a}", sender=999)
    check("a stranger cannot start a session", store.get_talk() is None)
    press("k", lead_a, sender=999)
    check("a stranger cannot press Talk here", store.get_talk() is None)
    owner_says(f"/talk {lead_a}")
    got = owner_says("hello", sender=999)
    check("a stranger's messages are not relayed even during a session", got == [])
    owner_says("/back")
    r = client.post("/webhook/alerts", json={"update_id": 1, "message": {"from": {"id": OWNER}, "chat": {"id": OWNER}, "text": "/talk 1"}}, headers={"X-Telegram-Bot-Api-Secret-Token": "guess"})
    check("a wrong webhook secret is refused", r.status_code == 401)
    check("a customer typing /talk is only a customer", not [x for x in post_customer(CUSTOMERS["B"], "/talk 1", username="omar_h1") if "Talking to" in x.get("text", "")] and store.get_talk() is None)

    # ------------------------------------------------------------------ S10: the real model
    print("== S10 the real model: a hold from a customer with a username, then a chat")
    m.run_turn = real_run_turn
    import time

    def says_with_resends(text):
        """The free model tier rate-limits now and then; a real customer would just send it again."""
        for attempt in range(5):
            got = post_customer(CUSTOMERS["L"], text, username="lena_k1", name="Lena")
            if not any(x["text"] == m.ERROR_REPLY for x in got):
                return got
            time.sleep(20)
        return got

    r1 = says_with_resends("Show me certified Toyotas under 30000, cheapest first")
    r2 = says_with_resends("Please hold the cheapest one for me. I'm Lena Kay, talk.l@example.com, +971 50 111 2233.")
    row = httpx.get(f"{SUPA}/rest/v1/leads?chat_ref=eq.{ref(CUSTOMERS['L'])}&select=odoo_lead_id,kind&order=created_at.desc", headers=SVC).json()
    lead_l = row[0]["odoo_lead_id"] if row else None
    model_down = any(x["text"] == m.ERROR_REPLY for x in r1 + r2)
    if model_down:
        # The free model tier is rate limited or out of quota for the day. That says nothing about
        # this feature, so it is reported as skipped, not as a failure.
        print("SKIP the real-model scenario: the language model was unavailable (rate limited)")
    else:
        check("the model placed a hold", row and row[0]["kind"] == "reservation", (row, [x["text"][:80] for x in r2]))
    if lead_l:
        al = [x for x in alerts if f"#{lead_l}" in x.get("text", "") and "reservation" in x.get("text", "").lower()]
        lb = {b["text"]: b for b in buttons(al[0])} if al else {}
        check("its alert has Open chat and Talk here", lb.get("Open chat", {}).get("url") == "https://t.me/lena_k1" and "Talk here" in lb, lb)
        check("a customer who gave a phone in the chat is not asked for one", prompts_for(CUSTOMERS["L"]) == [])
        press("k", lead_l)
        check("and talk mode works on it", [x["chat"] for x in owner_says("Hi Lena, the car is held for you")] == [CUSTOMERS["L"]])
        owner_says("/back")

cleanup()
left = odoo("crm.lead", "search_count", [["email_from", "in", EMAILS], "|", ["active", "=", True], ["active", "=", False]])
check("everything the run created was removed", left == 0 and store._db["lead_chats"].count_documents({"chat_id": {"$in": list(CUSTOMERS.values())}}) == 0, left)
print("ALL OK" if ok_all else "SOME FAILED")
