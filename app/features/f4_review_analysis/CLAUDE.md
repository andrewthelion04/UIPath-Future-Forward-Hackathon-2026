# F4 — Analiza recenziilor

## Scop
La cererea hostului, preia recenziile unei proprietăți din sursa configurată (mock sau Apify), le salvează ca snapshot cu sursă și dată, apoi un agent le analizează și propune îmbunătățiri concrete. Recomandările ajung la host ca notificare. Dacă sursa de recenzii cade, folosește ultimul snapshot salvat și îi spune hostului.

## Contract
- Ascultă: `ReviewAnalysisRequested` (`property_id=None` înseamnă toate proprietățile) și evenimentul privat `PropertyReviewAnalysisRequested` (`events.py`).
- Emite: `PropertyReviewAnalysisRequested` (privat, consumat doar de F4: câte unul per proprietate); rezultatul ajunge la host ca notificare.
- Date deținute: `f4_review_snapshots`, `f4_insights`, `f4_recommendations`; promptul din `prompts/system.md`.
- Porturi folosite: `reviews`, `llm` (prin `ctx.agent`).
- Cereri de aprobare: niciuna; recomandările sunt informative.

## Stare curentă
- `ReviewAnalysisRequested` → câte un `PropertyReviewAnalysisRequested` per proprietate (aceeași corelare) → handler propriu, cu timeoutul lui (`F4_PROPERTY_TIMEOUT_S`).
- Per proprietate: `reviews.fetch_reviews` cu timeout → snapshot (doar dacă recenziile s-au schimbat) → agentul `f4_review_analysis.recommend` → `f4_insights` + `f4_recommendations` → notificare cu comparația față de analiza anterioară.
- Uneltele agentului: `get_rating_stats` (număr, medie, distribuție, calculate în cod), `get_reviews` (filtru `max_rating`, `limit`, text trunchiat) și `add_recommendation(theme, priority, action, quotes)`.
- `add_recommendation` impune în cod: citatele trebuie să apară într-o recenzie (normalizare de majuscule/spații/ghilimele, fragmente legate cu „...”), o temă o singură dată (a doua oară o înlocuiește, cheie `theme_key`), maximum 5 recomandări. Încălcările se întorc modelului ca eroare de unealtă.
- Recomandările se salvează pe rânduri și se întorc sortate după prioritate (`high` → `medium` → `low`, apoi ordinea dată). `Insight.recommendations` rămâne text lizibil (lista formatată), pentru compatibilitate.
- Comparația (`Insight.changes`: `new`, `persisting`, `resolved`) se face pe `theme_key` față de analiza imediat anterioară a proprietății; temele anterioare îi sunt date agentului în obiectiv, ca să le refolosească numele.
- Recenzii identice cu ultimul snapshot (aceeași sursă, aceleași recenzii; data preluării nu contează): nu se salvează snapshot nou. Dacă snapshot-ul are deja o analiză, agentul nu mai rulează și hostul primește „No new reviews for …” cu recomandările valabile. Dacă nu are (ex. agentul a căzut data trecută), analiza rulează pe snapshot-ul existent.
- Eșec sau timeout al sursei: se folosește ultimul snapshot, iar notificarea e marcată ca avertisment. Fără snapshot: notificare „skipped”.
- O proprietate care eșuează sau expiră nu le oprește pe celelalte.
- `GET /f4/insights` (cu `items` și `changes`), `GET /f4/snapshots` (metadate, fără texte) și pagina de UI (carduri sortate după prioritate, comparația, lista snapshot-urilor).
- Setări proprii: `F4_FETCH_TIMEOUT_S` (implicit 150), `F4_PROPERTY_TIMEOUT_S` (implicit 600).
- **Apify verificat live (14 noiembrie 2026)** cu `tri_angle/airbnb-reviews-scraper`: 50 de recenzii per anunț în ~5 s, 3 proprietăți în paralel în ~7 s; a doua rulare nu creează snapshot-uri noi; proprietatea nelegată e sărită cu avertisment.
- Anunțurile reale stau în `listings.json` (nume proprietate demo → URL). `python -m app.features.f4_review_analysis.link_listings` (API pornit) creează legăturile prin `PUT /integrations/mappings`; e idempotent și trebuie rulat din nou după `manage.py seed` / reset.
  - Old Town Studio → `rooms/33615780` (Centrul Vechi, 1 cameră, 4.94★)
  - Calea Victoriei Apartment → `rooms/36172401` (pe Calea Victoriei, balcon, AC, 4.96★)
  - Herastrau Family Flat → `rooms/1063624740877318380` (Aviației, parcare, 4.98★)
  - Floreasca Garden Studio → `rooms/1538655218312297751`, Dorobanti City Apartment → `rooms/1572459639428350279`, Aviatiei Family Apartment → `rooms/1330605373645952305` (adăugate la 14 noiembrie 2026, când lumea demo a trecut la șase proprietăți în București; linkurile răspund, dar recenziile lor nu au fost încă preluate prin Apify).
  - Aceleași șase linkuri sunt și în `seed/world.json` (`listing_url`), de unde le folosesc oferta de relocare și copilotul.
