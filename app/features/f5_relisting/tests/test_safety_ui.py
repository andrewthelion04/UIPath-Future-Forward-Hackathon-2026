"""UI-ul semnalează explicit eșecul și păstrează atribuirea sursei."""

from pathlib import Path

from streamlit.testing.v1 import AppTest

from app.features.f5_relisting.tests.test_relisting import FakeMarket, cancel
from conftest import app_client
from ui import api_client


def test_failed_proposal_is_visible_with_its_source(settings, monkeypatch):
    market = FakeMarket(mode="fail")
    market.attribution = "Inside Airbnb — CC BY 4.0"
    with app_client(settings, market=market) as client:
        cancel(client, "Emma Johnson")

        def request(method, path, *, params=None, json=None):
            response = client.request(method, path, params=params, json=json)
            response.raise_for_status()
            return response.json()

        monkeypatch.setattr(api_client, "request", request)
        page = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "ui_page.py"),
                                default_timeout=30).run()
        assert not page.exception
        assert any("could not be completed" in w.value for w in page.warning)
        assert any("Inside Airbnb" in c.value and "CC BY 4.0" in c.value for c in page.caption)
