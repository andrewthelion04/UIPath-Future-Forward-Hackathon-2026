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

### Garanții adăugate după auditul de integrare (noiembrie 2026)

Problemele sunt descrise în `docs/INTEGRATION_AUDIT_2026-11-14.md`; aici sunt mecanismele comune care le rezolvă. Toate sunt adăugări: niciun contract existent nu și-a schimbat forma.

30. **O decizie fără eveniment nu rămâne salvată.** Dacă publicarea `ApprovalDecided` eșuează, `decide()` readuce aprobarea în `pending` și ridică eroarea, deci hostul (sau un retry) o poate aplica din nou. Înainte rămânea `approved` fără niciun efect.
31. **`ApprovalDecided` contează doar dacă aprobarea salvată spune același lucru.** Filtrul din `ApprovalService.on_decision` compară evenimentul cu rândul din `approvals` (feature, tip, stare, opțiune). Un eveniment construit în altă parte nu execută nicio acțiune, în niciun feature.
32. **Aprobări retrase (`withdrawn`).** `ctx.approvals.withdraw(id, reason=...)` scoate din inbox o aprobare încă nedecisă care nu mai descrie realitatea (F2: un raport nou după „reparat”; F5: propunere expirată). Nu publică eveniment; `decide()` pe ea întoarce `409`.
33. **Lock comun pe calendarul unei proprietăți: `ctx.calendar.lock(property_id)`.** Portul nu are tranzacții, deci orice secvență „citesc, verific, scriu” se ține sub acest lock. Îl folosesc relocarea din F2 și aplicarea prețurilor din F5; două fluxuri nu mai decid pe aceeași citire. Lockul e în proces, la fel ca event bus-ul; o rezervare apărută direct în PMS între citire și scriere rămâne posibilă.
34. **Munca de fundal a unui feature se declară cu `ctx.on_quiesce(hook)`.** Hook-ul e apelat înainte ca baza să fie închisă (`Container.shutdown`) sau recreată (resetul lumii). F1 își așteaptă reprocesările, F2 își oprește pașii simulați aflați în pauză.
35. **Resetul lumii oprește ce mai rulează.** După 10 s de așteptare, handlerii rămași sunt anulați (`EventBus.cancel_running`) înainte de ștergerea tabelelor, deci o analiză pornită în lumea veche nu mai scrie în cea nouă.
36. **Autentificare opțională pe API (`API_TOKEN`).** Când e setat, orice request cere `Authorization: Bearer <token>`, în afară de preflight-ul CORS, `/health` și `/webhooks/*` (care au `WEBHOOK_TOKEN` și verificările F1). Streamlit citește tokenul din același `.env`; extensia îl primește la build (`WXT_API_TOKEN`). Implicit e nesetat: demo-ul local merge fără niciun secret.
37. **Intrările verifică relațiile, nu doar existența.** O sesizare cu o rezervare de la altă proprietate primește `422`; o mapare manuală care ar lega o rezervare sau o proprietate de altă conexiune PMS decât a ei primește `409`, iar pipeline-ul F1 tratează o astfel de mapare ca „nemapat”.
38. **Un răspuns `200` fără date nu înseamnă „nimic”.** Paginarea OwnerRez ridică `PmsError` dacă răspunsul nu conține lista de elemente; altfel un calendar necitit ar arăta nopți libere. Două fixture-uri din `tests/test_real_adapters.py` care întorceau un obiect fără `items` au fost corectate la o pagină goală validă.

### Conversația cu oaspetele și dashboardul agentului (noiembrie 2026)

