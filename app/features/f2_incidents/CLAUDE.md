# F2 — Incidente

## Scop
Primește sesizări despre probleme la o proprietate, de la personal (prin aplicație) sau de la oaspete (prin F3), și deschide un incident. Propune hostului fie remedierea la timp, fie relocarea oaspetelui într-o proprietate liberă compatibilă. După decizia hostului execută acțiunea aleasă și anunță oaspetele.

## Contract
- Ascultă: `IssueReported` (sursă `staff` sau `guest`; un singur abonat, `on_issue_reported`), `ApprovalDecided` pentru aprobările proprii (`incident_resolution`, `incident_closure`), `GuestOfferAnswered` (răspunsul oaspetelui la oferta de relocare, dat în conversație), `GuestMessageReceived` (doar prelungește termenul ofertei), evenimentele interne din `events.py`.
- Emite: `ApprovalRequested` (prin serviciul de aprobări), `GuestMessageSent` (prin `ChannelGateway`), `IssueReported` (doar din `POST /incidents/report`, aceeași cale ca simulatorul). Private, în `events.py`: `IncidentDecided`, `IncidentResolved` și pașii interni (`StaffContactRequested`, `StaffReplied`, `EstimateReady`, `GuestAnsweredRelocation`, `IncidentTaskCompleted`, `IncidentTimeout`, `Sim*Due`).
- Date deținute: `f2_incidents`, `f2_incident_reports`, `f2_staff_messages`, `f2_tasks`. Publică în `guest_topics` (nucleu) starea incidentului pe care oaspetele are voie să o afle.
- Porturi folosite: `calendar` (disponibilitatea candidaților, blocarea la relocare), `channel` (mesajele către oaspete), `llm` (prin `ctx.agent`).
- Cereri de aprobare: `incident_resolution` (opțiunile `relocate` = „oferă relocarea”, doar dacă există o proprietate compatibilă liberă, și `remediate`; cea recomandată e prima), `incident_closure` (opțiunea `close`).

## Stare curentă
- **Fluxul complet** (vezi „Cerințe suplimentare”): sesizare → triere (agent) → întrebare către mentenanță (agent) → răspuns (simulat) → estimare (agent, cel mult o clarificare) → propunere cu recomandare calculată în cod și mesajele către oaspete scrise de agent → decizia hostului → ofertă de relocare → răspunsul oaspetelui (simulat) → relocare sau scuze cu estimarea → sarcina de reparație (terminată de personalul simulat) → rezumatul de închidere (agent) → aprobarea închiderii → `resolved`.
- Modulele: `service.py` (orchestrarea, câte un agent per handler), `agents.py` (agenții și uneltele), `rules.py` (regulile pure: compatibilitate, timp până la check-in, „la timp”, recomandare, validări), `repo.py` (tabelele + `ensure_columns` pentru baze vechi), `simulation.py` (personalul simulat), `templates.py` (fallback-urile), `mock_llm.py`, `prompts/`, `settings.py` (`F2_*`).
- Endpointuri: `GET /incidents`, `GET /incidents/{id}`, `POST /incidents/report`, `POST /incidents/{id}/staff-reply`, `POST /incidents/{id}/guest-answer`, `POST /incidents/tasks/{id}/complete`.
- Pagina Streamlit și tab-ul „Incidents” din extensie arată incidentele, estimarea, recomandarea, conversația cu personalul și sarcinile; ambele doar citesc.
- Termenele (personal, oaspete) stau în baza de date și se verifică la fiecare eveniment F2 și la `GET /incidents`.
- Testat inițial: 41 de teste F2 (fluxuri cap-coadă, deduplicare inclusiv simultană, timeouturi, intrări manuale, fallback-uri cu LLM căzut, reguli pure, migrarea schemei); suita completă trece neschimbată în afara F2. Verificat și pe API-ul pornit cu întârzieri de simulare (calea în fundal).
- **După auditul de integrare (14 noiembrie 2026), 54 de teste F2** (`tests/test_integration_guards.py` pentru cele de mai jos):
  - **ținta relocării** trebuie să fie compatibilă, liberă pe un calendar complet al ei (`covers_free_nights`), fără incident deschis și pe o conexiune care poate bloca disponibilitatea; toate se reverifică la acceptarea oaspetelui, sub lockul comun `ctx.calendar.lock(target)`, deci două relocări nu ajung în aceeași proprietate;
  - **nimic confirmat pe jumătate:** dacă ținta nu mai e potrivită, blocarea în calendar eșuează sau oferta nu poate fi livrată, oaspetele nu primește nimic în plus, hostul e notificat și F2 cere o propunere nouă (`_propose_again`); o ofertă nelivrată nu mai pornește răspunsul simulat;
  - **rezervarea trebuie să fie a proprietății raportate:** `POST /incidents/report` (și `/sim/staff-report`) răspund `422`; un `IssueReported` cu o rezervare străină folosește sejurul real al proprietății;
  - **un raport nou după „reparat”** retrage cererea de închidere (`ctx.approvals.withdraw`), creează o sarcină „Follow-up: reported again after the repair: …” și anunță hostul; aprobarea veche nu mai poate închide incidentul;
  - **textul către oaspete de la închidere** apare și în rezumatul aprobării, iar ambele inboxuri arată descrierea opțiunii și când e una singură;
  - **termenele** se restaurează dacă evenimentul de timeout nu a putut fi publicat;
  - **trierea mock** separă aceeași defecțiune din încăperi diferite (scurgere la chiuveta din bucătărie vs. dușul din baie); promptul de triere spune același lucru modelului real;
  - pașii simulați aflați în pauză se opresc la shutdown/reset (`ctx.on_quiesce`).
