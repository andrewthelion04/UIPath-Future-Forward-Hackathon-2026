# F4 — Analiza recenziilor

Cerere de analiză → recenzii din adaptor → snapshot → agent → recomandări trimise hostului.

| | |
|---|---|
| **Ascultă** | `ReviewAnalysisRequested` |
| **Emite** | niciun eveniment; rezultatul e o notificare către host |
| **Date deținute** | `f4_review_snapshots`, `f4_insights`; promptul din `prompts/system.md` |
| **Endpointuri** | `GET /f4/insights` |
| **Porturi** | `reviews`, `llm` |
| **Setări** | `F4_FETCH_TIMEOUT_S` (implicit 150) |

Dacă sursa de recenzii dă eroare sau depășește timeoutul, analiza folosește ultimul snapshot salvat și notificarea spune asta. Fără niciun snapshot, hostul află că analiza a fost sărită.

Cu `REVIEWS_PROVIDER=apify`, leagă întâi proprietatea de URL-ul unui anunț real (vezi README-ul din rădăcină).

Teste: `python manage.py test app/features/f4_review_analysis`