39. **Subiecte deschise cu oaspetele: `ctx.guest_topics`.** Copilotul (F3) vorbește cu oaspetele, dar incidentele sunt ale F2, iar un feature nu citește tabelele altuia. F2 publică de aceea în tabelul comun `guest_topics` starea pe care oaspetele are voie să o afle (`facts`) și, când e cazul, o ofertă (`GuestOffer`) cu detaliile ei. F3 le primește în `open_topics`, la fiecare mesaj, și răspunde din ele. Regula de conținut: în `facts` intră doar ce a aprobat deja hostul sau ce e pur informativ; o propunere încă nedecisă nu ajunge acolo.
40. **Răspunsul la o ofertă e un eveniment comun: `GuestOfferAnswered`.** Unealta `answer_offer` din F3 înregistrează răspunsul o singură dată (tranziție atomică în `guest_topics`), trimite o confirmare de primire scrisă în cod și publică evenimentul. Ajunge doar la feature-ul care a făcut oferta (`ctx.on_guest_offer_answered`). Rezultatul (mutarea, textul de confirmare) rămâne al acelui feature, cu textul aprobat de host. Agentul nu poate executa și nu poate confirma oferta singur.
41. **Conversația nu cere aprobare, deciziile da.** Răspunsurile informative pleacă direct; orice excepție, promisiune sau rezultat trece printr-o unealtă cu validare în cod și printr-o aprobare. Garda pe text liber din F3 blochează o formulare permisivă doar în aceeași propoziție cu subiectul unei excepții (oră, animale, persoane în plus, bani) sau, pentru formulările ferme, cu mutarea oaspetelui. Rămâne o euristică: protecția reală e structurală.
42. **`Property.listing_url`.** Anunțul public al proprietății, opțional, venit din PMS (din seed, pe mock). Îl folosesc oferta de relocare și copilotul când oaspetele cere poze. Coloana se adaugă și pe bazele existente (`Database.ensure_column`).
43. **Povestea unui flux se reconstituie, nu se stochează.** `app/core/flow_story.py` citește jurnalele nucleului (`events`, `event_handler_runs`, `agent_runs`, `agent_steps`, `approvals`, `notifications`, `messages`) și le leagă prin `correlation_id`; fluxurile legate cauzal (mesajul din chat care a acceptat oferta unui incident) se găsesc prin `causation_id`. Endpointurile `GET /agent/overview`, `/agent/flows`, `/agent/flows/{id}` și `/agent/decisions` alimentează pagina „Agent dashboard”. Nu există un jurnal separat de decizii: motivul fiecărui pas e `reason`-ul cerut de buclă, iar regulile aplicate de cod se văd ca apeluri respinse și ca rezumatul aprobării.
44. **Lumea demo are șase proprietăți, toate în București**, fiecare cu anunț real comparabil. Așa relocarea are mereu o a doua opțiune, iar fiecare țintă are un link de trimis oaspetelui.

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
   - **Scrii în calendar:** ține citirea, verificarea și scrierea sub `async with ctx.calendar.lock(property_id)`.
   - **Aprobarea ta nu mai e valabilă:** `ctx.approvals.withdraw(approval_id, reason=...)`, ca hostul să nu aprobe ceva depășit.
   - **Pornești sarcini `asyncio` proprii** (în afara event bus-ului): înregistrează `ctx.on_quiesce(hook)`, care le termină sau le oprește la shutdown și la reset.
   - **Faci ceva ce îl privește pe oaspete** (un incident, o ofertă): publică starea cu `ctx.guest_topics.publish(...)`, doar cu fapte pe care oaspetele are voie să le afle, și închide subiectul când s-a terminat. Dacă ai făcut o ofertă, primești răspunsul cu `ctx.on_guest_offer_answered(handler)`.
10. **Endpointuri noi:** un `APIRouter` dat lui `ctx.include_router(...)`.
11. **Pagină de UI:** `ui_page.py`, care folosește doar `ui.api_client` și `ui.components`.
12. **Teste:** în `tests/`, cu fixture-urile din `conftest.py` (`client`, `container`, `app_client`).

Dacă ai nevoie de ceva ce nucleul nu oferă sau de o schimbare într-un contract comun, fă un PR mic, separat, doar cu adăugarea.

## 4. Cine deține ce date

| Tabele | Deținător |
|---|---|
| `hosts`, `properties`, `bookings`, `staff`, `id_map`, `messages`, `guest_topics` | nucleu |
| `events`, `event_handler_runs`, `approvals`, `notifications`, `agent_runs`, `agent_steps` | nucleu |
| `mock_*` | adaptoarele mock |
| `market_listings` | adaptorul Inside Airbnb |
| `f1_webhook_log`, `f1_seen_messages` | F1 |
| `f2_incidents`, `f2_incident_reports`, `f2_staff_messages`, `f2_tasks` | F2 |
| `f4_review_snapshots`, `f4_insights`, `f4_recommendations` | F4 |
| `f5_relist_proposals` | F5 |

F3 nu are tabele proprii: folosește `messages` și `agent_runs`.

## 5. Adaptoarele reale: ce e confirmat și ce nu

Toate sunt testate cu transport HTTP simulat (`tests/test_real_adapters.py`). Cu chei reale au fost rulate până acum doar Apify (actorul `tri_angle/airbnb-reviews-scraper`) și OpenRouter, o singură dată fiecare, la 14 noiembrie 2026 (detalii în `CLAUDE.md`-ul F4). OwnerRez și Hospitable nu au fost rulate cu credențiale reale.

