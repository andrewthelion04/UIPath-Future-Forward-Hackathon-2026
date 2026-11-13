# Arhitectura StayPilot AI

Acest document descrie deciziile luate la construirea scheletului, abaterile de la `CLAUDE.md` și ce rămâne de verificat. Regulile proiectului sunt în `CLAUDE.md`; aici e doar felul în care au fost puse în practică.

## 1. Imaginea de ansamblu

```text
 Intrări                      Nucleu (app/core)                         Ieșiri
 ───────                      ─────────────────                         ──────
 webhook PMS ──┐
 simulator ────┼─► eveniment ─► EventBus ─► handlerii feature-urilor ─► porturi ─► adaptoare
 UI / extensie ┘                  │              (izolați)                │        (mock | real)
                                  │                                        │
                                  └─► aprobări + notificări (comune) ◄─────┘
```

- **Hexagonal.** Feature-urile depind de porturi (`app/ports`) și de serviciile nucleului. Adaptoarele concrete sunt cunoscute doar de `app/container.py`, care le alege din config.
- **Event-driven.** Intrările publică un eveniment; feature-urile se abonează. Un feature nu importă alt feature.
- **Un singur proces.** FastAPI, event bus în proces, SQLite local.

## 2. Decizii

Fiecare decizie are motivul ei. Cele marcate **(abatere)** se depărtează de litera `CLAUDE.md`.

### Structură și lucru în paralel

1. **Feature-urile stau în `app/features/<feature>/` și se descoperă automat.** `feature_loader` încarcă orice subpachet care are `feature.py` cu `register(ctx)`. Tabelele vin din `schema.sql`-ul feature-ului, pagina de UI din `ui_page.py`, testele din `tests/`. Niciun fișier central nu se editează la adăugarea unui feature.
2. **Înregistrarea unui feature e atomică.** Abonările, routerele și politicile mock cerute în `register(ctx)` se aplică doar dacă funcția se termină fără eroare. Un feature care cade la încărcare nu lasă nimic pe jumătate; e raportat la `/health` și hostul e notificat.
3. **Setările proprii ale unui feature stau în feature**, cu prefix (`F4_`, `F5_`), prin `ctx.load_settings(...)`. Așa nimeni nu editează `app/config.py` pentru o setare de feature.
4. **`tests/test_architecture.py` apără regulile**: analizează importurile și pică dacă un feature importă alt feature, un adaptor sau stratul de API, dacă nucleul importă feature-uri, sau dacă o pagină de UI importă cod de backend.

### Config

5. **(abatere, formă) Comutatoarele au schema în `app/config.py` și valorile în `.env`.** `CLAUDE.md` cere „un singur fișier de config”. Schema, valorile implicite și validarea sunt într-un singur fișier; valorile concrete stau în `.env`, pentru că acolo sunt și secretele și pentru că un fișier de comutatoare comis ar fi modificat local de fiecare dintre cei cinci și ar produce conflicte.
6. **Fail-fast cu listă completă.** Validarea adună toate secretele lipsă și oprește pornirea cu un singur mesaj.
7. **(abatere) Calendarul nu are comutator separat.** Calendarul și prețurile aparțin PMS-ului, deci `CalendarGateway` rutează după conexiunea PMS a proprietății, la fel cum `ChannelGateway` rutează mesajele după conexiunea rezervării. Un comutator global ar fi trimis proprietăți reale către calendarul mock.
8. **(abatere) Conexiunea mock rămâne mereu înregistrată** lângă cea reală aleasă prin `PMS_PROVIDER`. Simulatorul continuă să funcționeze, iar operațiile indisponibile în PMS-ul real se pot demonstra pe mock, cum cere §4.

### Evenimente

9. **Event bus în proces, cu handleri izolați.** `publish()` persistă evenimentul și pornește câte un task per handler, fără să aștepte. Fiecare handler are `wait_for(timeout)` și `try/except`; eroarea sau timeoutul se scriu în `event_handler_runs` și ajung la host ca notificare.
10. **Corelarea se propagă singură.** Evenimentele publicate dintr-un handler moștenesc `correlation_id`. Aprobarea îl păstrează, deci decizia continuă același flux: de la declanșator până la acțiunea finală există un singur ID de urmărit.
11. **Contracte comune puține.** În `app/core/contracts/events.py` sunt doar evenimentele care trec granița dintre componente. Un eveniment folosit de un singur feature stă în folderul lui (ex. `IncidentResolved` în F2) și se mută în contracte când îl consumă altcineva.
12. **`BookingCancelled` e emis de `SyncService`**, când observă că o rezervare cunoscută a trecut în `cancelled`. Simulatorul anulează rezervarea în PMS-ul mock și apoi sincronizează, deci anularea simulată și cea reală produc același eveniment pe același drum.
13. **`?wait=true` pe endpointurile simulatorului și pe decizie.** Implicit răspund imediat, ca un webhook. Cu `wait=true` răspunsul vine după ce handlerii au terminat; îl folosesc UI-ul și testele.

### Transport și mapare