- **Conversația cu oaspetele (14 noiembrie 2026)**, 66 de teste F2 (`tests/test_guest_conversation.py` pentru cele de mai jos):
  - `guest_view.py` calculează, pentru fiecare stare, ce are voie să afle oaspetele; `service._sync_guest_topic` îl publică în `ctx.guest_topics` după fiecare scriere a incidentului (hook-ul `repo.on_change`). Înainte de decizia hostului: „echipa evaluează”, fără estimare. `awaiting_guest`: oferta cu detaliile țintei și linkul anunțului. `remediation_scheduled`: estimarea, doar dacă oaspetele a primit-o într-un mesaj aprobat. `relocated`: noua proprietate, valabil până la check-out (rămâne și după închiderea incidentului). `resolved` fără mutare și `dismissed`: subiectul se închide;
  - răspunsul oaspetelui din chat ajunge ca `GuestOfferAnswered` și trece prin aceeași metodă ca endpointul (`answer_relocation`); un răspuns la o ofertă deja închisă nu schimbă nimic și notifică hostul;
  - un mesaj al oaspetelui cât timp oferta e deschisă prelungește termenul (nu mai e trecut automat la „rămâne” în timp ce pune întrebări);
  - oferta de relocare conține linkul anunțului țintei, când există (`Property.listing_url`), în ciorna agentului și în șablon;
  - mentenanța simulată primește din `simulation.py` faptele și durata („situația”), iar modelul doar le formulează: pe model real, AC ≈ 30 h, scurgere ≈ 2 h, ca în mock;
  - agentul `closure` nu mai primește starea internă și valorile lipsă;
  - pagina F2 are două butoane pe incidentele în `awaiting_guest`, pentru când hostul a lămurit direct cu oaspetele.
- **Verificat pe model real** (`anthropic/claude-sonnet-5.5`, 14 noiembrie 2026): trierea (aceeași scurgere în alte cuvinte se atașează, becul rămâne separat), întrebarea și interpretarea răspunsului mentenanței, ciornele cu link, rezumatul de închidere și mesajul „s-a reparat”. Scenariile sunt în `scripts/eval_models.py`.

## Decizii
- Ce află oaspetele se hotărăște în cod (`guest_view.py`), nu în promptul copilotului: în `facts` intră doar ce a aprobat hostul sau ce e pur informativ. Textele evită formulările pe care garda copilotului le tratează drept promisiuni („confirmed”, „you can”).
- Duratele din simulare vin din cod, nu din model — demo-ul rămâne previzibil și pe model real (AC → relocare recomandată, scurgere → reparație pe loc).
- O proprietate cu incident deschis nu e oferită ca țintă de relocare — nu mutăm un oaspete într-o proprietate cu o problemă cunoscută (decizia de politică cerută de audit, I11).
- Fără posibilitatea de a bloca ținta în calendar, relocarea nu se oferă și nu se confirmă — o țintă neblocată poate fi vândută altcuiva după confirmare. Pe OwnerRez (disponibilitate nescriabilă) F2 oferă deci doar remedierea.
- Un pas aprobat care nu se poate executa duce la o propunere nouă, nu la o stare intermediară — hostul decide din nou, cu faptele actualizate.
- Un agent per handler, pașii asincroni prin evenimente interne — limita de 180 s a unui handler, corelarea și `?wait=true` funcționează singure.
- Rezultatele structurate ale agenților se scriu prin unelte cu validare în cod (`record_estimate`, `save_guest_messages`, `save_closure`), nu se parsează din text liber; doar trierea întoarce `attach:<id>` / `new` în `finish`, validat de `parse_triage_result`.
- Ciornele către oaspete se scriu înainte de decizie și se arată verbatim în opțiunile aprobării; alegerea opțiunii le aprobă pe toate — o singură aprobare per decizie, nimic regenerat după.
- Ambele opțiuni se oferă când există o proprietate liberă; recomandarea doar le ordonează — hostul poate alege altceva.
- Mesajele către personal nu cer aprobare — sunt comunicare internă, vizibile în istoric și în timeline.
- Întârzierea 0 publică pasul simulat direct (îl așteaptă `?wait=true`); peste 0 rulează în fundal, cu contextul copiat — UI-ul nu stă blocat, corelarea se păstrează.
- Timeouturile nu sunt sarcini care dorm — termenele sunt în baza de date, deci nu se pierd la restart.
- `ensure_columns` adaugă coloanele noi pe o bază veche — nu e nevoie de `manage.py seed` după actualizare.
- Rapoartele de la oaspete și de la personal despre aceeași problemă se atașează aceluiași incident; o sesizare atașată nu reîntreabă mentenanța.
- Personalul simulat răspunde după tipul problemei (aer condiționat ~30 h, scurgere ~2 h, bec ~30 min, problemă neclară → clarificare → ~4 h), ca demo-ul să arate ambele ramuri.
- `IncidentResolved` rămâne privat până îl consumă alt feature.