| Adaptor | Implementat | Confirmat din | Neconfirmat / stub |
|---|---|---|---|
| OpenRouter | chat cu unelte, `reasoning_details`, cost, două modele | documentația OpenRouter, citită în noiembrie 2026 | antetele `HTTP-Referer` / `X-Title` nu au fost găsite în paginile citite; trimitem doar `X-Title` |
| Hospitable | listări, trimitere mesaj, parser de webhook | `CLAUDE.md` §4 (mesaje, webhook) | **forma răspunsurilor de la `/properties` și `/reservations` e scrisă din memorie**: documentația nu a putut fi citită automat. Calendarul e stub. |
| OwnerRez | listări cu paginare, mesaje prin thread, webhook, preț prin `spotrates` | documentația OwnerRez API v2, citită în noiembrie 2026 | numele câmpului `id`, cheia `items` la proprietăți, `GET /v2/bookings/{id}`, valorile lui `from_role`. Citirea calendarului e implementată (`/v2/bookings` + `/v2/spotrates`), neverificată în Stage; scrierea disponibilității e dezactivată intenționat (API-ul nu poate șterge o blocare) |
| Apify | rulare sincronă a actorului, normalizare permisivă, itemii altui anunț și recenziile repetate sunt ignorate | `CLAUDE.md` §4 și rularea live din 14 noiembrie 2026 | forma outputului depinde de actorul ales |
| Inside Airbnb | import `listings.csv(.gz)`, căutare de comparabile | structura publică a fișierului | numele facilităților sunt text liber; maparea la vocabularul nostru e parțială |

Diferențe față de `CLAUDE.md` §4, de știut:

- **Apify:** tokenul se trimite în antetul `Authorization`, nu ca `?token=` în URL, ca să nu ajungă în loguri.
- **OwnerRez:** mesageria și webhook-urile cer token OAuth. Cu personal access token API-ul răspunde `402`, deci adaptorul își dezactivează singur capabilitatea de mesaje.
- **OwnerRez:** scrierea prețului are un endpoint documentat (`PATCH /v2/spotrates`), deci e implementată. Scrierea disponibilității nu are unul clar și rămâne stub.
- **OpenRouter:** costul vine automat în `usage.cost`; parametrul `usage: {include: true}` este depășit.

## 6. Riscuri și lucruri de verificat

1. **Fără `API_TOKEN`, API-ul e deschis.** E implicit așa, pentru demo-ul local. Înainte de a expune API-ul printr-un tunel, setează `API_TOKEN` (decizia 36): altfel oricine are adresa tunelului poate aproba o cerere. `WEBHOOK_TOKEN` protejează doar webhook-urile. Tokenul extensiei e inclus în bundle la build, deci extensia construită cu el nu se distribuie.
2. **Evenimentele în curs se pierd la restart.** Bus-ul e în proces. Evenimentele rămân în tabela `events`, dar nu există reluare automată. API-ul rulează cu un singur worker. Un handler oprit de timeout după ce hostul a aprobat lasă aprobarea consumată; hostul primește notificarea de eroare, iar F5 își consemnează progresul parțial.
3. **Payload-ul webhook Hospitable e neconfirmat.** Parserul e permisiv și F1 păstrează payload-urile brute; ajustați `parse_webhook` după primul webhook real.
4. **Relocarea din F2 nu mută rezervarea în PMS.** Blochează calendarul proprietății țintă, anunță oaspetele și spune hostului că mutarea rămâne de făcut manual. Copilotul răspunde cu noua adresă cât timp subiectul incidentului e deschis (până la check-out), dar rezervarea din PMS arată tot proprietatea veche.
5. **Copilotul știe despre un incident doar ce publică F2.** Întrebările la care `facts` nu răspund (compensații, altă proprietate, ora exactă a tehnicianului) sunt predate hostului. Mesajele neprovocate ale personalului și urmărirea activă („n-a răspuns nimeni de două ore”) nu există încă.
6. **Relocarea cere un calendar pe care îl putem bloca.** Pe OwnerRez (disponibilitate nescriabilă) și pe Hospitable (calendar stub) F2 oferă doar remedierea. F3 primește eroarea de calendar ca rezultat de unealtă.
7. **Garda F3 pentru text liber e euristică.** Confirmările tipizate trec prin aprobare, dar un model real poate formula în text liber o permisiune pe care regulile hostului o interzic. Politica mock din F3 e pe cuvinte-cheie în engleză: e suficientă pentru demo, nu pentru a judeca promptul.
8. **Orașul de piață.** Inside Airbnb nu acoperă România, deci cu date reale de piață proprietățile demo din București se compară cu orașul configurat.
9. **Extensia Chrome a fost construită și verificată la tipuri, dar nu a fost încărcată în Chrome** în cadrul acestor sarcini.
10. **Scriptul de evaluare a modelelor** există (`scripts/eval_models.py`), dar a fost rulat doar pe mock; alegerea modelului din `CLAUDE.md` §5 rămâne de făcut cu o cheie reală.
