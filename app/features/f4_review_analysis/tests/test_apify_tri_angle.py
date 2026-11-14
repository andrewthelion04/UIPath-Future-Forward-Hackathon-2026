"""Adaptorul Apify cu actorul ales de echipă: `tri_angle/airbnb-reviews-scraper`.

Transport HTTP simulat, fără rețea. Itemul de mai jos are forma din documentația actorului
(Apify Store, citită în noiembrie 2026); de reconfirmat după prima rulare reală.
"""

from __future__ import annotations

import json
from datetime import date

import httpx

from app.adapters.apify.reviews import ApifyReviewsAdapter, clean_text, normalize_review

TEMPLATE = (
    '{"startUrls": [{"url": "{listing_url}"}], "maxReviewsPerListing": 50, '
    '"sortBy": "most-recent", "locale": "en-US"}'
)
LISTING = "https://www.airbnb.com/rooms/12937"
TRI_ANGLE_ITEM = {
    "startUrl": LISTING,
    "id": "1187002287342743799",
    "language": "en",
    "text": "Great location, but the wifi kept dropping.",
    "localizedText": None,
    "localizedReview": None,
    "createdAt": "2024-06-25T18:01:39Z",
    "reviewee": {"id": "50124", "firstName": "Orestes"},
    "reviewer": {"id": "140401498", "firstName": "Megan"},
    "reviewHighlight": "Stayed a few nights",
    "highlightType": "LENGTH_OF_STAY",
    "rating": 5,
    "response": None,
    "ratingAccessibilityLabel": "Rating, 5 stars",
}


async def test_tri_angle_actor_id_input_and_output_are_handled(container):
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(201, json=[TRI_ANGLE_ITEM])

    adapter = ApifyReviewsAdapter(
        token="apify-token",
        actor_id="tri_angle/airbnb-reviews-scraper",  # exact cum apare în Store
        input_template=TEMPLATE,
        ids=container.ids,
        timeout_s=30,
        transport=httpx.MockTransport(respond),
    )
    container.ids.link("airbnb", "listing", LISTING, "prop_demo")
    try:
        batch = await adapter.fetch_reviews("prop_demo")
    finally:
        await adapter.aclose()

    (request,) = requests
    assert request.url.path == "/v2/acts/tri_angle~airbnb-reviews-scraper/run-sync-get-dataset-items"
    assert json.loads(request.content) == {
        "startUrls": [{"url": LISTING}],
        "maxReviewsPerListing": 50,
        "sortBy": "most-recent",
        "locale": "en-US",
    }
    (review,) = batch.reviews
    assert (review.rating, review.author, review.reviewed_on) == (5.0, "Megan", date(2024, 6, 25))
    assert review.text == "Great location, but the wifi kept dropping."
    assert review.property_id == "prop_demo" and review.source == "apify"


# Formă observată la rularea reală (noiembrie 2026): original în română, traducere în engleză
# (după `locale`), HTML în text.
TRANSLATED_ITEM = {
    "id": "1803505066787722927",
    "language": "ro",
    "text": "Locația e foarte bună.<br/>Mie mi-a lipsit un birou să lucrez la laptop",
    "localizedText": "The location is very good.<br/>I missed having a desk &amp; a lamp",
    "localizedReview": {"text": "The location is very good.<br/>I missed having a desk &amp; a lamp",
                        "disclaimer": "Translated from Romanian"},
    "createdAt": "2026-10-24T08:42:37Z",
    "reviewer": {"firstName": "Cristian"},
    "rating": 4,
}  # fmt: skip


def test_translation_is_preferred_and_html_is_cleaned():
    review = normalize_review(TRANSLATED_ITEM, "prop_demo")
    assert review.text == "The location is very good.\nI missed having a desk & a lamp"
    assert (review.rating, review.author, review.reviewed_on) == (4.0, "Cristian", date(2024 + 2, 10, 24))

    original_only = {k: v for k, v in TRANSLATED_ITEM.items() if k not in ("localizedText", "localizedReview")}
    assert normalize_review(original_only, "p").text.startswith("Locația e foarte bună.\nMie")

    only_review_dict = {"id": "x", "rating": 5, "localizedReview": {"text": "Great <b>stay</b>"}}
    assert normalize_review(only_review_dict, "p").text == "Great stay"


def test_clean_text_handles_empty_and_odd_values():
    assert clean_text("<br/><br/>  ") is None
    assert clean_text(None) is None and clean_text(42) is None
    assert clean_text("a<BR>b &quot;c&quot;") == 'a\nb "c"'
