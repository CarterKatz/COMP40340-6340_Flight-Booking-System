import uuid

from fastapi import FastAPI

app = FastAPI()

BOOT_ID = uuid.uuid4().hex


@app.get("/health")
def health():
    return {"status": "ok", "boot_id": BOOT_ID, "version": "0.1"}