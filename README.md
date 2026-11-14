# StayPilot AI

UiPath Future Forward Hackathon 2026

Team Members: Tudor Popanica, Mihaela Tudor, Andu Bădescu, Leu Petrișor, Luncă Diana

**StayPilot AI** este un copilot pentru hosturile care gestionează rezervări printr-un PMS. Ține hostul la curent și îi cere aprobarea pentru acțiunile importante.

Acest repo conține **scheletul**: aplicația rulează local, complet pe un mediu simulat, fără niciun API extern și fără nicio cheie. Cele cinci fluxuri (F1–F5) funcționează cap-coadă și pot fi urmărite în interfață.

- Ce construim și regulile proiectului: [`CLAUDE.md`](CLAUDE.md)
- Deciziile de arhitectură, abaterile și riscurile: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)
- Notițele fiecărui feature: `app/features/<feature>/CLAUDE.md` și `README.md`

## Pornire rapidă

Ai nevoie de **Python 3.11 sau mai nou**. Pentru extensia Chrome ai nevoie și de Node 20+.

Comenzile sunt aceleași pe Windows, macOS și Linux. Pe macOS și Linux scrie `python3` dacă `python` nu există.

```bash
git clone https://github.com/andrewthelion04/UIPath-Future-Forward-Hackathon-2026.git
cd UIPath-Future-Forward-Hackathon-2026

python manage.py setup     # o singură dată: creează .venv, instalează dependențele, creează .env
python manage.py api       # terminalul 1: API-ul pe http://127.0.0.1:8000
python manage.py ui        # terminalul 2: simulatorul pe http://localhost:8501
```

La prima pornire, API-ul populează singur lumea simulată. Nu trebuie activat mediul virtual: `manage.py` rulează totul cu Python-ul din `.venv`.

## Comenzi

| Comandă | Ce face |
|---|---|
| `python manage.py setup` | Creează `.venv`, instalează `requirements-dev.txt`, copiază `.env.example` în `.env` |
| `python manage.py seed` | Șterge datele și reface lumea simulată din `seed/world.json` |
| `python manage.py api` | Pornește API-ul FastAPI cu auto-reload (documentația interactivă: `/docs`) |
| `python manage.py ui` | Pornește simulatorul Streamlit |
| `python manage.py test` | Rulează toate testele; argumentele în plus ajung la pytest, ex. `python manage.py test app/features/f2_incidents` |

`.venv` este local fiecărui laptop și nu se comite. Versiunile pachetelor sunt fixate în `requirements.txt` și `requirements-dev.txt`, ca toată echipa să ruleze același lucru. Dacă adaugi o dependență, trece-o acolo cu versiune exactă și rulează din nou `python manage.py setup`.

## Ce poți încerca în simulator

| Flux | În pagina Simulator | Ce urmărești |
|---|---|---|
| F1 + F3 | *Guest chat* → „Ask about check-in” | Răspunsul copilotului; pașii lui în *Agent timeline* |
| F3 + aprobare | *Guest chat* → „Request early check-in” | Cererea din *Approvals*; după aprobare, oaspetele primește confirmarea |
| F3 → F2 | *Guest chat* → „Report a problem” | Evenimentul `IssueReported` în *Events* și incidentul în *F2 · Incidents* |
| F2 | *Staff report* | Aprobarea cu opțiunile remediere / relocare |
| F2 + F3 | cu `F2_SIM_GUEST_ANSWER=off`: după oferta de relocare, *Guest chat* → Lukas întreabă de poze, apoi acceptă | Copilotul răspunde din datele ofertei (cu linkul anunțului) și transmite răspunsul; incidentul devine `relocated` |
| F4 | *Review analysis* | Recomandările din *Notifications* și din *F4 · Review Analysis* |
| F5 | *Cancel booking* → Emma Johnson | Prețul propus în *Approvals*; după aprobare, calendarul din *World data* |
| Reset | *Reset world* | Lumea revine la starea din seed |