- **După auditul de integrare (14 noiembrie 2026)** (`tests/test_integration_guards.py`):
  - la host ajunge doar ce a trecut prin `add_recommendation`: fără recomandări înregistrate, textul analizei e `NOTHING_TO_FIX` (scris în cod), iar dovezile salvate sunt doar citatele verificate; `result` și `evidence` din `finish` nu se mai salvează;
  - o singură analiză per proprietate la un moment dat (lock per proprietate): două cereri simultane rulează agentul o dată, a doua primește „No new reviews”;
  - lotul de recenzii trebuie să fie al proprietății cerute (`checked_batch`): altă proprietate sau recenzii amestecate = eroare a sursei, deci fallback pe ultimul snapshot; recenziile cu același ID se numără o dată;
  - `theme` și `action` se curăță de spații înainte de validarea lungimii; fragmentele unui citat cu „...” trebuie să apară în ordinea dată;
  - adaptorul Apify ignoră itemii altui anunț (`startUrl`), recenziile repetate și ratingurile booleene;
  - pagina F4 și simulatorul spun „still running” când API-ul întoarce `completed=false`, nu „finished”.
- **Pasul 5 (autentificare pe API) e implementat** în nucleu: `API_TOKEN` (vezi `docs/ARCHITECTURE.md`, decizia 36, și `tests/test_api_auth.py`).

## Decizii
- Textul liber al modelului nu e sursă de recomandări — garanția „citatele există în recenzii” nu poate fi ocolită sărind peste unealtă.
- Recenziile sunt citite de handler și puse la dispoziția agentului prin unelte țintite — modelul nu primește toate recenziile în prompt, iar cifrele vin din cod.
- Snapshot doar când recenziile s-au schimbat — §4 cere snapshot cu sursă și dată; un snapshot identic nu adaugă informație, iar fallback-ul folosește oricum ultimul.
- Fără aprobare — nu există acțiune cu impact, doar recomandări.
- Recomandările structurate stau într-o tabelă nouă (`f4_recommendations`), nu în coloane noi pe `f4_insights` — `CREATE TABLE IF NOT EXISTS` o adaugă și bazelor de date existente, fără migrare și fără `manage.py seed`.
- Analiza tuturor proprietăților prin câte un eveniment privat per proprietate (nu un timeout mare pe un singur handler) — fiecare proprietate are timeoutul și izolarea ei, iar rulările merg în paralel.
- Comparația se calculează la citire (`list_insights`), nu se stochează — fără coloană nouă, iar rezultatul e mereu consecvent cu rândurile salvate.
- La recenzii neschimbate cu analiză existentă, agentul nu mai rulează — răspunsul ar fi același, iar un model real costă.
- La fallback (sursă căzută), agentul rulează din nou pe ultimul snapshot — comportament păstrat din schelet (și din testul existent); analiza e marcată ca avertisment.
- Actorul Apify ales: `tri_angle/airbnb-reviews-scraper` (adaptorul transformă `/` în `~` pentru URL-ul API-ului). Șablon: `{"startUrls": [{"url": "{listing_url}"}], "maxReviewsPerListing": 50, "sortBy": "most-recent", "locale": "en-US"}` — 50 de recenzii țin costul mic (~$0.12/anunț/rulare) și contextul agentului sub control.
- Adaptorul preferă `localizedText` (traducerea în limba din `locale`) față de `text` (originalul) — recenziile reale sunt în multe limbi (ro, de, it, he, pt...); într-o singură limbă temele și citatele sunt consecvente, iar UI-ul e în engleză. Textul vine cu HTML (`<br/>`, entități) și e curățat în adaptor (`clean_text`). Formele reale sunt fixate în `tests/test_apify_tri_angle.py`.
- Anunțurile reale stau într-un fișier de date al F4 (`listings.json`) plus un script care folosește API-ul REST — corespondențele sunt în `id_map` și se pierd la seed/reset; așa se refac dintr-o comandă, fără să atingem nucleul sau seed-ul comun.
- Politica mock dă toate `add_recommendation` într-un singur mesaj — rundele rămân puține, deci fereastra glisantă (6 runde) nu ascunde rezultatele de care are nevoie politica.

