"""Relocare OwnerRez cap-coadă prin API HTTP controlat, fără scrieri pe Stage."""
import json
import httpx
import pytest

from app.adapters.ownerrez.adapter import OwnerRezAdapter
from conftest import approve


@pytest.mark.parametrize("outcome", ["success", "rejected", "unverified"])
def test_ownerrez_relocation_updates_both_calendars_only_after_verified_move(client, outcome):
    c = client.app.state.container
    state = {"property_id":10, "writes":[], "messages":[]}

    def booking():
        return {"id":101, "property_id":state["property_id"], "status":"active", "type":"booking",
                "arrival":"2026-06-03", "departure":"2026-06-06", "adults":2,
                "guest":{"first_name":"OwnerRez","last_name":"Guest"}, "currency_code":"RON",
                "thread_ids":[901]}

    def transport(request):
        path = request.url.path
        if path == "/v2/properties":
            return httpx.Response(200,json={"items":[
                {"id":i,"name":name,"bedrooms":1,"max_guests":4,"currency_code":"RON",
                 "address":{"city":"Bucharest","street1":name}} for i,name in [(10,"OR Source"),(20,"OR Target")]]})
        if path == "/v2/bookings":
            wanted = request.url.params.get("property_ids", "10,20").split(",")
            return httpx.Response(200,json={"items":[booking()] if str(state["property_id"]) in wanted else []})
        if path == "/v2/bookings/101":
            if request.method == "PATCH":
                payload = json.loads(request.content)
                state["writes"].append(payload)
                if outcome == "rejected":
                    return httpx.Response(400,json={"message":"Booking must be moved on its channel"})
                if outcome == "success":
                    state["property_id"] = payload["property_id"]
            return httpx.Response(200,json=booking())
        if path == "/v2/messages":
            state["messages"].append(json.loads(request.content)["body"])
            return httpx.Response(200,json={})
        if path == "/v2/spotrates":
            return httpx.Response(200,json={"items":[]})
        return httpx.Response(404,json={})

    c.pms.register(OwnerRezAdapter(base_url="https://apistage.ownerrez.com", ids=c.ids,
        access_token="test-token", transport=httpx.MockTransport(transport)))
    assert client.post("/integrations/ownerrez/sync").status_code == 200
    source, target = [next(p for p in client.get("/properties").json() if p["name"] == name)
                      for name in ["OR Source","OR Target"]]
    b = next(b for b in client.get("/bookings").json() if b["pms_provider"] == "ownerrez")
    response = client.post("/incidents/report?wait=true",json={"property_id":source["id"],
        "booking_id":b["id"],"description":"The air conditioning is not working","severity":"high"})
    assert response.status_code == 200
    incident = client.get("/incidents").json()[0]
    assert incident["relocation_property_id"] == target["id"]
    approve(client,incident["approval_id"],"relocate")
    now = client.get(f"/incidents/{incident['id']}").json()
    moved = client.get(f"/bookings/{b['id']}").json()
    assert state["writes"] == [{"property_id":20}]
    if outcome == "success":
        assert now["status"] == "relocated"
        assert moved["property_id"] == target["id"]
        client.portal.call(c.pms.get("ownerrez").move_reservation, b["id"], target["id"])
        assert state["writes"] == [{"property_id":20}]
        assert client.post(f"/incidents/{incident['id']}/review-relocation").status_code == 409
        for prop, available in [(source,True),(target,False)]:
            days = client.get(f"/properties/{prop['id']}/calendar",params={"start":"2026-06-03","days":3}).json()
            assert all(d["available"] == available for d in days)
        assert len(state["messages"]) == 2
    else:
        assert now["status"] == "awaiting_decision"
        assert moved["property_id"] == source["id"]
        assert len(state["messages"]) == 1


def test_old_unapplied_relocation_requires_a_new_approval(client):
    c = client.app.state.container
    b = next(b for b in c.world.list_bookings() if b.guest_name == "Lukas Mueller")
    client.post("/incidents/report?wait=true",json={"property_id":b.property_id,
        "booking_id":b.id,"description":"The air conditioning is not working"})
    incident = client.get("/incidents").json()[0]
    approve(client,incident["approval_id"],"relocate")
    assert client.get(f"/bookings/{b.id}").json()["property_id"] != b.property_id
    c.world.upsert_booking(b)
    result = client.post(f"/incidents/{incident['id']}/review-relocation?wait=true")
    assert result.status_code == 200
    revised = client.get(f"/incidents/{incident['id']}").json()
    assert revised["status"] == "awaiting_decision"
    assert revised["approval_id"] != incident["approval_id"]
    assert revised["closure_approval_id"] is None