Pagina **Agent dashboard** arată, din jurnalele reale ale rulării, ce a făcut agentul singur, ce a cerut hostului și motivul fiecărui pas: indicatorii, povestea fiecărui flux și jurnalul deciziilor. Scenariul de prezentare, pas cu pas, e în [`docs/DEMO.md`](docs/DEMO.md).

Pagina *F1 · Pms Integration* are un tester de webhook cu payload-uri de exemplu: mesaj de la oaspete, duplicat, ecoul propriului mesaj (filtrul anti-buclă), rezervare nemapată.

## Structura repo-ului

```text
app/
├── config.py          # toate comutatoarele mock <-> real + validarea la pornire
├── container.py       # singurul loc care cunoaște adaptoarele concrete
├── main.py            # aplicația FastAPI
├── seeding.py         # încărcarea și validarea seed-ului
├── core/              # nucleul comun: event bus, aprobări, notificări, IdMapper, SyncService,
│                      #   ChannelGateway, bucla agentului, contractele (evenimente, modele)
├── ports/             # interfețele dependențelor externe
├── adapters/          # mock/, hospitable/, ownerrez/, openrouter/, apify/, inside_airbnb/
├── routers/           # API-ul REST comun
└── features/          # f1_pms_integration/ ... f5_relisting/ (fiecare cu tot ce îi aparține)
ui/                    # simulatorul Streamlit (consumă doar API-ul REST)
extension/             # extensia Chrome pentru host (WXT + React + TypeScript)
seed/world.json        # lumea simulată
tests/                 # testele nucleului; testele unui feature stau în folderul lui
docs/ARCHITECTURE.md
```

## Comutarea mock ↔ real

Totul se comută din `.env`, fără nicio modificare în cod. Schema și valorile implicite sunt în [`app/config.py`](app/config.py); `.env.example` listează variabilele.

| Dependență | Variabilă | Valori | Ce mai trebuie setat pentru varianta reală |
|---|---|---|---|
| PMS (proprietăți, rezervări, mesaje, calendar) | `PMS_PROVIDER` | `mock`, `hospitable`, `ownerrez` | `HOSPITABLE_API_TOKEN`, respectiv `OWNERREZ_ACCESS_TOKEN` (sau `OWNERREZ_USERNAME` + `OWNERREZ_PERSONAL_TOKEN`) și `OWNERREZ_ENV=stage\|production` |
| LLM | `LLM_PROVIDER` | `mock`, `openrouter` | `OPENROUTER_API_KEY`, `LLM_MODEL`; opțional `LLM_MODEL_UTILITY` |
| Recenzii | `REVIEWS_PROVIDER` | `mock`, `apify` | `APIFY_TOKEN`, `APIFY_REVIEWS_ACTOR_ID`; opțional `APIFY_REVIEWS_INPUT_TEMPLATE` |
| Date de piață | `MARKET_PROVIDER` | `mock`, `inside_airbnb` | `INSIDE_AIRBNB_LISTINGS_CSV` (calea către `listings.csv` sau `listings.csv.gz`), `MARKET_CITY`, `MARKET_CURRENCY` |

- **Fail-fast:** dacă alegi un adaptor real și lipsește un secret, aplicația nu pornește și îți spune exact ce variabilă lipsește.
- **Conexiunea mock rămâne mereu activă** lângă cea reală, ca simulatorul să funcționeze și operațiile indisponibile în PMS-ul real să poată fi demonstrate pe mock.
- După ce alegi un PMS real, importă-i datele cu `POST /integrations/<provider>/sync` (sau butonul *Sync* din *World data*).
- Pentru Apify, anunțul demo e fictiv. Leagă proprietatea de URL-ul unui anunț real comparabil:

  ```bash
  curl -X PUT http://127.0.0.1:8000/integrations/mappings -H "Content-Type: application/json" \
    -d '{"connection": "airbnb", "entity_type": "listing", "external_id": "https://www.airbnb.com/rooms/<id>", "internal_id": "<prop_id>"}'
  ```

