from fastapi.testclient import TestClient
from app.main import app

with TestClient(app) as c:
    r = c.get("/api/health")
    print("HEALTH:", r.status_code, r.json())
    r2 = c.get("/api/engine/status")
    print("ENGINE:", r2.status_code, r2.json())