## Plan faza 1

**Zona permisă:** `app/features/f4_review_analysis/`, `app/adapters/apify/`. Autentificarea pe API (pasul 5) atinge nucleul și se face ca PR separat.

**De implementat, în ordine:**

1. [x] **Recomandări structurate.** Agentul primește o unealtă `add_recommendation(theme, priority, action, quotes)`; recomandările se salvează pe rânduri, iar UI-ul le sortează după prioritate.
2. [x] **Comparație cu analiza anterioară.** Notificarea spune ce temă e nouă, ce persistă și ce a dispărut față de ultima analiză a proprietății.
3. [x] **Snapshot-uri fără dubluri.** Dacă recenziile preluate sunt identice cu ultimul snapshot, nu se salvează unul nou.
4. [x] **Analiza tuturor proprietăților** fără risc de timeout: câte un eveniment per proprietate sau un timeout de handler potrivit.
5. [x] **Autentificare pe API** (în nucleu: `app/main.py`, `ui/api_client.py`, `extension/lib/api.ts`): un token opțional din config, cerut pe toate endpointurile în afară de `/health` și `/webhooks/*`; Streamlit și extensia îl trimit.

**Criterii de acceptare:**
- recomandările au temă și prioritate și apar sortate în pagina F4;
- a doua analiză pe aceleași recenzii nu creează un snapshot nou și spune că nu s-a schimbat nimic;
- eșecul sursei folosește în continuare ultimul snapshot și anunță hostul;
- cu tokenul setat, un request fără el primește `401`; fără token setat, totul merge ca acum.

**De verificat manual, cu cheie Apify:** alegerea actorului, șablonul de input (`APIFY_REVIEWS_INPUT_TEMPLATE`), maparea proprietății la un anunț real, normalizarea câmpurilor reale ale actorului.

## Reguli pentru implementare (one-shot)

Aceste reguli sunt pentru Claude Code. Scopul: implementarea să iasă bine din prima și merge-ul să nu strice munca celorlalți.

1. **Citește întâi**, în ordine: acest fișier, `CLAUDE.md` din rădăcină, `docs/ARCHITECTURE.md` §3 (rețeta de extindere) și codul existent al feature-ului.
2. **Lucrează doar în zona permisă** de mai sus. Nu edita alt feature, `app/core/`, `app/ports/`, `app/routers/`, `app/container.py` sau `app/main.py`.
3. **Dacă ai nevoie de o schimbare în nucleu sau în contractele comune, oprește-te și spune.** Ea se face într-un PR separat, mic, doar prin adăugare. Nu o strecura în PR-ul de feature și nu o ocoli duplicând cod.
4. **Fișierele comune** (`seed/world.json`, `requirements*.txt`, `.env.example`, `README.md`) primesc doar adăugări minime. Nu reformata și nu reordona ce există.
5. **Nu importa alt feature și nu importa adaptoare.** Comunicarea cu alt feature se face doar prin evenimente.
6. **Fiecare comportament nou are un test** în `tests/` al feature-ului, pe mock, fără rețea. Testele existente rămân verzi; nu le slăbi ca să treacă.
7. **Respectă garanțiile proiectului:** acțiunile cu impact trec prin aprobare, limitele de bani și regulile hostului se impun în cod, orice eșec de adaptor are fallback explicit și notificare, orice apel extern are timeout.
8. **Dacă schimbi fluxul unui agent**, actualizează și `mock_llm.py`, altfel demo-ul pe mock nu îl folosește.
9. **Texte vizibile în engleză**; comentarii și documentație în română; identificatori în engleză.
10. **Înainte să declari gata**, rulează toată suita: `python manage.py test`. Apoi parcurge fluxul în simulator (`python manage.py api` + `python manage.py ui`).
11. **Ce cere o cheie reală nu se poate verifica automat.** Implementează, testează cu transport simulat și listează explicit la final ce rămâne „de verificat manual”. Fără secrete în cod sau în git.
12. **La final, actualizează acest fișier** (Stare curentă, Decizii, Probleme deschise; bifează pașii din plan) și `README.md`-ul feature-ului. În rezumat spune ce ai făcut, ce nu ai putut verifica și orice abatere de la plan.