## Plan faza 1

**Zona permisă:** `app/features/f2_incidents/`, `extension/`. Opțional, adăugări minime în `.env.example` pentru setările `F2_*` (regula 4).

**Fluxul țintă, pe scurt** (detaliile sunt în „Cerințe suplimentare”): sesizare → triere (atașez sau deschid) → agentul întreabă mentenanța cât durează → estimare → propunere către host (oferă relocarea dacă e disponibilă și necesară, altfel remediere), cu mesajele exacte pentru oaspete → hostul aprobă → oaspetele primește oferta → acceptă (relocare) sau rămâne (scuze + estimare) → sarcina de reparație → personalul termină → aprobarea închiderii → `resolved`. În demo, personalul și răspunsul oaspetelui sunt simulați automat: nimeni nu apasă nimic în afară de host.

**De implementat, în ordine:**

1. [x] **Model de date și stări.** Stările noi, tabelele noi, evenimentele private (vezi „Stări, tabele și evenimente”).
2. [x] **Conversația cu mentenanța.** Agentul întreabă persoana de mentenanță, interpretează răspunsul, obține o estimare (ore sau „necunoscut”). Răspunsul vine automat de la personalul simulat.
3. [x] **Propunerea către host.** Recomandarea calculată în cod din estimare și timpul până la check-in; ciornele mesajelor către oaspete scrise de LLM; aprobarea `incident_resolution`.
4. [x] **Oferta de relocare și răspunsul oaspetelui.** Oaspetele acceptă sau rămâne; răspunsul e simulat automat în demo și poate fi dat și prin endpoint.
5. [x] **Sarcini pentru personal și închiderea incidentului.** Sarcina de reparație, terminarea ei (automată în demo), rezumatul agentului, aprobarea `incident_closure`, `resolved`.
6. [x] **Deduplicarea sesizărilor**, cu agentul de triere.
7. [x] **Extensia Chrome:** tab „Incidents”, doar pentru citire.

**Criterii de acceptare:**
- **testele din afara F2 trec neschimbate**: `tests/test_ui_smoke.py`, `tests/test_api.py`, `app/features/f3_guest_copilot/tests/test_copilot.py` (vezi „Compatibilitate cu restul aplicației”);
- pe setările implicite, o sesizare parcurge singură drumul până la aprobarea hostului, iar după aprobare singură până la aprobarea închiderii;
- hostul vede în aprobare răspunsul mentenanței, estimarea, timpul până la check-in, recomandarea și mesajele exacte pentru oaspete; opțiunea recomandată e prima;
- oaspetele care acceptă e relocat și primește confirmarea; oaspetele care rămâne primește scuzele cu estimarea („as soon as possible” dacă estimarea e necunoscută);
- nimic nu pleacă spre oaspete înainte de decizia hostului, iar ce pleacă e exact textul arătat în aprobare;
- personalul care nu răspunde și oaspetele care nu răspunde nu blochează incidentul (timeout → pasul următor + notificare);
- două sesizări identice la rând (inclusiv simultane) produc un singur incident și o singură aprobare; două sesizări cu aceeași problemă în cuvinte diferite se atașează aceluiași incident (în teste, pe LLM-ul mock);
- cu LLM-ul căzut, fiecare agent are fallback-ul lui și fluxul ajunge tot la host;
- `cd extension && npm run build && npm run compile` trec.

