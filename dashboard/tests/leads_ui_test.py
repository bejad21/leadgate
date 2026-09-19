"""Browser tests for the Leads view.

Runs against a dev server (npm run dev) and the real Supabase project in .env:
signed out it shows sample data; signed in it shows live leads and conversations.
Rows the test inserts are removed again at the end.

    pip install playwright python-dotenv httpx && playwright install chromium
    python dashboard/tests/leads_ui_test.py
"""
import sys
import time
from pathlib import Path

import httpx
from dotenv import dotenv_values
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent.parent
ENV = dotenv_values(ROOT / ".env")
URL = "http://localhost:5173/#/leads"
SUPABASE = ENV["SUPABASE_URL"].rstrip("/")
SERVICE = {
    "apikey": ENV["SUPABASE_SERVICE_KEY"],
    "Authorization": "Bearer " + ENV["SUPABASE_SERVICE_KEY"],
    "Content-Type": "application/json",
    "Prefer": "return=minimal",
}
EMAIL, PASSWORD = ENV["DASHBOARD_LOGIN_EMAIL"], ENV["DASHBOARD_LOGIN_PASSWORD"]
results = []


def check(name, ok, detail=""):
    results.append(ok)
    print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail else ""))


def insert(table, row):
    r = httpx.post(f"{SUPABASE}/rest/v1/{table}", headers=SERVICE, json=row, timeout=20)
    assert r.status_code in (200, 201, 204), r.text


def cleanup():
    for table, col in (("conversation_turns", "chat_ref"), ("leads", "chat_ref")):
        httpx.delete(f"{SUPABASE}/rest/v1/{table}?{col}=like.uitest-*", headers=SERVICE, timeout=20)


def open_page(browser, w=1440, h=900):
    ctx = browser.new_context(viewport={"width": w, "height": h})
    page = ctx.new_page()
    errors, private_requests = [], []
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on("request", lambda r: private_requests.append(r.url) if ("/rest/v1/leads" in r.url or "/rest/v1/conversation_turns" in r.url) else None)
    page.goto(URL, wait_until="load")
    page.wait_for_selector(".slip", timeout=15000)
    page.wait_for_timeout(600)
    return ctx, page, errors, private_requests


