from fastapi import FastAPI, Request, Response

from engine.config import load_config
from engine.telegram_client import extract_message, send_message

app = FastAPI(title="LeadGate Engine")

config = load_config()

# In-memory conversation history, keyed by chat_id. Acceptable for a
# portfolio project; a production deployment would move this to a
# persistent store such as Redis or Supabase so history survives restarts
# and is shared across worker processes.
conversation_history: dict[int, list[dict]] = {}

PLACEHOLDER_REPLY = "Got your message — the AI backend isn't wired in yet (Task 2.4)."


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/webhook/telegram")
async def telegram_webhook(request: Request):
    # Verify the secret token before doing anything else, including
    # parsing the request body, so a malformed/malicious body can't cause
    # a crash before the auth check runs.
    secret_header = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
    if secret_header != config["TELEGRAM_WEBHOOK_SECRET"]:
        return Response(status_code=401)

    body = await request.json()
    extracted = extract_message(body)
    if extracted is None:
        # Non-text update (e.g. edited message, channel post, callback
        # query) - nothing to reply to, acknowledge and move on.
        return {"ok": True}

    chat_id, text = extracted
    conversation_history.setdefault(chat_id, []).append({"role": "user", "content": text})

    # Task 2.4 will replace this placeholder with a real run_turn() call
    # through the agent loop.
    send_message(chat_id, PLACEHOLDER_REPLY)

    return {"ok": True}