**De verificat manual:** extensia încărcată în Chrome (`chrome://extensions` → Load unpacked), aprobare și respingere din panou. Relocarea pe o conexiune PMS reală depinde de citirea calendarului (F1). **Cu modelul real (OpenRouter):** trierea pe perechi de sesizări (aceeași problemă în alte cuvinte, probleme diferite în aceeași cameră), interpretarea răspunsurilor vagi ale mentenanței, ciornele către oaspete (ton, fără promisiuni în plus), rezumatul de închidere și ajustarea prompturilor după rezultate. Pe mock ordinea pașilor e scrisă de noi, deci nu spune nimic despre prompturi.

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

Stabilite înainte de one-shot. Unde contrazic textul de mai sus, au prioritate acestea.

### Principiu
- Agentul (F2) face totul: vorbește cu mentenanța, propune soluția, scrie mesajele, urmărește execuția. **Hostul doar citește și aprobă.** Orice acțiune cu impact asupra oaspetelui (ofertă de relocare, relocare, promisiunea unui termen, mesajul „s-a rezolvat”) trece prin inboxul comun de aprobări.
- Mesajele agentului către personal sunt comunicare internă și pleacă fără aprobare; hostul le vede în istoricul incidentului și în timeline.
- **În demo totul merge singur:** personalul și oaspetele sunt simulați automat (vezi „Simulare”). Singurul om care acționează e hostul, din inboxul de aprobări.

### LLM-ul
- **Acum nu avem cheie LLM; ea se poate adăuga oricând** (`LLM_PROVIDER=openrouter` în `.env`), fără nicio schimbare de cod. Până atunci totul rulează pe LLM-ul mock, deci **politicile mock sunt calea folosită acum în demo**: trebuie să producă rezultate credibile pentru scenariile de demo, nu doar să treacă testele.
- Agenți, toți prin bucla comună (`ctx.agent.run` cu `AgentSpec`), pe modelul `decision`, cu prompturile în `prompts/`:
  - `f2_incidents.triage` — sesizare nouă: o atașez unui incident deschis sau deschid unul nou;
  - `f2_incidents.staff_liaison` — scrie întrebarea către mentenanță, interpretează răspunsul, cere cel mult o clarificare, extrage estimarea;
  - `f2_incidents.proposal` — scrie mesajele către oaspete pentru fiecare opțiune oferită hostului;
  - `f2_incidents.closure` — scrie rezumatul pentru aprobarea `incident_closure` și, dacă e cazul, mesajul „s-a rezolvat”;
  - `f2_incidents.sim_staff` — **doar simulare**: joacă rolul persoanei de mentenanță și răspunde la întrebare (vezi „Simulare”).
- Prompturile respectă `CLAUDE.md` §5: obiectivul și condiția de terminare, nu „ești un asistent”. Uneltele moștenesc `ToolArgs` (au `reason`), citesc doar datele noastre și întorc output scurt. Textele venite de la oameni (sesizări, răspunsuri ale personalului) intră în obiectiv între delimitatori, marcate ca date.
- **Ce rămâne în cod, nu în LLM:** recomandarea, compatibilitatea proprietăților, ce opțiuni se oferă, limitele (numărul de întrebări către personal), validarea tuturor răspunsurilor agenților.
- **Ce rămâne obligatoriu** (regulile proiectului): politici mock în `mock_llm.py`, înregistrate cu `ctx.register_mock_llm_policy`, câte una per agent; un fallback minim la eroare sau timeout pentru fiecare agent (descris la fiecare pas). Nu se construiește logică alternativă elaborată.

### Stări, tabele și evenimente (pasul 1)
- Stări: `awaiting_staff` → `awaiting_decision` → `awaiting_guest` (oferta de relocare a plecat) → `relocated` | `remediation_scheduled`; apoi → `resolved`. Din `awaiting_decision`, respingerea duce în `dismissed`. Fără rezervare afectată, `awaiting_decision` duce direct în `remediation_scheduled`.
- **Valorile existente rămân**: stările `awaiting_decision`, `remediation_scheduled`, `relocated`, `dismissed` și cheile de opțiune `remediate`, `relocate` își păstrează numele (le folosesc testele din afara F2).
- **„Deschis”** înseamnă orice stare în afară de `resolved` și `dismissed`. Doar incidentele deschise primesc sesizări atașate.
- Tabele proprii (nume orientative): `f2_incidents` (extins: estimarea în ore sau null, sursa ei, ciornele mesajelor către oaspete pe variantă, răspunsul oaspetelui), `f2_incident_reports` (fiecare sesizare: cine, ce, când, sursa), `f2_staff_messages` (conversația cu personalul, în ambele direcții), `f2_tasks` (sarcinile). Schimbările de schemă cer `python manage.py seed`.
- Evenimente private, în `events.py`:
  - `IncidentDecided` — la decizia hostului, cu opțiunea aleasă;
  - `IncidentResolved` — doar la închiderea finală (`resolved`) și la respingere (motiv `dismissed`);
  - evenimente interne pentru pașii asincroni (ex. `StaffReplied`, `GuestAnsweredRelocation`, `TaskCompleted`). Pașii asincroni trec prin event bus, nu prin apeluri directe: așa moștenesc corelarea, sunt izolați, apar în timeline și sunt așteptați de `?wait=true`.