- Setările proprii ale unui feature au prefixul lui, ex. `F5_UNDERCUT_PCT=3`, `F4_FETCH_TIMEOUT_S=150`. Sunt descrise în README-ul feature-ului.

> **Adaptoarele reale sunt o primă versiune, netestată cu chei reale.** Ce e confirmat din documentație și ce nu este scris în antetul fiecărui adaptor și în `docs/ARCHITECTURE.md`.

## Webhook-uri și tunel

PMS-ul trimite webhook-urile către un URL public HTTPS. Local, deschide un tunel către API:

```bash
ngrok http 8000                                       # sau:
cloudflared tunnel --url http://127.0.0.1:8000
```

Configurează în PMS adresa `https://<adresa-tunelului>/webhooks/<provider>`, de exemplu `/webhooks/hospitable` sau `/webhooks/ownerrez`.

F1 oferă și Basic Auth OwnerRez (`F1_OWNERREZ_WEBHOOK_USERNAME` + `F1_OWNERREZ_WEBHOOK_PASSWORD`) și filtrare CIDR opțională Hospitable (`F1_HOSPITABLE_WEBHOOK_ALLOW_CIDRS`). Configurarea, reprocesarea mesajelor nemapate și limitele calendarului sunt descrise în [README-ul F1](app/features/f1_pms_integration/README.md).

- Endpointul răspunde mereu `200` și procesează în background. Payload-urile brute se păstrează și se văd în pagina *F1* sau la `GET /f1/webhook-log`.
- **Atenție:** tunelul expune tot API-ul. Înainte să-l deschizi, setează în `.env`:
  - `API_TOKEN` — orice request cere atunci `Authorization: Bearer <valoarea>`, în afară de `/health` și `/webhooks/*`. Streamlit îl citește din același `.env`; extensia îl primește la build (`WXT_API_TOKEN=<valoarea> npm run build`). Fără el, oricine are adresa tunelului poate aproba o cerere.
  - `WEBHOOK_TOKEN` — webhook-urile trebuie să vină atunci la `/webhooks/<provider>?token=<valoarea>`.

## Extensia Chrome

```bash
cd extension
npm ci            # instalează exact versiunile din package-lock.json
npm run dev       # deschide Chrome cu extensia încărcată și reîncărcare automată
npm run build     # build de producție în extension/.output/chrome-mv3
```

Pentru build-ul de producție: `chrome://extensions` → *Developer mode* → *Load unpacked* → alege `extension/.output/chrome-mv3`. Clic pe iconița extensiei deschide panoul lateral cu aprobările, notificările și proprietățile. API-ul trebuie să ruleze pe `http://127.0.0.1:8000`; altă adresă se dă la build prin `WXT_API_URL` (permisiunile din manifest o urmează automat), iar tokenul API-ului, dacă e setat, prin `WXT_API_TOKEN`. Tokenul ajunge în bundle, deci un build făcut cu el nu se distribuie.

## Teste

```bash
python manage.py test                       # tot
python manage.py test tests                 # doar nucleul
python manage.py test app/features/f3_guest_copilot
```

Testele rulează pe mock, cu o bază de date temporară, fără rețea și fără `.env`. Acoperă garanțiile din `CLAUDE.md` §6: izolarea handlerilor, filtrul anti-buclă și deduplicarea din F1, maparea ID-urilor, idempotența aprobărilor și câte un test cap-coadă pentru fiecare flux. `tests/test_architecture.py` verifică automat că un feature nu importă alt feature și nu importă adaptoare concrete.

## Lucrul în paralel

- Fiecare lucrează în `app/features/<feature>/`: cod, `schema.sql`, prompturi, teste, pagina de UI, `README.md` și `CLAUDE.md`.
- Un feature nou sau o pagină nouă se descoperă automat. Nu se editează niciun fișier central.
- Feature-urile comunică doar prin evenimente. Contractele comune din `app/core/contracts/` se modifică doar prin adăugare, în PR-uri mici.
- Când preiei un feature, trece-ți numele în `CLAUDE.md`-ul lui.

Rețeta completă pentru extinderea unui feature este în [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).
