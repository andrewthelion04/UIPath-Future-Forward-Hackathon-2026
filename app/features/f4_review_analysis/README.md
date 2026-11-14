# F4 — Analiza recenziilor

Cerere de analiză → câte un eveniment per proprietate → recenzii din adaptor → snapshot (doar dacă s-au schimbat) → agent → recomandări structurate (temă, prioritate, acțiune, citate) → notificare către host, cu comparația față de analiza anterioară.

| | |
|---|---|
| **Ascultă** | `ReviewAnalysisRequested`; privat: `PropertyReviewAnalysisRequested` |
| **Emite** | privat: `PropertyReviewAnalysisRequested` (câte unul per proprietate); rezultatul e o notificare către host |
| **Date deținute** | `f4_review_snapshots`, `f4_insights`, `f4_recommendations`; promptul din `prompts/system.md` |
| **Endpointuri** | `GET /f4/insights` (cu `items` sortate după prioritate și `changes`), `GET /f4/snapshots` |
| **Porturi** | `reviews`, `llm` |
| **Setări** | `F4_FETCH_TIMEOUT_S` (implicit 150), `F4_PROPERTY_TIMEOUT_S` (implicit 600) |

- Unealta `add_recommendation` acceptă doar citate care apar într-o recenzie, o temă o singură dată și maximum 5 recomandări.
- Notificarea spune ce teme sunt noi, care persistă și care nu mai apar față de ultima analiză a proprietății.
- Dacă recenziile sunt identice cu ultimul snapshot, nu se salvează unul nou; dacă există deja o analiză pe ele, hostul află că nu s-a schimbat nimic, iar agentul nu mai rulează.
- Dacă sursa de recenzii dă eroare sau depășește timeoutul, analiza folosește ultimul snapshot salvat și notificarea spune asta. Fără niciun snapshot, hostul află că analiza a fost sărită.
- La „toate proprietățile”, fiecare proprietate rulează în handlerul ei, cu timeoutul ei; una care cade sau expiră nu le afectează pe celelalte.

## Recenzii reale (Apify)

Actor: `tri_angle/airbnb-reviews-scraper` (verificat live în noiembrie 2026). În `.env`:

```
REVIEWS_PROVIDER=apify
APIFY_TOKEN=<token din Apify: Settings → API & Integrations>
APIFY_REVIEWS_ACTOR_ID=tri_angle/airbnb-reviews-scraper
APIFY_REVIEWS_INPUT_TEMPLATE='{"startUrls": [{"url": "{listing_url}"}], "maxReviewsPerListing": 50, "sortBy": "most-recent", "locale": "en-US"}'
```

Anunțurile demo sunt fictive, deci fiecare e legat de un anunț Airbnb real, comparabil, din `listings.json`. Cu API-ul pornit (și din nou după `manage.py seed` sau reset):

```
.venv/Scripts/python -m app.features.f4_review_analysis.link_listings    # Windows
.venv/bin/python -m app.features.f4_review_analysis.link_listings        # macOS / Linux
```

O proprietate fără anunț legat e sărită, cu avertisment către host. Cost: ~$0.12 per anunț și per rulare; o rulare cu recenzii neschimbate nu creează snapshot nou și nu pornește agentul (dar actorul tot rulează).

Teste: `python manage.py test app/features/f4_review_analysis`
