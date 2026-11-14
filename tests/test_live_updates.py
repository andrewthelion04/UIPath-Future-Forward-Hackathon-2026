"""Invalidări după commit, autentificare WS și reconectare fără date pierdute."""
import pytest
from starlette.websockets import WebSocketDisconnect

from app.core.db import Database
from conftest import app_client, make_settings


def test_database_notifies_only_after_commit_and_not_after_rollback():
    db = Database(":memory:")
    db.script("CREATE TABLE sample (id INTEGER PRIMARY KEY, value TEXT)")
    changes = []
    db.on_change = lambda: changes.append(True)
    with db.transaction():
        db.execute("INSERT INTO sample VALUES (1, 'first')")
        db.execute("UPDATE sample SET value='second' WHERE id=1")
        assert not changes
    assert len(changes) == 1
    with pytest.raises(RuntimeError):
        with db.transaction():
            db.execute("UPDATE sample SET value='discarded' WHERE id=1")
            raise RuntimeError("rollback")
    assert len(changes) == 1
    db.query("SELECT * FROM sample")
    db.execute("UPDATE sample SET value='none' WHERE id=999")
    assert len(changes) == 1
    db.close()


def authenticate(ws, token=""):
    ws.send_json({"type":"authenticate", "token":token})
    initial = ws.receive_json()
    assert initial["type"] == "snapshot"
    return initial["version"]


def test_change_is_broadcast_to_both_clients_and_reconnect_gets_current_version(client):
    prop = client.get("/properties").json()[0]
    with client.websocket_connect("/ws/updates") as first, client.websocket_connect("/ws/updates") as second:
        initial = authenticate(first)
        assert authenticate(second) == initial
        assert client.put(f"/properties/{prop['id']}", json={"city":"Updated city"}).status_code == 200
        event = first.receive_json()
        assert event["type"] == "changed" and event["version"] != initial
        assert second.receive_json()["version"] == event["version"]
    with client.websocket_connect("/ws/updates") as reconnected:
        assert authenticate(reconnected) == event["version"]


def test_websocket_protects_notifications_and_answers_with_api_token(tmp_path):
    with app_client(make_settings(tmp_path, api_token="test-token")) as client:
        with client.websocket_connect("/ws/updates") as ws:
            ws.send_json({"token":"incorrect"})
            with pytest.raises(WebSocketDisconnect) as closed:
                ws.receive_json()
            assert closed.value.code == 1008
        with client.websocket_connect("/ws/updates") as ws:
            authenticate(ws, "test-token")
            c = client.app.state.container
            c.notifications.notify(feature="test",title="New notification")
            update = ws.receive_json()
            assert update["type"] == "changed"
            assert set(update) == {"type","version"}  # Fără date private în flux.


def test_untrusted_websocket_origin_is_rejected(client):
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws/updates", headers={"origin":"https://untrusted.example"}):
            pass


def test_web_app_and_extension_origins_are_accepted(client):
    for origin in ["http://localhost:8501", "chrome-extension://example"]:
        with client.websocket_connect("/ws/updates", headers={"origin":origin}) as ws:
            authenticate(ws)
