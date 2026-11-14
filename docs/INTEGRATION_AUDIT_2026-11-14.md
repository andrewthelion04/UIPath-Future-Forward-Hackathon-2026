# Audit de integrare StayPilot AI — 14 noiembrie 2026

Versiune verificată: `4213071`. Auditul nu modifică sursele aplicației, extensia sau testele existente. Au fost adăugate numai scripturi de reproducere și acest raport cu rezultate JSON.

**Concluzie:** contractele de bază și fluxurile normale funcționează împreună, dar suita existentă nu acoperă toate conflictele de concurență, aprobările învechite și eșecurile parțiale. Nu se poate confirma funcționarea completă fără corectarea observațiilor de mai jos. Unele lipsuri sunt deja declarate în documentație și sunt marcate separat.

## Ce s-a executat

| Verificare | Rezultat |
|---|---|
| `python manage.py test` | **513 passed**, 48,61 s; include arhitectura și smoke-urile Streamlit |
| `npm ci`, în `extension/` | Instalare din lockfile; fără modificarea dependențelor |
| `npm run compile` și `npm run build` | Ambele trec |
| Toate combinațiile de feature-uri activate/dezactivate | **32/32**: API comun funcțional, încărcare corectă, fără rute duplicate |
| Cereri simultane cu toate feature-urile încărcate | **45 cereri HTTP**, 18 mesaje distincte, 4 analize F4, 1 incident, 1 propunere F5, fără erori de handler |
| Decizie repetată concurent | **12 cereri**, exact o tranziție aplicată |
| Probe suplimentare F2/F3 | **17 scenarii**; inclusiv 20 de sesizări concurente și eroare LLM la toate etapele F2 |
| Probe suplimentare F4 | **8 probe de logică + 1 AppTest**; include restart, cache, sursă căzută și răspunsuri LLM controlate |
| Probe suplimentare F5 | Politici schimbate, autoritatea aprobării, timeout parțial, concurență F2–F5 și tarif zero |
| Test generativ de preț | **20.000 de programe / 149.140 de nopți**, 9 contraexemple de rotunjire la zero; celelalte invariabile verificate țin |
| Probe extensie | Corpul exact `refresh` executat în Node cu răspunsuri din API-ul real de test; build separat cu URL API extern |
| Probe suplimentare nucleu/F1 | 11 probe, inclusiv reset după timeout, publicare eșuată și shutdown direct |
| API și Streamlit pornite efectiv, pe mock | HTTP 200; toate cele 5 feature-uri încărcate; incident remediat și închis, 4 analize F4 / 9 recomandări, relistare F5 aplicată; **18 handleri OK, 12 rulări terminate**, paginile F3/F4 fără excepții |

Cele 149.140 de nopți sunt verificări generative ale unor invariabile, nu 149.140 de teste pytest distincte. Testele suplimentare folosesc baze temporare și mock-uri/`MockTransport`; nu trimit mesaje prin PMS real și nu folosesc chei OpenRouter/Apify. Smoke-ul operațional este salvat în `docs/audit_results/live_local_smoke.json`; procesele pornite au fost oprite, porturile eliberate și baza temporară eliminată.

## Arhitectura verificată

Auto-discovery încarcă cinci module independente. Intrările F1 normalizează mesaje și sincronizează PMS-ul; F3 răspunde sau emite `IssueReported`, preluat de F2. Anulările observate de `SyncService` produc `BookingCancelled`, consumat de F5. F4 își distribuie analiza pe proprietăți. Toate folosesc același SQLite, `EventBus`, serviciu de aprobări și gateway-uri de calendar/mesaje.

Testele de import confirmă separarea modulelor. Conflictele descoperite apar mai ales la resursele comune: aceeași proprietate folosită de două incidente, calendarul folosit simultan de F2 și F5, stări modificate după emiterea unei aprobări și operații asincrone rămase active la reset/shutdown.

## Neconcordanțe confirmate

P1 = impact ridicat asupra autorizării, oaspeților sau integrității datelor; P2 = impact mediu; P3 = calitatea datelor/documentației. Intrările „politică” și „configurare” nu sunt prezentate ca încălcări certe ale unui criteriu explicit.