cleanup()
with sync_playwright() as p:
    browser = p.chromium.launch()

    # ---------- signed out: samples ----------
    ctx, page, errors, private_requests = open_page(browser)
    check("signed out: sample leads are shown", page.locator(".slip").count() == 3)
    check("signed out: tallies match the samples", page.locator(".tape-count").all_inner_texts() == ["5", "3", "1"], str(page.locator(".tape-count").all_inner_texts()))
    check("signed out: the roll is labelled Sample", page.locator(".roll-sample").count() == 1)
    check("signed out: private tables are never requested", not private_requests, str(private_requests))
    check("signed out: no console errors", not errors, str(errors))

    page.locator(".slip", has_text="Sarah Connor").click()
    roll = page.locator(".roll").inner_text()
    check("roll shows the customer's words", "I'll take the 2020 Toyota Camry at $21,834" in roll)
    check("roll shows what the assistant did", "Searched the cars" in roll and "Toyota · up to $25,000" in roll and "Created a lead" in roll, "")
    check("bold in the reply renders as <strong>, not asterisks", page.locator(".roll strong").count() >= 1 and "**" not in roll)
    check("the unverified price is flagged on its slip", page.locator(".slip", has_text="Mallory").locator(".slip-flag").count() == 1)

    page.locator(".other", has_text="Ignore all previous instructions").click()
    check("a turned-away attempt is stamped in the roll", page.locator(".roll-blocked").count() == 1)
    check("selected row is marked", page.locator(".other[data-selected]").count() == 1)

    # keyboard: tab to a slip and press Enter
    page.locator(".slip").first.focus()
    page.keyboard.press("Enter")
    check("keyboard: Enter picks a slip", page.locator(".slip[data-selected]").count() == 1)

    # sign-in errors
    page.fill("input[type=email]", EMAIL)
    page.fill("input[type=password]", "definitely-wrong")
    page.click("button.signin-button")
    page.wait_for_selector(".signin-error", timeout=10000)
    check("wrong password shows a plain message", "do not match" in page.locator(".signin-error").inner_text())
    check("still signed out after a failed attempt", page.locator(".signin").count() == 1)

    # ---------- sign in ----------
    page.fill("input[type=password]", PASSWORD)
    page.click("button.signin-button")
    page.wait_for_selector(".signout", timeout=15000)
    page.wait_for_timeout(1500)
    check("signed in: the sign-in plate is gone", page.locator(".signin").count() == 0)
    check("signed in: samples are gone", page.locator(".roll-sample").count() == 0)
    check("signed in: an empty table says so", "No conversations yet" in page.locator(".pad").inner_text() or page.locator(".slip").count() > 0)

    # ---------- live inserts over Realtime ----------
    before = page.locator(".slip").count()
    chat = f"uitest-{int(time.time())}"
    insert("conversation_turns", {"chat_ref": chat, "domain_type": "cars", "user_message": "Any Honda under 20000?", "reply": "Here are **3 Hondas** I found.", "tool_calls": [{"name": "search_inventory", "arguments": {"make": "Honda", "price_max": 20000}}], "blocked": False})
    insert("leads", {"odoo_lead_id": 990001, "domain_type": "cars", "item_name": "2019 Honda Odyssey", "customer_name": "UI Tester", "email": "ui.tester@example.com", "phone": "+971501112222", "price": 32900, "price_verified": True, "chat_ref": chat})
    try:
        page.wait_for_function(f"document.querySelectorAll('.slip').length === {before + 1}", timeout=15000)
        check("a new lead appears live, with no reload", True)
    except Exception:
        check("a new lead appears live, with no reload", False, "timed out")
    slip = page.locator(".slip", has_text="UI Tester")
    check("slip shows the contact", "ui.tester@example.com" in slip.inner_text() and "+971501112222" in slip.inner_text())
    slip.click()
    roll = page.locator(".roll").inner_text()
    check("the live conversation is in the roll", "Any Honda under 20000?" in roll and "Searched the cars" in roll and "Honda · up to $20,000" in roll, roll[:120].replace("\n", " "))
    check("tallies count the live data", page.locator(".tape-count").all_inner_texts()[1] == str(before + 1), str(page.locator(".tape-count").all_inner_texts()))

    # ---------- hostile text is inert ----------
    chat2 = f"uitest-x-{int(time.time())}"
    insert("conversation_turns", {"chat_ref": chat2, "domain_type": "cars", "user_message": "<img src=x onerror=window.__pwned=1> <script>window.__pwned=1</script>", "reply": "**<b>bold?</b>** and <a href='https://evil.example'>link</a>", "tool_calls": [{"name": "create_lead", "arguments": {"name": "<i>x</i>", "price": 1}}], "blocked": False})
    insert("leads", {"odoo_lead_id": 990002, "domain_type": "cars", "item_name": "<u>Injected</u> item", "customer_name": "<b>Boss</b>", "email": None, "phone": None, "price": None, "price_verified": True, "chat_ref": chat2})
    page.wait_for_function("document.querySelectorAll('.slip').length >= 2", timeout=15000)
    page.locator(".slip", has_text="Injected").click()
    page.wait_for_timeout(300)
    check("markup in messages is shown as text", "<img src=x" in page.locator(".roll").inner_text() and page.locator(".roll img, .roll script, .roll a").count() == 0)
    check("no script ran", page.evaluate("window.__pwned === undefined"))
    check("bold markers around markup do not create elements", page.locator(".roll b, .roll i, .slip b, .slip u").count() == 0)

    # ---------- persistence and sign-out ----------
    page.reload(wait_until="load")
    page.wait_for_selector(".signout", timeout=15000)
    try:
        page.wait_for_function("document.querySelectorAll('.slip').length >= 2", timeout=15000)
        reloaded = True
    except Exception:
        reloaded = False
    check("the session survives a reload and the leads load again", reloaded and page.locator(".roll-sample").count() == 0)
    page.click(".signout")
    page.wait_for_selector(".signin", timeout=10000)
    check("sign-out returns to the samples", page.locator(".roll-sample").count() == 1 and page.locator(".slip").count() == 3)
    ctx.close()

    # ---------- other views still work ----------
    ctx = browser.new_context(viewport={"width": 1440, "height": 900})
    page = ctx.new_page()
    page.goto("http://localhost:5173/#/leads", wait_until="load")
    page.wait_for_selector(".slip")
    page.click(".tab >> text=Keys")
    page.wait_for_selector(".key-cell[data-status]", state="attached", timeout=15000)
    check("switching to Keys shows the board", page.locator(".key-cell[data-status]").count() > 0 and page.url.endswith("#/"))
    check("the domain switch is shown on Keys", page.locator(".switch").count() == 1)
    page.click(".tab >> text=Leads")
    page.wait_for_selector(".slip")
    check("the domain switch is hidden on Leads", page.locator(".switch").count() == 0)
    ctx.close()

    # ---------- phone ----------
    ctx, page, errors, _ = open_page(browser, 390, 844)
    overflow = page.evaluate("document.documentElement.scrollWidth - window.innerWidth")
    check("phone: no horizontal scroll", overflow <= 0, f"{overflow}px")
    check("phone: no console errors", not errors, str(errors))
    page.locator(".slip").nth(1).click()
    page.wait_for_timeout(900)
    top = page.locator(".roll").bounding_box()["y"]
    check("phone: picking a slip brings the conversation into view", -5 <= top <= 300, f"roll top at {top:.0f}px")
    check("phone: the back link is visible", page.locator(".roll-back").is_visible())
    ctx.close()

    browser.close()

cleanup()
left = httpx.get(f"{SUPABASE}/rest/v1/leads?select=id&chat_ref=like.uitest-*", headers=SERVICE).json()
check("test rows were cleaned up", left == [], str(left))

failed = results.count(False)
print(f"\n{len(results) - failed}/{len(results)} checks passed")
sys.exit(1 if failed else 0)