14. **(abatere, plasare) `ChannelGateway` stă în nucleu, nu în F1.** Îl folosesc toate feature-urile, iar un feature nu poate importa alt feature. F1 deține intrarea: webhook-urile, pipeline-ul și jurnalul lor.
15. **`POST /sim/guest-message` e servit de F1, iar `GET /incidents` de F2**, pe căile din tabelul comun. Mesajul simulat trebuie să treacă prin același pipeline ca un webhook, iar incidentele sunt datele lui F2.
16. **ID-urile externe apar în patru locuri**: adaptoare, `IdMapper`, `SyncService` și pipeline-ul F1. Porturile apelate de feature-uri primesc ID-uri interne, iar adaptorul le traduce. Modelele de domeniu și evenimentele nu conțin ID-uri externe.
17. **ID-urile interne sunt deterministe**, derivate din (conexiune, tip, ID extern). Un reset al lumii păstrează aceleași ID-uri, deci linkurile și testele rămân stabile.
18. **Seed-ul parcurge drumul unui PMS real.** Lumea mock se încarcă în tabelele `mock_*`, apoi `SyncService.sync("mock")` creează proprietățile, rezervările și corespondențele. Lumea mock stă în SQLite, nu în memorie, ca modificările să supraviețuiască repornirii API-ului.

### Human-in-the-loop

19. **Aprobări idempotente la două niveluri.** `request()` nu dublează o cerere cu același `dedupe_key`. `decide()` face tranziția cu `UPDATE … WHERE status = 'pending'`: aceeași decizie repetată întoarce rezultatul existent fără să republice evenimentul, iar o decizie contrară primește `409`.
20. **Decizia ajunge doar la feature-ul solicitant**, prin `ctx.on_decision(kind, handler)`, care filtrează `ApprovalDecided` după feature și tip.

### Agent și LLM

21. **O singură buclă, în `app/core/agent/loop.py`.** Feature-urile dau doar promptul și uneltele (`AgentSpec`). `finish(result, evidence)` e adăugat de buclă și e singura cale de încheiere.
22. **`reason` e impus în cod.** Orice model de parametri moștenește `ToolArgs`; o unealtă definită fără el e respinsă la construcție, iar un apel fără `reason` se întoarce modelului ca eroare.
23. **Erorile se întorc modelului**: JSON invalid, unealtă necunoscută, argumente invalide, excepții din unealtă. Doar eșecul apelului la model încheie rularea.
24. **Fereastra de context lucrează pe runde.** O rundă este mesajul asistentului plus rezultatele uneltelor lui. Rundele vechi devin o notă atașată obiectivului; un `tool_call` nu rămâne niciodată fără răspuns.
25. **LLM-ul mock primește politica de la feature.** Fiecare agent își înregistrează o funcție deterministă `ChatRequest → ChatMessage` prin `ctx.register_mock_llm_policy`. Adaptorul mock nu conține logică de feature, iar pe un model real înregistrarea nu are efect.
26. **OpenRouter prin SDK-ul `openai`.** `reasoning_details` se păstrează pe mesaj și se retrimite nemodificat; costul se citește din `usage.cost`.
27. **Prompturile și textele vizibile sunt în engleză**, la decizia echipei: UI, notificări, aprobări, date demo. Documentația și comentariile rămân în română.

### Interfață

28. **Streamlit și extensia consumă același API REST**, cu aceleași modele de răspuns. Paginile feature-urilor stau în folderul feature-ului și sunt descoperite cu un glob.
29. **`manage.py` folosește doar biblioteca standard** și rulează comenzile cu Python-ul din `.venv`. Aceeași comandă merge pe Windows și pe macOS, fără activarea mediului virtual.

## 3. Rețetă: cum extinzi un feature

1. Citește `CLAUDE.md`-ul feature-ului.
2. Lucrează doar în `app/features/<feature>/`.
3. **Asculți un eveniment:** `ctx.subscribe(Eveniment, handler)` în `register(ctx)`.
4. **Emiți un eveniment:** `await ctx.bus.publish(...)`. Dacă e nou și îl folosești doar tu, definește-l în feature.
5. **Ai nevoie de aprobarea hostului:** `await ctx.approvals.request(...)`, apoi `ctx.on_decision(kind, handler)`.
6. **Anunți hostul:** `ctx.notify(title, body, level=...)`.
7. **Trimiți un mesaj oaspetelui:** `await ctx.channel.send_to_guest(booking_id, text, feature=ctx.feature)`.
8. **Ai nevoie de un agent:** definește `AgentSpec` (prompt + unelte) și apelează `await ctx.agent.run(spec, objective=..., state=...)`. Adaugă și o politică mock.
9. **Tabele noi:** în `schema.sql`, cu prefixul feature-ului și `CREATE TABLE IF NOT EXISTS`. Dacă schimbi o tabelă existentă, rulează `python manage.py seed`.
10. **Endpointuri noi:** un `APIRouter` dat lui `ctx.include_router(...)`.
11. **Pagină de UI:** `ui_page.py`, care folosește doar `ui.api_client` și `ui.components`.
12. **Teste:** în `tests/`, cu fixture-urile din `conftest.py` (`client`, `container`, `app_client`).