### Nucleu și F1

| ID | Nivel | Reproducere și rezultat | Cod |
|---|---|---|---|
| C01 | P1 | O eroare la publicarea `ApprovalDecided` apare după salvarea `approved`. Retry-ul întoarce `applied=False`; nu există eveniment și nu se execută confirmarea/acțiunea. Aprobarea poate rămâne definitiv fără efect, fără restart. | `app/core/approvals.py:172`, `:184` |
| C02 | P1 | La prima primire, publicarea `GuestMessageReceived` eșuează o dată. Webhook-ul răspunde `error`, retry-ul devine `duplicate`, iar reprocesarea nu preia rândurile `error`: mesajul nu mai ajunge la F3. | `app/features/f1_pms_integration/inbound.py:184`, `:216` |
| C03 | P2 | O eroare de publicare în reprocesarea `unmapped` lasă mesajul deja în conversație. Următoarea reprocesare creează încă o intrare inbound: două mesaje în conversație pentru un singur mesaj extern, deși evenimentul final este unul. | `inbound.py:220`, `:281` |
| C04 | P2 | API-ul acceptă maparea unei rezervări Hospitable la o rezervare internă `mock`. Webhook-ul Hospitable se publică cu `pms_provider=mock`, citește contextul acelei rezervări și răspunde prin mock, nu prin Hospitable. Necesită o mapare greșită; nu există verificarea acestei incompatibilități. | `inbound.py:197`, `:216`; `app/routers/integrations.py` |
| C05 | P1 | OwnerRez întoarce HTTP 200 cu un obiect fără `items` la `/v2/bookings`. Adaptorul îl tratează ca listă goală și întoarce toate nopțile `available=True`, în loc de disponibilitate necunoscută/eroare. | `app/adapters/ownerrez/adapter.py:153` |
| C06 | P1 | O analiză F4 durează 11 s. Resetul așteaptă 10 s, ignoră rezultatul `False` al `bus.join` și șterge datele. Analiza veche continuă și salvează un snapshot în lumea proaspăt resetată: zero snapshot-uri imediat după reset, unul după terminarea taskului vechi. | `app/container.py:269`; `app/seeding.py:98` |
| C07 | P2 | În utilizarea directă `Container`, `ids.link()` programează reprocesarea F1, apoi un shutdown imediat închide SQLite înaintea taskului. Rezultat: `ProgrammingError: Cannot operate on a closed database`. Lifespan-ul routerului drenează cazul web, dar `Container.shutdown()` nu acoperă consumatorii direcți. | `app/container.py:276`, `:285`; `app/features/f1_pms_integration/inbound.py:57` |

Dovezi: `docs/audit_results/core.json`. Scriptul include și cele două scenarii pozitive globale: toate combinațiile de feature-uri și cererile concurente.

### F2 și fluxul de aprobări

