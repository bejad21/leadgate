from fastapi import FastAPI

app = FastAPI(title="LeadGate Engine")


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}
