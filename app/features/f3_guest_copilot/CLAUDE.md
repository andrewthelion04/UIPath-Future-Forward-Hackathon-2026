# F3 — Copilot oaspeți

## Scop
Răspunde la mesajele oaspeților consultând datele noastre: rezervarea, proprietatea, regulile hostului și calendarul. Rulează bucla comună a agentului cu uneltele proprii. Cererile care înseamnă o excepție sau o promisiune (de exemplu early check-in) merg întâi la host pentru aprobare, iar problemele semnalate de oaspete sunt trimise mai departe ca eveniment.

## Contract
- Ascultă: `GuestMessageReceived`, `ApprovalDecided` pentru aprobările proprii de tip `guest_request`.
- Emite: `GuestMessageSent` (prin `ChannelGateway`), `IssueReported` cu `reported_via="guest"` (preluat de F2), `ApprovalRequested` (prin serviciul de aprobări).
- Date deținute: niciun tabel propriu. Folosește `messages` și `agent_runs` / `agent_steps` din nucleu. Promptul e în `prompts/system.md`.
- Porturi folosite: `llm` (prin `ctx.agent`), `calendar`, `channel`.
- Cereri de aprobare: `guest_request` — excepție de la regulile hostului sau promisiune către oaspete; la aprobare se trimite răspunsul propus.

## Stare curentă
- Agentul `f3_guest_copilot.reply` are uneltele: `get_booking_context`, `check_calendar`, `get_conversation`, `send_guest_reply`, `request_host_approval`, `report_issue`, plus `finish` din buclă.
- Întrebare informativă → citește contextul → răspunde.
- Early check-in / late check-out → verifică noaptea relevantă în calendar → cere aprobarea hostului → îi spune oaspetelui că hostul va confirma. După decizie, oaspetele primește confirmarea sau un refuz politicos.
- Problemă semnalată → `report_issue` → `IssueReported` → F2 deschide incidentul.
- Dacă agentul nu termină sau nu trimite niciun răspuns, hostul primește notificarea „reply manually”.
- Limite impuse în cod (`tools.py`): cel mult 2 răspunsuri și o singură cerere de aprobare per rulare, interval de calendar de cel mult 14 nopți.
- LLM-ul mock are o politică deterministă pe cuvinte-cheie în `mock_llm.py`.

## Decizii
- După decizia hostului pleacă un mesaj fix (răspunsul propus sau refuzul), fără o nouă rulare de agent — e simplu, previzibil și nu costă un apel la model.
- Mesajul oaspetelui intră în obiectiv între delimitatori, marcat ca date — reduce riscul ca textul oaspetelui să fie luat drept instrucțiune.
- `send_guest_reply` e doar informativ; promisiunile trec prin `request_host_approval` — regula e în prompt, iar limitele numerice sunt în cod.
- Uneltele întorc doar câmpurile utile, nu obiecte întregi — contextul rămâne mic.

## Plan faza 1

**Zona permisă:** `app/features/f3_guest_copilot/`, `scripts/` (scriptul de evaluare), `app/adapters/openrouter/`.

**De implementat, în ordine:**

1. [ ] **Aprobările în așteptare, vizibile agentului.** `get_booking_context` întoarce și cererile `guest_request` nedecise ale rezervării; promptul cere să nu se ceară din nou același lucru.
2. [ ] **Coadă per conversație.** Două mesaje rapide pe aceeași rezervare se procesează pe rând, nu în paralel (un lock per `booking_id` în feature).
3. [ ] **Mai multe tipuri de cereri:** oaspeți în plus, animale, parcare, indicații de acces. Excepțiile merg la host; informațiile vin din date.
4. [ ] **Gardă în cod pentru promisiuni.** `send_guest_reply` refuză un text care confirmă o excepție când nu există o aprobare acordată pentru rezervare; eroarea se întoarce modelului.
5. [ ] **Scriptul de evaluare** (`scripts/eval_models.py`): 5–8 scenarii fixe, rulate pe modelele date ca argument; raportează succes, pași, cost și timp. Rulează și pe LLM-ul mock, ca să poată fi testat fără cheie.

**Criterii de acceptare:**
- a doua cerere identică nu creează a doua aprobare;
- două mesaje simultane produc două rulări consecutive;
- un răspuns care promite o excepție neaprobată nu pleacă spre oaspete;
- scriptul de evaluare rulează cap-coadă pe mock.

**De verificat manual, cu cheie OpenRouter:** toate scenariile pe 2–3 modele, alegerea modelului și ajustarea promptului din `prompts/system.md` după rezultate. Pe mock ordinea pașilor e scrisă de noi, deci nu spune nimic despre prompt.

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
- Codul nu poate verifica dacă textul din `send_guest_reply` conține o promisiune; regula stă doar în prompt. De evaluat cu un model real.
- Politica mock recunoaște intenții după cuvinte-cheie în engleză; nu spune nimic despre calitatea promptului.
- Două mesaje rapide pe aceeași rezervare pornesc două rulări în paralel.
- Agentul nu vede aprobările în așteptare ale aceleiași rezervări, deci poate cere din nou același lucru la un mesaj ulterior.
- Refuzul e un text fix; nota hostului nu ajunge la oaspete.
- Pe o conexiune PMS reală `check_calendar` întoarce eroare (calendarul nu se poate citi încă); agentul trebuie să se descurce fără.
- Scenariile fixe de evaluare pe 2–3 modele (`CLAUDE.md` §5) nu există încă.

## Note pentru Claude
- Promptul descrie obiectivul și condiția de terminare; păstrează formatul „ai terminat când…”. Nu adăuga „ești un asistent”.
- Orice unealtă nouă își moștenește parametrii din `ToolArgs` (are `reason`) și întoarce puțin text. Erorile se ridică normal: bucla le întoarce modelului.
- Cifrele vin din unelte. Nu pune ore, prețuri sau date în prompt.
- Când adaugi o unealtă sau schimbi fluxul, actualizează și `mock_llm.py`, altfel demo-ul pe mock nu o folosește.
- Nu importa F2. Pentru incidente există doar evenimentul `IssueReported`.
