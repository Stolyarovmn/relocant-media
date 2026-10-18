from fastapi.testclient import TestClient

from relocant_media.api.app import app


def test_health() -> None:
    client = TestClient(app)
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_webhook_classifies_update() -> None:
    client = TestClient(app)
    response = client.post("/telegram/webhook", json={"update_id": 11, "message": {}})
    assert response.status_code == 200
    assert response.json() == {"ok": True, "update_id": 11, "kind": "message"}