### Conversația cu mentenanța (pasul 2)
- Pe cine întreabă: personalul `maintenance` alocat proprietății, apoi oricine alocat, chiar dacă sesizarea a venit de la femeia de serviciu. Fără nimeni alocat: se sare direct la propunere, cu estimarea „necunoscut”, și hostul primește o notificare `warning`.
- Fluxul e asincron, pentru că un agent nu poate aștepta ore: rularea scrie întrebarea și se încheie (`awaiting_staff`); răspunsul personalului (eveniment intern) pornește o rulare nouă, care primește din baza de date conversația de până atunci.
- Agentul încheie cu `finish` și un rezultat structurat: estimarea în ore (sau „necunoscut”), certitudinea și citatul relevant. Codul validează (număr pozitiv, plafon rezonabil); un rezultat invalid devine „necunoscut”.
- Cel mult o clarificare (deci cel mult două întrebări per incident), impus în cod. După aceea, estimarea e ce s-a putut afla sau „necunoscut”.
- Personalul primește și mesajele interne următoare, tot fără aprobare: „go ahead” când hostul alege o variantă cu reparație, sarcina creată, „thanks” la terminare.
- Timeout: fără răspuns în `F2_STAFF_REPLY_TIMEOUT_MIN` (implicit `30`) → propunerea pleacă cu estimarea „necunoscut”, hostul e notificat.
- Fallback LLM: întrebare-șablon către personal; dacă interpretarea eșuează → estimarea „necunoscut”.
- Canalul cu personalul e al F2: tabelul `f2_staff_messages` plus endpointuri F2. **Trimiterea către personal stă într-o singură funcție**, ca un canal real (WhatsApp, aplicație) să o poată înlocui fără schimbări în restul fluxului.

### Propunerea către host (pasul 3)
- Timpul până la check-in = data de check-in + `check_in_time` din regulile hostului (oră locală) − momentul curent, cu data din `ctx.clock.today()` (poate fi înghețată pentru demo). Calculul primește momentul curent ca parametru, ca testele să-l fixeze. Dacă oaspetele e deja cazat, se marchează „in-house”.
- Reparația e **„la timp”** dacă estimarea e cunoscută și: oaspetele nu a sosit și reparația se termină cu cel puțin `F2_FIX_MARGIN_HOURS` (implicit `2`) înainte de check-in; sau oaspetele e cazat și estimarea e cel mult `F2_INHOUSE_TOLERABLE_HOURS` (implicit `3`). O estimare necunoscută nu e „la timp”.
- **Regula de recomandare (în cod):** reparația nu e „la timp” și există o proprietate compatibilă liberă → **oferă relocarea** (opțiunea `relocate`, prima); altfel → **remediere** (opțiunea `remediate`). Ambele opțiuni se oferă când există o proprietate liberă, ca hostul să poată alege altceva decât recomandarea.
- Fără rezervare afectată: nu există mesaje pentru oaspete și nici relocare; aprobarea oferă doar remedierea.
- Ciornele către oaspete (agentul `proposal`), salvate la incident:
  - opțiunea `relocate`: **oferta** (problema, scuze, propunerea de mutare în proprietatea X, întrebarea dacă acceptă), **confirmarea** pentru cazul în care acceptă, **scuzele cu estimarea** pentru cazul în care rămâne („will be fixed in about X hours”, sau „as soon as possible” când estimarea e necunoscută);
  - opțiunea `remediate`: **scuzele cu estimarea**; dacă reparația se termină înainte de sosirea oaspetelui, nu există mesaj (oaspetele nu trebuie îngrijorat degeaba).