| ID | Nivel | Reproducere și rezultat | Cod |
|---|---|---|---|
| I01 | P1 | Două incidente, pentru Lukas și Emma, citesc simultan aceeași țintă liberă. Ambele ajung `relocated` în Herastrau pentru nopți suprapuse. Lockurile sunt per incident, fără protecție comună pentru țintă. | `app/features/f2_incidents/service.py:169`, `:987`, `:1015` |
| I02 | P1 | După ofertă, ținta este schimbată la capacitate 1, zero dormitoare și alt oraș. Acceptarea mută totuși doi oaspeți și trimite vechea adresă, deoarece se verifică doar disponibilitatea. | `service.py:807`, `:991` |
| I03 | P1 | `set_availability` eșuează; nopțile țintei rămân libere. F2 trimite totuși confirmarea și marchează `relocated`, cu o notificare `success` care cere blocare manuală. Este relevant și pentru OwnerRez, unde scrierea disponibilității este dezactivată. | `service.py:1014` |
| I04 | P1 | Trimiterea ofertei eșuează, dar starea trece la `awaiting_guest`, iar simularea acceptă automat. Confirmarea este trimisă deși oferta are status `failed`; hostul vede fals „Relocation offered”. | `service.py:879` |
| I05 | P1 | `/incidents/report` acceptă proprietatea Old Town împreună cu rezervarea lui Lukas de la Calea Victoriei. Produce incident și aprobare pentru combinația greșită; existența ID-urilor este verificată, relația lor nu. | `service.py:295`, `:398` |
| I06 | P1 | Pentru o opțiune unică, ambele inboxuri ascund `option.description`. La închiderea F2, textul exact către oaspete există doar acolo și nu apare în rezumat: hostul aprobă un mesaj pe care UI-ul nu îl arată. Confirmat prin AppTest cu aprobare reală. | `ui/views/approvals.py:36`; `extension/components/ApprovalsInbox.tsx:36`; `service.py:1107` |
| I07 | P1 | După reparație și emiterea aprobării de închidere, un raport nou spune că încă există scurgerea. Raportul se atașează, dar aprobarea veche nu se invalidează. Aprobarea ei închide incidentul și spune oaspetelui că problema e rezolvată. | `service.py:451`, `:1162` |
| I08 | P2 | Trei rânduri duplicate, cu aceeași zi și proprietate greșită, sunt numărate drept trei nopți libere. F2 oferă relocarea; verificarea cere numai numărul rândurilor și `all(available)`. | `service.py:828` |
| I09 | P2 | La expirarea termenului mentenanței, termenul este șters înaintea publicării `IncidentTimeout`. Dacă publicarea eșuează, retry-ul nu mai găsește termenul; incidentul rămâne `awaiting_staff`, fără deadline. | `service.py:370` |
| I10 | P2 | Mock-ul unește scurgerea de la chiuveta bucătăriei cu scurgerea dușului din baie: un incident și o aprobare pentru două probleme distincte. Aceeași categorie produce imediat potrivire, fără distingerea locației. Comportamentul unui LLM real nu este demonstrat de această probă. | `app/features/f2_incidents/mock_llm.py:38` |
| I11 | Politică | Herastrau are un incident deschis pentru mucegai, dar este oferit totuși ca țintă. Efectul este confirmat; CLAUDE definește compatibilitatea prin oraș/capacitate/dormitoare, fără politica de sănătate a țintei. Este necesară o decizie despre incidentele care exclud sau avertizează relocarea. | `service.py:807` |

Dovezi: `docs/audit_results/f2_f3.json`. Verificările pozitive din același fișier confirmă: 20 de rapoarte duplicate simultane → un incident și o aprobare; fallback F2 complet la eroare LLM; refuzul confirmării F3 după anulare; idempotența deciziei F2.

### F4 și sursele de recenzii

| ID | Nivel | Reproducere și rezultat | Cod |
|---|---|---|---|
| R01 | P1 | Un LLM sare peste `add_recommendation` și termină cu o recomandare și un citat inventate despre ploșnițe. F4 salvează textul și dovada, notifică succes și consideră snapshot-ul analizat, deși `items=[]` și recenziile vorbeau numai despre Wi-Fi. | `app/features/f4_review_analysis/analysis.py:392`, `:501`, `:516` |
| R02 | P1 | Analizele simultane pentru aceeași proprietate dublează rulările și notificările. În plus, analiza veche Wi-Fi terminată după analiza nouă Noise devine „ultima” în API, deși snapshot-ul curent și notificarea ulterioară indică Noise. | `analysis.py:358`, `:379`, `:403` |
| R03 | P1 | Sunt acceptate un batch pentru altă proprietate și recenzii individuale cu alt `property_id`. Adaptorul Apify cu `MockTransport` acceptă și un item `startUrl=B` la o cerere pentru A, etichetându-l drept A. | `analysis.py:453`, `:471`; `app/adapters/apify/reviews.py:164` |
| R04 | P2 | `theme="  "` și o acțiune numai din spații trec lungimile minime, apoi `.strip()` le transformă în șiruri goale acceptate. | `analysis.py:261`, `:263` |
| R05 | P2 | Citatul `dirty ... not` este acceptat pentru sursa `The room was not dirty.`. Fragmentele sunt căutate fără păstrarea ordinii, alterând sensul citatului. | `analysis.py:167` |
| R06 | P2 | UI-ul primește `completed=false` și afișează „Analysis finished”. Timpul de așteptare comun este 120 s, iar limita F4 per proprietate este 600 s, deci cazul poate apărea și cu servicii lente valide. | `app/features/f4_review_analysis/ui_page.py:26` |
| R07 | P3 | Apify `rating=True` devine 1 stea, afectând statisticile. | `app/adapters/apify/reviews.py:99` |
| R08 | P2 | Recenziile repetate cu același ID sunt păstrate de două ori; numărul, media și frecvența problemelor pot fi denaturate. | `reviews.py:164` |