Dacă ai nevoie de ceva ce nucleul nu oferă sau de o schimbare într-un contract comun, fă un PR mic, separat, doar cu adăugarea.

## 4. Cine deține ce date

| Tabele | Deținător |
|---|---|
| `hosts`, `properties`, `bookings`, `staff`, `id_map`, `messages` | nucleu |
| `events`, `event_handler_runs`, `approvals`, `notifications`, `agent_runs`, `agent_steps` | nucleu |
| `mock_*` | adaptoarele mock |
| `market_listings` | adaptorul Inside Airbnb |
| `f1_webhook_log`, `f1_seen_messages` | F1 |
| `f2_incidents` | F2 |
| `f4_review_snapshots`, `f4_insights` | F4 |
| `f5_relist_proposals` | F5 |

F3 nu are tabele proprii: folosește `messages` și `agent_runs`.

## 5. Adaptoarele reale: ce e confirmat și ce nu

Toate sunt o primă versiune, testată doar cu transport HTTP simulat (`tests/test_real_adapters.py`). **Niciunul nu a fost rulat cu o cheie reală.**

| Adaptor | Implementat | Confirmat din | Neconfirmat / stub |
|---|---|---|---|
| OpenRouter | chat cu unelte, `reasoning_details`, cost, două modele | documentația OpenRouter, citită în noiembrie 2026 | antetele `HTTP-Referer` / `X-Title` nu au fost găsite în paginile citite; trimitem doar `X-Title` |
| Hospitable | listări, trimitere mesaj, parser de webhook | `CLAUDE.md` §4 (mesaje, webhook) | **forma răspunsurilor de la `/properties` și `/reservations` e scrisă din memorie**: documentația nu a putut fi citită automat. Calendarul e stub. |
| OwnerRez | listări cu paginare, mesaje prin thread, webhook, preț prin `spotrates` | documentația OwnerRez API v2, citită în noiembrie 2026 | numele câmpului `id`, cheia `items` la proprietăți, `GET /v2/bookings/{id}`, valorile lui `from_role`; citirea calendarului și scrierea disponibilității sunt stub |
| Apify | rulare sincronă a actorului, normalizare permisivă | `CLAUDE.md` §4 | forma outputului depinde de actorul ales |
| Inside Airbnb | import `listings.csv(.gz)`, căutare de comparabile | structura publică a fișierului | numele facilităților sunt text liber; maparea la vocabularul nostru e parțială |

Diferențe față de `CLAUDE.md` §4, de știut:

- **Apify:** tokenul se trimite în antetul `Authorization`, nu ca `?token=` în URL, ca să nu ajungă în loguri.
- **OwnerRez:** mesageria și webhook-urile cer token OAuth. Cu personal access token API-ul răspunde `402`, deci adaptorul își dezactivează singur capabilitatea de mesaje.
- **OwnerRez:** scrierea prețului are un endpoint documentat (`PATCH /v2/spotrates`), deci e implementată. Scrierea disponibilității nu are unul clar și rămâne stub.
- **OpenRouter:** costul vine automat în `usage.cost`; parametrul `usage: {include: true}` este depășit.

## 6. Riscuri și lucruri de verificat

1. **Tunelul expune tot API-ul, iar API-ul nu are autentificare.** Oricine are adresa tunelului poate aproba o cerere. `WEBHOOK_TOKEN` protejează doar webhook-urile. Înainte de orice folosire în afara laptopului trebuie autentificare pe API.
2. **Evenimentele în curs se pierd la restart.** Bus-ul e în proces. Evenimentele rămân în tabela `events`, dar nu există reluare automată. API-ul rulează cu un singur worker.
3. **Payload-ul webhook Hospitable e neconfirmat.** Parserul e permisiv și F1 păstrează payload-urile brute; ajustați `parse_webhook` după primul webhook real.
4. **Relocarea din F2 nu mută rezervarea în PMS.** Blochează calendarul proprietății țintă, anunță oaspetele și spune hostului că mutarea rămâne de făcut manual.
5. **Calendarul unui PMS real nu se poate citi încă.** F2 nu va găsi proprietăți libere pe o conexiune reală, deci va oferi doar remedierea. F3 primește eroarea ca rezultat de unealtă.
6. **Două mesaje rapide pe aceeași rezervare pornesc două rulări de agent în paralel.** Nu există coadă per conversație.
7. **Politica mock din F3 e pe cuvinte-cheie în engleză.** E suficientă pentru demo, nu pentru a judeca promptul. Promptul se evaluează doar cu un model real.
8. **Orașul de piață.** Inside Airbnb nu acoperă România, deci cu date reale de piață proprietățile demo din București se compară cu orașul configurat.
9. **Extensia Chrome a fost construită și verificată la tipuri, dar nu a fost încărcată în Chrome** în cadrul acestei sarcini.
10. **Scriptul de evaluare a modelelor** din `CLAUDE.md` §5 nu face parte din schelet.