- Aprobarea `incident_resolution` arată: sesizarea (și câte rapoarte are), răspunsul mentenanței citat, estimarea, timpul până la check-in sau „in-house”, recomandarea cu motivul ei. Descrierea fiecărei opțiuni arată acțiunea și textul exact al fiecărui mesaj („Guest will receive: …”, „If the guest accepts: …”, „If the guest stays: …”). **Alegerea opțiunii aprobă toate mesajele ei**; nu se cer aprobări separate pentru mesaje.
- La oaspete pleacă textul salvat, nu unul regenerat. Validare în cod: text nevid, lungime maximă, fără placeholder-e. Ciornă invalidă sau agent căzut → mesajele-șablon, marcate „template” în aprobare.
- Hostul nu poate edita textul, doar aproba sau respinge (vezi „Probleme deschise”).
- Respingere → `dismissed`, nimic către oaspete, `IncidentResolved` cu motivul `dismissed`.

### Oferta de relocare și răspunsul oaspetelui (pasul 4)
- Hostul alege `relocate` → oferta aprobată pleacă la oaspete, incidentul trece în `awaiting_guest`. Calendarul **nu** se blochează încă.
- Răspunsul oaspetelui ajunge în F2 printr-o metodă a serviciului, independentă de canal, expusă ca `POST /incidents/{id}/guest-answer` (`accept` / `stay`) și folosită de simulare.
- **Acceptă** → se reverifică disponibilitatea proprietății țintă. Liberă: blocare în calendar, confirmarea aprobată pleacă la oaspete, `relocated`, sarcina de reparație, hostul e notificat să mute rezervarea în PMS. Ocupată între timp: nimic către oaspete, hostul e notificat, F2 cere o aprobare nouă cu opțiunile recalculate.
- **Rămâne** → scuzele cu estimarea (aprobate) pleacă la oaspete, `remediation_scheduled`, sarcina de reparație.
- Timeout: fără răspuns în `F2_GUEST_ANSWER_TIMEOUT_MIN` (implicit `60`) → tratat ca „rămâne” și hostul e notificat.
- Hostul alege `remediate` → mesajul aprobat (dacă există) pleacă la oaspete, `remediation_scheduled`, sarcina de reparație.

### Sarcini și închiderea incidentului (pasul 5)
- Sarcina de reparație (`f2_tasks`): incident, persoana de contact (sau neasignat), descriere, stare (`open` / `done`), nota de final, momente. ID scurt, ușor de citat într-un mesaj (ex. `T-12`). Fără tarife.
- Se creează la `remediate`, la oaspetele care rămâne și la relocare (proprietatea rămâne stricată). Fără nimeni alocat: sarcină neasignată + notificare `warning`.
- Personalul marchează sarcina terminată cu o notă: metodă a serviciului expusă ca `POST /incidents/tasks/{id}/complete`; în demo o face personalul simulat.
- Când ultima sarcină deschisă e terminată, agentul `closure` scrie rezumatul (ce s-a raportat și de câte ori, răspunsul și estimarea mentenanței, decizia hostului, alegerea oaspetelui, ce a făcut personalul și când, orice neclaritate din notă, ex. „temporary fix”). Dacă oaspetele a rămas și sejurul nu s-a terminat, scrie și mesajul „s-a rezolvat”. F2 cere aprobarea `incident_closure` (o singură dată, cu `dedupe_key`).
- **Aprobat** → `resolved`, `IncidentResolved`, mesajul din aprobare (dacă există) pleacă la oaspete.
- **Respins** → incidentul rămâne deschis, sarcina nouă „Follow-up: <nota hostului>” pentru aceeași persoană, hostul e notificat. Extensia nu are câmp de notă la respingere; fără notă, textul sarcinii e „Follow-up requested by the host”.
- Fallback LLM: rezumatul e lista brută a faptelor, mesajul e șablonul „fixed”, aprobarea se cere oricum.
- Nu există un endpoint de rezolvare manuală (abatere de la planul inițial).

### Deduplicare (pasul 6)
- Un `asyncio.Lock` per proprietate în serviciu, ținut pe toată decizia „atașez sau deschid”, ca două sesizări simultane să nu deschidă două incidente.
- Fără incident deschis la proprietate → incident nou direct (nu există nimic de comparat). Altfel decide agentul `triage`, cu unealta `list_open_incidents` (descriere, rapoarte, stare; output scurt) și `finish(result="attach:<id>" | "new", evidence)`. Promptul cere ca problemele diferite din aceeași cameră sau cu cuvinte comune să rămână separate.
- Codul validează: un ID care nu e printre incidentele deschise ale proprietății sau un rezultat în alt format → incident nou.
- Fallback LLM: incident nou + notificare către host că ar putea fi un duplicat (în cel mai rău caz două incidente, niciodată o sesizare pierdută).
- La atașare: raportul se salvează, severitatea crește dacă noul raport e mai grav, hostul primește o notificare scurtă, nu se creează aprobare nouă și nu se reîntreabă mentenanța.