Dovezi: `docs/audit_results/f4.json`, `f4_ui.json`. Restartul, reordonarea acelorași recenzii și fallback-ul după restart au trecut: un snapshot și o analiză rămân reutilizabile.

### F5 și regulile de preț

| ID | Nivel | Reproducere și rezultat | Cod |
|---|---|---|---|
| P01 | P1 | După propunere, limita hostului este schimbată din 25% în 0%. Aprobarea aplică totuși `65.96 / 65.96 / 75.85` peste tarifele obișnuite `70 / 70 / 80.5`; regula financiară curentă nu este recitită. | `app/features/f5_relisting/relisting.py:280` |
| P02 | P1 | Un `ApprovalDecided(approved)` intern, cu ID/payload valide dar aprobare persistentă încă `pending`, aplică prețurile. Este un control lipsă la granița dintre componente, **nu o exploatare demonstrată prin HTTP public**. | `relisting.py:282` |
| P03 | P1 | Timeoutul total anulează aplicarea după prima noapte. Calendarul e parțial modificat, aprobarea comună e `approved`, propunerea rămâne `awaiting_approval`, iar retry-ul aprobării nu reia nimic. Nu se înregistrează starea F5 `failed` și progresul parțial. | `relisting.py:318`; `app/core/event_bus.py:192` |
| P04 | P2 | Expirarea salvată la 15:00 rămâne valabilă după schimbarea orei de check-in la 11:00. La 11:30 în aceeași zi, aprobarea încă aplică prețurile. | `relisting.py:393` |
| P05 | P2 | Cu reducere maximă 100%, undercut valid 99,9%, tarif original pozitiv de 0,01 și bază 70, se propune și se scrie 0,00. Contrazice garanția documentată că o noapte pozitivă nu devine gratuită. Testul generativ a găsit 9 astfel de nopți. | `app/features/f5_relisting/pricing.py:99` |

Dovezi: `docs/audit_results/f5.json`, `f5_advanced.json`, `f5_zero_price.json`, `f5_pricing_stress.json`. Pragul, plafonul, precizia la cent și valorile finite au ținut în celelalte verificări generative.

### Extensie și configurare

| ID | Nivel | Reproducere și rezultat | Cod |
|---|---|---|---|
| U01 | P1 | Cu F2 dezactivat, `/incidents` dă 404, deși aprobările F3 și proprietățile răspund corect. `Promise.all` respinge întregul refresh și toate listele extensiei rămân goale. Funcția exactă din sursă a fost executată în Node cu fixture-uri din API. | `extension/entrypoints/sidepanel/App.tsx:31` |
| U02 | P2 | Un refresh vechi rezolvat după cel nou readuce în inbox o aprobare deja decisă. Lipsesc ordonarea sau anularea cererilor concurente. Serverul rămâne idempotent, dar UI-ul arată o stare învechită. | `App.tsx:29`, `:51` |
| U03 | P2 | `WXT_API_URL` cu `/` final produce `//properties`; API-ul real de test răspunde 404, în loc de 200 pentru `/properties`. Clientul Streamlit normalizează URL-ul, extensia nu. | `extension/lib/api.ts:5`, `:111` |
| U04 | Configurare | Build-ul cu un URL API HTTPS extern actualizează bundle-ul, dar manifestul păstrează numai permisiunile localhost:8000. Nealinirea este confirmată în build; **blocarea efectivă în Chrome nu a fost demonstrată**, deoarece CORS-ul backendului permite extensii. | `extension/wxt.config.ts:11` |

