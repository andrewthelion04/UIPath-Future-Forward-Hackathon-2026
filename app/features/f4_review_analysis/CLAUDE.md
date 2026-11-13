# F4 — Analiza recenziilor

## Scop
La cererea hostului, preia recenziile unei proprietăți din sursa configurată (mock sau Apify), le salvează ca snapshot cu sursă și dată, apoi un agent le analizează și propune îmbunătățiri concrete. Recomandările ajung la host ca notificare. Dacă sursa de recenzii cade, folosește ultimul snapshot salvat și îi spune hostului.

## Contract
- Ascultă: `ReviewAnalysisRequested` (`property_id=None` înseamnă toate proprietățile).
- Emite: niciun eveniment propriu; rezultatul ajunge la host ca notificare.
- Date deținute: `f4_review_snapshots`, `f4_insights`; promptul din `prompts/system.md`.
- Porturi folosite: `reviews`, `llm` (prin `ctx.agent`).
- Cereri de aprobare: niciuna; recomandările sunt informative.

## Stare curentă
- Cerere → `reviews.fetch_reviews` cu timeout → snapshot → agentul `f4_review_analysis.recommend` → `f4_insights` → notificare.
- Uneltele agentului: `get_rating_stats` (număr, medie, distribuție, calculate în cod) și `get_reviews` (cu filtru `max_rating` și `limit`).
- Eșec sau timeout al sursei: se folosește ultimul snapshot, iar notificarea e marcată ca avertisment. Fără snapshot: notificare „skipped”.
- O proprietate care eșuează nu le oprește pe celelalte.
- `GET /f4/insights` și pagina de UI arată recomandările și citatele pe care se bazează.
- Setare proprie: `F4_FETCH_TIMEOUT_S` (implicit 150).

## Decizii
- Recenziile sunt citite de handler și puse la dispoziția agentului prin unelte țintite — modelul nu primește toate recenziile în prompt, iar cifrele vin din cod.
- Snapshot la fiecare preluare reușită — cerut de `CLAUDE.md` §4 și necesar pentru fallback.
- Fără aprobare — nu există acțiune cu impact, doar recomandări.

## Plan faza 1

**Zona permisă:** `app/features/f4_review_analysis/`, `app/adapters/apify/`. Autentificarea pe API (pasul 5) atinge nucleul și se face ca PR separat.

**De implementat, în ordine:**

1. [ ] **Recomandări structurate.** Agentul primește o unealtă `add_recommendation(theme, priority, action, quotes)`; recomandările se salvează pe rânduri, iar UI-ul le sortează după prioritate.
2. [ ] **Comparație cu analiza anterioară.** Notificarea spune ce temă e nouă, ce persistă și ce a dispărut față de ultima analiză a proprietății.
3. [ ] **Snapshot-uri fără dubluri.** Dacă recenziile preluate sunt identice cu ultimul snapshot, nu se salvează unul nou.
4. [ ] **Analiza tuturor proprietăților** fără risc de timeout: câte un eveniment per proprietate sau un timeout de handler potrivit.
5. [ ] **Autentificare pe API** (PR separat, în nucleu): un token opțional din config, cerut pe toate endpointurile în afară de `/health` și `/webhooks/*`; Streamlit și extensia îl trimit.

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
- Fiecare analiză salvează un snapshot nou, chiar dacă recenziile sunt aceleași; nu există curățare.
- Recomandările sunt text liber în `result`; nu au structură (temă, prioritate) pe care UI-ul să o poată sorta.
- Analiza pentru toate proprietățile rulează pe rând, într-un singur handler; cu multe proprietăți și un model real poate depăși timeoutul handlerului.
- Pentru Apify, proprietatea trebuie legată manual de URL-ul unui anunț real (`PUT /integrations/mappings`, conexiunea `airbnb`, tipul `listing`).
- Politica mock grupează problemele după cuvinte-cheie în engleză.
- Nu se compară analiza curentă cu cea anterioară (ce s-a îmbunătățit).

## Note pentru Claude
- Numerele (medie, număr de recenzii) vin din `rating_stats`, nu din model.
- Nu trimite toate recenziile într-un singur rezultat de unealtă: păstrează limita și trunchierea din `get_reviews`.
- Eșecul adaptorului se tratează în feature, cu fallback explicit și notificare; nu-l lăsa să ajungă la event bus.
- Dacă adaugi un eveniment de ieșire (ex. „recomandări gata”), ține-l privat până îl consumă alt feature.