### Simulare (demo fără nicio intervenție)
- Totul în F2 (ex. `simulation.py`), activat din setări, marcat clar ca simulare în istoricul incidentului și în UI.
- **Personalul simulat:** la fiecare întrebare, agentul `sim_staff` răspunde ca persoana de mentenanță, după `F2_SIM_STAFF_DELAY_S`. Politica mock dă răspunsuri credibile și variate după tipul problemei (ex. AC → „need a part, tomorrow evening”; bec → „30 minutes”), ca demo-ul să poată arăta ambele ramuri. După ce se creează sarcina, personalul simulat o termină după `F2_SIM_TASK_DELAY_S`, cu o notă.
- **Oaspetele simulat:** la o ofertă de relocare răspunde `F2_SIM_GUEST_ANSWER` (`accept` | `stay` | `off`) după `F2_SIM_GUEST_DELAY_S`, prin aceeași metodă ca endpointul. Răspunsul se vede în istoricul incidentului; nu trece prin chatul oaspetelui (acela aparține F3).
- Setări: `F2_SIM_ENABLED` (implicit `true`), întârzierile (implicit `0`), `F2_SIM_GUEST_ANSWER` (implicit `accept`). Valorile implicite sunt alese ca testele din afara F2 să treacă (vezi mai jos); pentru demo, README-ul feature-ului recomandă întârzieri de câteva secunde, ca fluxul să se vadă pas cu pas.
- Pagina F2 din Streamlit arată conversația cu personalul, sarcinile și răspunsul oaspetelui. Fără „Staff view” manual: personalul e simulat. Endpointurile rămân, pentru o viitoare aplicație a personalului.

### Execuție asincronă, corelare și temporizatoare
- **Un singur agent per handler.** Fiecare pas (triere, întrebare către personal, interpretarea răspunsului, propunere, închidere) rulează în handlerul propriului eveniment intern. Un handler are limita de 180 s, iar un apel la model are până la 60 s; mai mulți agenți în același handler ar putea depăși limita cu un model real.
- **Corelarea.** `ctx.approvals.request` și `bus.publish` iau corelarea din evenimentul în curs (`current_correlation_id()`), care există doar în interiorul unui handler. Deci: incidentul își salvează `correlation_id`-ul la deschidere; orice muncă (aprobări, mesaje, publicări) se face doar în handleri; orice pas pornit din afara unui handler (verificarea termenelor, endpointurile `guest-answer` / `tasks/{id}/complete`) doar publică un eveniment intern F2 cu `correlation_id` setat explicit la cel al incidentului, iar munca o face handlerul lui. Așa un incident are o singură corelare de la sesizare până la închidere.
- **Întârzierile simulării.** Cu întârziere `0` (implicit, folosit în teste), pasul simulat se publică direct ca eveniment intern, deci `?wait=true` îl așteaptă (`bus.join` așteaptă și handlerii porniți în lanț). Cu întârziere > 0 (demo), handlerul pornește o sarcină `asyncio` care doarme și apoi publică evenimentul; contextul se copiază la crearea sarcinii, deci corelarea se păstrează, iar UI-ul nu stă blocat cât doarme simularea. Fluxul continuă vizibil în fundal (extensia se reîmprospătează la 5 s).
- **Timeouturile (personal, oaspete) nu sunt sarcini care dorm.** Termenul se salvează la incident; o funcție de verificare publică evenimentul de timeout pentru incidentele cu termen depășit. Verificarea rulează la fiecare eveniment care ajunge la F2 și la `GET /incidents` (pe care extensia îl citește periodic). Testele o apelează direct, cu ceasul fixat. Nu se pierde nimic la restart: termenele sunt în baza de date.

### Compatibilitate cu restul aplicației (obligatoriu)
- Nu se modifică nimic în afara zonei permise. Testele din afara F2 care trec prin F2 rămân neschimbate și verzi:
  - `tests/test_ui_smoke.py::test_staff_report_form_and_two_option_approval` — după sesizare există exact un incident și exact o aprobare, cu opțiunea `relocate`; după aprobarea ei, incidentul e `relocated`;
  - `app/features/f3_guest_copilot/tests/test_copilot.py` — sesizarea oaspetelui produce un incident și exact o aprobare, pe aceeași corelare; cu LLM-ul căzut, sesizarea personalului produce tot un incident;
  - `tests/test_api.py` — sesizarea produce notificări.