Dovezi: fișierele `docs/audit_results/extension_*.json`. Componentele au fost testate logic, iar extensia a fost compilată; nu s-a încărcat extensia într-un browser real în acest audit.

## Lipsuri și limite deja declarate

Acestea rămân relevante pentru afirmația „toate feature-urile sunt complete”, dar nu sunt prezentate drept regresii nou introduse.

| ID | Observație confirmată sau documentată |
|---|---|
| D01 | **Autentificarea API este încă neimplementată.** Pasul 5 din CLAUDE F4 este nebifat și declarat PR separat. Cu `WEBHOOK_TOKEN` setat, un apel fără credențiale poate totuși citi proprietăți și aproba o cerere: ambele HTTP 200. Tokenul existent protejează numai webhook-urile. |
| D02 | **Relocarea F2 nu actualizează rezervarea PMS/internă.** După confirmarea mutării la Herastrau, F3 răspunde cu adresa Calea Victoriei, până la mutarea manuală și sincronizare. Efectul este reprodus și limita există în TODO-ul F2. |
| D03 | **Acceptarea relocării prin chat nu ajunge la F2.** Cu răspunsul simulat dezactivat, „Yes, I accept the relocation” este tratat de F3 ca o întrebare necunoscută, iar incidentul rămâne `awaiting_guest`. Oferta îi spune totuși oaspetelui „Just reply to this message”. Legătura lipsă este declarată în TODO. |
| D04 | **F2 și F5 nu au coordonare comună asupra calendarului.** Am blocat efectiv noaptea prin relocarea F2 după citirea liberă făcută de F5. F5 a scris ulterior reducerea pe noaptea ocupată și a raportat `applied`. Documentația recunoaște lipsa CAS/tranzacției în port; aici efectul este confirmat și între două feature-uri locale. |
| D05 | **Capabilitățile PMS reale rămân parțiale.** Calendarul Hospitable și scrierea disponibilității OwnerRez sunt stub-uri declarate. Scrierea prețurilor într-un PMS real nu este validată de testele mock. Aceste fallback-uri nu echivalează cu toate fluxurile reale disponibile. |
| D06 | **Garda F3 pentru text liber este euristică.** Cu reguli `pets_allowed=false`, un LLM controlat a trimis `Your dog can join you during the stay.` fără aprobare. Confirmările tipizate au controale stricte, dar textul liber poate exprima aceeași permisiune prin altă formulare. Limita este declarată; confirmările canonice și refuzul sunt încă în engleză. Dovadă: `core.json`, `free_text_guard_paraphrase`. |
| D07 | **Documentația centrală este în urma implementării.** `docs/ARCHITECTURE.md:102` omite `f4_recommendations`; `:132` spune că nu se citește niciun calendar real; `:133` spune că F3 nu are coadă; `:137` spune că evaluatorul nu există. `:109` afirmă că niciun adaptor nu a fost verificat cu chei reale, în contradicție cu verificarea Apify consemnată în CLAUDE F4. |

## Stare după corecturi (14 noiembrie 2026)

Secțiunile de mai sus descriu aplicația **înainte** de corecturi și rămân ca istoric. Toate cele 34 de neconcordanțe confirmate sunt corectate, fiecare cu test de regresie; `python manage.py test` trece cu **572 de teste** (513 înainte). Mecanismele comune sunt descrise în `docs/ARCHITECTURE.md`, „Garanții adăugate după auditul de integrare”.