## Cerințe suplimentare ale echipei

<de completat de membrul care preia feature-ul, înainte de one-shot: ce vrem exact în plus sau altfel față de planul de mai sus>

## Probleme deschise / TODO
- **LLM real verificat o singură dată** (14 noiembrie 2026, `anthropic/claude-sonnet-5.5`, Old Town Studio, 50 de recenzii Apify): 3 pași, 4 recomandări cu citate valide (nicio respingere), prioritățile coerente, ~15 s, ~$0.03. Încă neverificate cu modelul real: comparația pe mai multe rulări (stabilitatea numelor de teme), scenariile de eroare, alte modele (§5: scriptul de evaluare).
- Pe recenziile reale politica mock dă rezultate slabe: potrivește cuvinte-cheie fără context (ex. „Noise canceling windows” → temă „Noise”). E doar pentru demo/teste fără cheie.
- Anunțurile reale primite au 4.84–5.0★; între ultimele 50 de recenzii sunt puține critici, deci „nothing to fix” e un rezultat așteptat. Pentru un demo cu recomandări consistente, un anunț cu mai multe recenzii de 3–4★ ar arăta mai bine.
- Cele trei anunțuri adăugate pentru proprietățile noi nu au fost rulate prin Apify: de verificat că actorul întoarce recenzii pentru ele.
- Apify întoarce recenzii fără `localizedText` pentru limbile pe care Airbnb nu le traduce; atunci rămâne originalul.
- Snapshot-urile vechi nu se curăță (acum cresc doar când recenziile se schimbă).
- Comparația temelor depinde de nume: un model real care numește altfel aceeași problemă („Internet” vs „Wi-Fi”) o raportează ca nouă + dispărută. Atenuare: temele anterioare sunt date în obiectiv. De verificat cu un model real.
- Analizele „all properties” pornesc în paralel; cu un model real și multe proprietăți pot apărea limite de rată la OpenRouter. Nu există limită de concurență.
- Recenziile Apify fără `id` primesc un ID din autor + text; dacă actorul întoarce texte diferite la fiecare rulare (ex. traduceri), „identic cu ultimul snapshot” nu mai e detectat.
- Politica mock grupează problemele după cuvinte-cheie în engleză.

## Note pentru Claude
- Numerele (medie, număr de recenzii) vin din `rating_stats`, nu din model.
- Nu trimite toate recenziile într-un singur rezultat de unealtă: păstrează limita și trunchierea din `get_reviews`.
- Eșecul adaptorului se tratează în feature, cu fallback explicit și notificare; nu-l lăsa să ajungă la event bus.
- Dacă adaugi un eveniment de ieșire (ex. „recomandări gata”), ține-l privat până îl consumă alt feature.
- Citatele din `add_recommendation` se verifică pe textul complet al recenziei, nu pe cel trunchiat din `get_reviews`.
- Dacă schimbi uneltele sau ordinea lor, actualizează `mock_llm.py` și lista de unelte așteptată în `test_reviews_become_recommendations_for_the_host`.
- Testele fazei 1 sunt în `tests/test_phase1.py`.