- De aici rezultă:
  - pe setările implicite, simularea e activă cu întârziere `0` și oaspetele simulat acceptă; pașii asincroni trec prin event bus, ca `?wait=true` să-i aștepte și corelarea să se păstreze; mesajele către personal nu sunt aprobări;
  - **`IssueReported` are un singur abonat în F2, cu numele `on_issue_reported`** (testul F3 verifică exact lista handlerilor acestui eveniment); pașii următori folosesc evenimente interne F2;
  - testul smoke trimite descrierea implicită din simulator („Water is leaking under the kitchen sink.”) pentru oaspetele cazat acum la Calea Victoriei; opțiunea `relocate` trebuie să existe oricare ar fi estimarea, de aceea ambele opțiuni se oferă când există o proprietate liberă.
- **Testele proprii F2** (`tests/test_incidents.py`) se actualizează acolo unde comportamentul se schimbă intenționat (ordinea opțiunilor după recomandare, oferta urmată de confirmare la relocare, momentul lui `IncidentResolved`), cu verificări cel puțin la fel de stricte. Nu se șterg cazuri fără înlocuitor.
- Dacă una dintre condițiile pentru testele din afara F2 nu se poate respecta, oprește-te și spune (regula 3), nu modifica acele teste.

### Extensia (pasul 7)
- Tab „Incidents”, doar pentru citire: proprietatea, starea, descrierea, numărul de rapoarte, estimarea mentenanței, recomandarea, alegerea oaspetelui, sarcinile și starea lor. Fără butoane; deciziile rămân în „Approvals”.

## Probleme deschise / TODO
- Relocarea nu mută rezervarea în PMS; hostul trebuie să o mute manual (notificarea îi spune asta).
- Pe o conexiune PMS reală relocarea nu e oferită: OwnerRez nu poate bloca disponibilitatea prin API, iar calendarul Hospitable e stub.
- După relocare, rezervarea din PMS arată tot proprietatea veche până când hostul o mută manual; copilotul răspunde cu noua adresă din subiectul publicat de F2.
- Copilotul află despre incident doar ce publică `guest_view.py`. Întrebările la care faptele nu răspund (compensații, altă proprietate, ora exactă a tehnicianului) sunt predate hostului; mesajele proactive către oaspete în afara celor aprobate nu există.
- După ce copilotul predă discuția hostului, incidentul rămâne în `awaiting_guest` până la răspuns (chat, butoanele din pagina F2) sau până la timeout.
- **WhatsApp pentru personal (după faza 1).** Cere lucru în afara F2, în PR-uri separate: port + adaptor de canal (cel mai simplu, Twilio Sandbox; WhatsApp Business API direct cere verificare Meta și șabloane), webhook de intrare în F1, numărul de telefon în `StaffMember` și un eveniment comun pentru mesajele personalului (doar prin adăugare). În F2 se înlocuiește doar `IncidentService.send_to_staff`, iar răspunsurile intră pe `receive_staff_reply`.
- **Aplicație dedicată pentru personal**, dacă rămâne timp; endpointurile F2 o pregătesc.
- **Editarea ciornei de către host** înainte de trimitere cere o schimbare în nucleu (decizie cu text modificat), într-un PR separat, prin adăugare.
- Extensia nu are câmp de notă la respingere; respingerea închiderii din extensie creează sarcina „Follow-up requested by the host”.
- Temporizatoarele simulării cu întârziere > 0 sunt sarcini în proces: un restart în timpul pauzei pierde pasul simulat (incidentul ajunge oricum la timeout și apoi la host).
- Prompturile n-au fost rulate pe un model real (nu avem cheie): de verificat trierea, interpretarea răspunsurilor vagi, ciornele și rezumatul (vezi „De verificat manual”).

## Note pentru Claude
- Nu modela tarife pentru personal sau reparații (`CLAUDE.md` §1).
- Regula de compatibilitate e o funcție pură (`is_compatible`): schimb-o acolo și adaugă cazul în `tests/`. La fel regula „la timp” și regula de recomandare: funcții pure, testate separat.
- Mesajele către oaspete pleacă cu `sender="host"`, fiindcă urmează unei decizii a hostului.
- F3 nu e cunoscut de acest feature: sesizările oaspeților ajung aici ca `IssueReported`, răspunsul la ofertă ca `GuestOfferAnswered`, iar ce află oaspetele pleacă prin `ctx.guest_topics`.
- Când adaugi o stare sau un câmp vizibil oaspetelui, actualizează `guest_view.py` și testele lui; nu pune acolo nimic neaprobat de host.
- Când adaugi o unealtă sau schimbi fluxul unui agent F2, actualizează și `mock_llm.py`.
- Simularea nu are voie să ocolească fluxul real: personalul și oaspetele simulați apelează aceleași metode ale serviciului ca endpointurile.