| ID | Stare | Unde e testul |
|---|---|---|
| C01, C06, C07, P02 | corectat în nucleu | `tests/test_integration_guards.py` |
| C02, C03, C04 | corectat | `app/features/f1_pms_integration/tests/test_publish_failures.py`, `tests/test_integration_guards.py` |
| C05 | corectat | `tests/test_real_adapters.py` |
| I01–I05, I07–I11 | corectat (I11: ținta cu incident deschis nu se mai oferă) | `app/features/f2_incidents/tests/test_integration_guards.py` |
| I06 | corectat în ambele inboxuri și în rezumatul aprobării | `tests/test_ui_smoke.py`, testele F2 |
| R01–R08 | corectat | `app/features/f4_review_analysis/tests/test_integration_guards.py` |
| P01, P03, P04, P05 | corectat | `app/features/f5_relisting/tests/test_integration_guards.py` |
| U01–U04 | corectat; `npm run compile` și `npm run build` trec | fără test automat (extensia nu are suită de teste) |
| D01 | implementat: `API_TOKEN` opțional | `tests/test_api_auth.py` |
| D04 | corectat în proces: lock comun pe calendar | testele F2 și F5 |
| D07 | documentația centrală actualizată | — |
| D03 | închis ulterior: copilotul transmite răspunsul la ofertă prin `GuestOfferAnswered` | testele F2 și F3 de conversație |
| D02 | atenuat: după relocare copilotul răspunde cu noua adresă; rezervarea din PMS se mută tot manual | testele F3 de conversație |
| D06 | atenuat: parafraza din audit e acum blocată; garda rămâne euristică | `test_incident_conversation.py` |
| D05 | **rămâne deschis**, documentat în `docs/ARCHITECTURE.md` §6 | — |

**Scripturile de reproducere din `scripts/`** verifică prezența defectelor vechi, deci cele mai multe se opresc acum la prima aserțiune (defectul nu mai apare). `scripts/audit_integration_core.py` raportează `PASS` pentru toate cazurile, în afară de `public_api_without_auth` (cazul setează doar `WEBHOOK_TOKEN`; autentificarea API-ului se activează cu `API_TOKEN`) și `free_text_guard_paraphrase` (D06, deschis). `f5_pricing_stress.py` raportează zero încălcări.

## Cum se reproduc probele

Din rădăcina proiectului, folosind mediul virtual existent:

```powershell
python manage.py test
cd extension
npm ci
npm run compile
npm run build
cd ..

.\.venv\Scripts\python.exe scripts/audit_integration_core.py
.\.venv\Scripts\python.exe scripts/integration_audit/f2_f3.py
.\.venv\Scripts\python.exe scripts/integration_audit/f4.py
.\.venv\Scripts\python.exe scripts/integration_audit/f4_ui.py
.\.venv\Scripts\python.exe scripts/integration_audit/f5.py
.\.venv\Scripts\python.exe scripts/integration_audit/f5_advanced.py
.\.venv\Scripts\python.exe scripts/integration_audit/f5_zero_price.py
.\.venv\Scripts\python.exe scripts/integration_audit/f5_pricing_stress.py

.\.venv\Scripts\python.exe scripts/integration_audit/extension_fixture.py
node scripts/integration_audit/extension_refresh.js
.\.venv\Scripts\python.exe scripts/integration_audit/extension_race_fixture.py
node scripts/integration_audit/extension_refresh_race.js
```

Pe macOS/Linux se înlocuiește executabilul Python cu `.venv/bin/python`. Probele folosesc baze temporare și mock-uri. Scripturile JS necesită generarea prealabilă a fixture-urilor Python corespunzătoare.

**Interpretarea codului de ieșire:** `audit_integration_core.py` și `f5_pricing_stress.py` întorc intenționat `1` când găsesc neconcordanțele descrise. Celelalte scripturi au aserțiuni asupra rezultatului observat și întorc `0` când reproduc problema; acest `0` nu afirmă că aplicația nu are defecte. Rezultatele brute și sumarul execuției sunt în `docs/audit_results/`.

Nu au fost schimbate regulile de business, sursele `app/`, sursele `extension/` sau testele existente. Nu s-au folosit credențiale reale și nu s-au executat operații pe PMS-uri de producție. Scenariile cu evenimente interne construite direct testează granițele dintre componente și nu trebuie confundate cu exploatări demonstrate prin API public.
