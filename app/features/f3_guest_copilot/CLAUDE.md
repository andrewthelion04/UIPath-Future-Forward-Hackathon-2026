# F3 — Copilot oaspeți

## Scop
Răspunde la mesajele oaspeților consultând datele noastre: rezervarea, proprietatea, regulile hostului și calendarul. Rulează bucla comună a agentului cu uneltele proprii. Cererile care înseamnă o excepție sau o promisiune (de exemplu early check-in) merg întâi la host pentru aprobare, iar problemele semnalate de oaspete sunt trimise mai departe ca eveniment.

## Contract
- Ascultă: `GuestMessageReceived`, `ApprovalDecided` pentru aprobările proprii de tip `guest_request`.
- Emite: `GuestMessageSent` (prin `ChannelGateway`), `IssueReported` cu `reported_via="guest"` (preluat de F2), `ApprovalRequested` (prin serviciul de aprobări), `GuestOfferAnswered` (răspunsul oaspetelui la o ofertă publicată de alt feature în `guest_topics`).
- Date deținute: niciun tabel propriu. Folosește `messages` și `agent_runs` / `agent_steps` din nucleu. Promptul e în `prompts/system.md`.
- Porturi folosite: `llm` (prin `ctx.agent`), `calendar`, `channel`.
- Cereri de aprobare: `guest_request` cu tip și parametri validați (`early_check_in`, `late_check_out`, `extra_guests`, `pets`); la aprobare se revalidează condițiile și se trimite confirmarea canonică arătată hostului.

## Stare curentă
- Întrebările pentru host sunt acum persistate în inboxul comun `host_answers`, în locul notificărilor generice de follow-up. Hostul răspunde din **Approvals / Answers**; `ChannelGateway` trimite textul exact cu sender `host`. Datele și endpointurile sunt comune, fără dependențe între feature-uri.
- Agentul `f3_guest_copilot.reply` are uneltele: `get_booking_context`, `check_calendar`, `get_conversation`, `send_guest_reply`, `request_host_approval`, `answer_offer`, `report_issue`, plus `finish` din buclă.
- Întrebare informativă → citește contextul → răspunde.
- Early check-in / late check-out → verifică noaptea relevantă → cere sau reutilizează aprobarea exactă → răspunde în funcție de status. Confirmarea cere și verificarea actuală a unei nopți libere; un calendar ocupat sau necunoscut cere intervenția manuală a hostului.
- `get_booking_context` expune aprobările F3 ale rezervării, filtrate înainte de orice limită globală. Repetarea semantică reutilizează aceeași aprobare inclusiv după aprobare/refuz; schimbarea parametrilor sau a condițiilor relevante creează un scop nou.
- Mesajele și deciziile aceleiași rezervări rulează consecutiv, cu timeout de așteptare și eliberarea sigură a lockului la anulare. Rezervările diferite pot rula în paralel; contextul se recitește după așteptare.
- Sunt tratate cererile pentru animale și oaspeți suplimentari, informațiile despre parcare/acces și intențiile mixte. Datele lipsă, cererile nesuportate și excepțiile suplimentare ajung la host pentru follow-up, fără răspunsuri inventate.
- Problemă semnalată → `report_issue` → `IssueReported` → F2 deschide incidentul.
- Dacă agentul nu termină sau nu trimite niciun răspuns, hostul primește notificarea „reply manually”.
- Limite impuse în cod (`tools.py`): cel mult 2 răspunsuri și o singură cerere de aprobare per rulare, interval de calendar de cel mult 14 nopți.
- `send_guest_reply` verifică aprobarea, rezervarea, cererea și textul exact al confirmării. Filtrul suplimentar pentru text liber blochează formulări cunoscute de promisiune, fără a pretinde o garanție semantică pentru orice limbă sau parafrază.
- LLM-ul mock are o politică deterministă pe cuvinte-cheie în `mock_llm.py`.
- `scripts/eval_models.py` rulează 14 scenarii izolate, cu PMS mock chiar pentru modele OpenRouter explicite; raportează efectele, pașii, costul cunoscut și timpul în tabel/JSON/CSV.
- Verificat la 14 noiembrie 2026: `python manage.py test` — **390 passed**, inclusiv arhitectura și regresiile F1. Evaluator mock: **8/8** scenarii reușite. Smoke cu API și Streamlit reale pe mock: cerere repetată, aprobare livrată o singură dată, refuz animale, mesaj mixt incident + late check-out și afișarea conversațiilor/rulărilor în pagina F3.
- **Conversația despre un incident (14 noiembrie 2026)** (`tests/test_incident_conversation.py`):
  - `get_booking_context` întoarce `open_topics`: ce gestionează alt feature pentru acest sejur (ex. un incident), cu `facts` care pot fi spuse oaspetelui și, eventual, o ofertă în așteptare. Fără subiecte deschise, comportamentul e cel dinainte;
  - unealta nouă `answer_offer(topic_id, choice)`: verifică în cod că oferta e a acestei rezervări și încă așteaptă un răspuns, îl înregistrează o singură dată, trimite o confirmare de primire scrisă în cod (`offer_acknowledgement`) și publică `GuestOfferAnswered`. Rezultatul îl comunică feature-ul care a făcut oferta, cu textul aprobat de host;
  - `send_guest_reply` are `follow_up_note`: la predarea către host, notificarea spune ce vrea oaspetele;
  - **garda pe text liber** blochează acum o formulare permisivă doar în aceeași propoziție cu subiectul unei excepții (oră, animale, persoane în plus, bani, fumat, petreceri) sau, pentru formulările ferme („confirmed”, „approved”), și cu mutarea oaspetelui. „You can see the photos here” trece; „Your dog can join you” (parafraza din audit, D06) și „Your move is confirmed” nu trec. Toate cazurile vechi din `test_requests.py` rămân blocate;
  - politica mock acoperă aceleași situații: întrebări despre oferta primită (cu linkul), starea reparației fără o nouă sesizare, „da”/„nu” clar, predare către host, noua adresă după mutare. O întrebare nu e niciodată luată drept răspuns la ofertă.
- **Evaluat pe model real** (`anthropic/claude-sonnet-5.5`, 14 noiembrie 2026): `scripts/eval_models.py` are 14 scenarii (cele 8 inițiale plus 6 conversații despre un incident, pornite printr-un incident real prin API). Verificarea datei acceptă și forma naturală („June 3”), nu doar ISO.

## Decizii
- Copilotul nu cunoaște F2: vede doar `open_topics` și emite `GuestOfferAnswered`. Ce are voie să afle oaspetele hotărăște feature-ul care publică subiectul, nu promptul F3.
- Confirmarea de primire a unui răspuns la ofertă e text fix, trimis de unealtă înaintea evenimentului — ordinea mesajelor rămâne firească (întâi „am transmis”, apoi confirmarea aprobată), iar agentul nu poate afirma rezultatul.
- Garda pe text liber leagă formularea de subiect, în loc să interzică vorbe uzuale — pe model real vechea listă respingea răspunsuri inofensive („how the parking works”) și costa pași în plus.
- După decizia hostului pleacă o confirmare canonică sau un refuz fix, fără o nouă rulare de agent. Autoritatea este aprobarea persistentă, nu payload-ul evenimentului. Repetarea deciziei nu dublează livrarea în procesul curent.
- Mesajul oaspetelui intră în obiectiv între delimitatori, marcat ca date — reduce riscul ca textul oaspetelui să fie luat drept instrucțiune.
- Modelele locale `GuestRequest` și funcțiile din `requests.py` impun identitatea semantică, capacitatea, limitele orare și scopul aprobării, fără modificarea contractelor comune. Textele propuse de LLM nu pot înlocui confirmarea generată de server.
- Cheia `dedupe_key` include rezervarea, cererea canonică și un snapshot al condițiilor relevante (inclusiv numele oaspetelui). O cerere identică pendinte/aprobată/refuzată reutilizează rezultatul; un scop schimbat cere altă aprobare. Condițiile se recitesc și după apelul de calendar, pentru a detecta sincronizările concurente.
- `None` la limita early/late dezactivează excepția. Depășirea `max_guests` sau a limitelor orare nu poate fi aprobată. `pets_allowed=False` și oaspeții suplimentari în limita capacității sunt negociabili numai prin host; fumatul, petrecerile și alte tipuri nesuportate necesită follow-up manual, fără confirmare automată.
- Aprobarea pentru oaspeți suplimentari acordă permisiunea, fără să modifice numărul din rezervarea PMS. Cererile de animale/oaspeți se referă la șederea rezervată; cererile cu alte date necesită clarificare sau intervenție manuală.
- Lockul per rezervare este comun mesajelor și deciziilor; așteptarea implicită este jumătate din `handler_timeout_s`. Timeoutul comun continuă să includă și așteptarea. Istoricul exclude mesajele inbound sosite după mesajul procesat, dar include răspunsurile deja trimise.
- Un calendar gol, incomplet, cu zile duplicate sau cu proprietate greșită este necunoscut, nu liber. Cererea poate informa hostul despre această lipsă; confirmarea automată este blocată până la verificarea disponibilității.
- Informațiile lipsă sunt comunicate explicit prin `host_follow_up=True`, cu notificare. Filtrul textului liber este conservator și suplimentar: nu înlocuiește autorizarea structurată și nici evaluarea unui LLM real.
- Evaluarea folosește baze temporare și dată fixă; succesul cere efecte reale în mesaje/aprobări/evenimente. Costul cunoscut al pașilor persistați se păstrează la întreruperi, iar consumul incomplet este marcat `usage_complete=false`.
- Uneltele întorc doar câmpurile utile, nu obiecte întregi — contextul rămâne mic.

## Plan faza 1

**Zona permisă:** `app/features/f3_guest_copilot/`, `scripts/` (scriptul de evaluare), `app/adapters/openrouter/`.

**De implementat, în ordine:**

1. [x] **Aprobările în așteptare, vizibile agentului.** `get_booking_context` întoarce și cererile `guest_request` nedecise ale rezervării; promptul cere să nu se ceară din nou același lucru.
2. [x] **Coadă per conversație.** Două mesaje rapide pe aceeași rezervare se procesează pe rând, nu în paralel (un lock per `booking_id` în feature).
3. [x] **Mai multe tipuri de cereri:** oaspeți în plus, animale, parcare, indicații de acces. Excepțiile merg la host; informațiile vin din date.
4. [x] **Gardă în cod pentru promisiuni.** Confirmările cer aprobarea exactă și textul canonic; filtrul suplimentar al textului liber respinge promisiuni cunoscute, cu limita semantică documentată.
5. [x] **Scriptul de evaluare** (`scripts/eval_models.py`): 8 scenarii fixe, pe modelele date ca argument; raportează succes, pași, cost și timp. Rulează cap-coadă pe mock, fără cheie.

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

1. **Identificarea cererilor și deduplicare în cod.**
   - Cererile `guest_request` au un tip și parametri structurați, definiți în F3: rezervare, dată, oră, număr de persoane sau alte detalii relevante tipului cererii.
   - Identitatea unei cereri se construiește din `booking_id`, tip și parametri canonici, nu din textul liber al rezumatului. Aceeași cerere aflată în așteptare reutilizează aprobarea existentă, inclusiv după reformulare; parametrii diferiți reprezintă altă cerere.
   - Folosește `dedupe_key` al serviciului comun de aprobări. Definește și testează explicit comportamentul cererilor repetate după aprobare sau refuz, deoarece serviciul comun deduplică indiferent de status.
   - `get_booking_context` expune numai aprobările relevante rezervării, feature-ului F3 și tipului `guest_request`.

2. **Aprobarea autorizează numai cererea exactă.**
   - O aprobare pentru early check-in nu autorizează late check-out, animale sau alte excepții. Verificarea leagă aprobarea de rezervarea, tipul și parametrii confirmați de host.
   - Verificarea se aplică atât în `send_guest_reply`, cât și înaintea mesajului trimis de `on_guest_request_decision`; existența oricărei aprobări acordate pe aceeași rezervare nu este suficientă.
   - Înaintea confirmării, recitește rezervarea și condițiile relevante. Dacă rezervarea este anulată sau condițiile aprobate nu mai sunt valabile, nu trimite confirmarea și notifică hostul.

3. **Gardă pentru promisiuni, cu limite explicite.**
   - Pentru confirmările tipurilor de cereri suportate, folosește parametri validați și mesaje construite controlat sau textul exact aprobat de host.
   - Nu considera suficiente etichetarea răspunsului de către model drept „informativ” sau existența unui `approval_id` neverificat.
   - Regex-urile peste text liber pot fi o protecție suplimentară, dar nu reprezintă o garanție de detectare a tuturor promisiunilor, formulărilor și limbilor. Documentează limita și testează direct uneltele cu încercări de confirmare neautorizată; eroarea trebuie întoarsă modelului.

4. **Limite ferme și excepții negociabile.**
   - Impune în cod capacitatea `max_guests` și limitele orare, înaintea cererii de aprobare și înaintea confirmării. Aprobarea hostului nu ocolește automat aceste limite.
   - Definește explicit comportamentul când `early_check_in_earliest` sau `late_check_out_latest` este `None`; lipsa unei limite nu se interpolează într-o promisiune și nu se înlocuiește cu o oră inventată.
   - Precizează politica pentru `pets_allowed=False` și celelalte reguli: interdicție fermă sau excepție care poate fi cerută hostului. Distincția se aplică în unelte, nu doar în prompt.

5. **Procesare consecutivă pentru mesaje și deciziile hostului.**
   - Mesajele și răspunsurile după decizia hostului folosesc aceeași protecție per `booking_id`; rezervările diferite se pot procesa în paralel.
   - Citește contextul relevant în interiorul secțiunii protejate și păstrează identificarea mesajului procesat, fără amestec între conversații.
   - Tratează explicit timeoutul și anularea, eliberează întotdeauna lockul și notifică hostul dacă procesarea nu poate continua. Ține cont că timeoutul handlerului comun include și așteptarea lockului.

6. **Răspunsuri exclusiv din informațiile disponibile.**
   - Informațiile despre acces, parcare, costuri, Wi-Fi și regulile proprietății provin numai din datele returnate de unelte. Nu deduce existența parcării publice, includerea gratuită a unui loc sau existența unui ghid cu parola Wi-Fi din simpla prezență ori absență a unei facilități.
   - Pentru informații lipsă, recunoaște lipsa lor și solicită intervenția hostului. Un calendar indisponibil, incomplet sau cu eroare înseamnă disponibilitate necunoscută, nu nopți libere.
   - Ține cont de starea integrărilor după F1: calendarul OwnerRez poate fi citit; calendarul Hospitable rămâne stub. F3 folosește în continuare portul comun, fără importuri de adaptoare concrete.
   - Actualizează `mock_llm.py` odată cu uneltele și promptul, inclusiv pentru date lipsă și mesaje cu intenții multiple, astfel încât o problemă raportată să nu ascundă o cerere distinctă a oaspetelui.

7. **Evaluare izolată și teste după efectele reale.**
   - `scripts/eval_models.py` rulează 5–8 scenarii fixe cu baze temporare, date resetate și dată de referință fixă. PMS-ul rămâne mock inclusiv la evaluarea unui LLM real; scriptul nu trimite mesaje prin conexiuni PMS reale și nu modifică baza locală a aplicației.
   - Verifică succesul prin mesajele trimise, aprobările create sau reutilizate și evenimentele publicate, nu doar prin apelarea `finish`. Raportează pentru fiecare scenariu și model succesul, pașii, costul și timpul.
   - Testele F3 acoperă cereri identice reformulate, parametri modificați, aprobări pentru altă rezervare sau alt tip de cerere, limite depășite, mesaje simultane pe aceeași rezervare și pe rezervări diferite, intenții multiple și erori de unelte, LLM sau canal.
   - Evaluarea pe mock rulează fără chei și fără rețea. Evaluarea promptului pe modele reale este separată și nu este înlocuită de rezultatele politicii mock.

## Probleme deschise / TODO
- Filtrul textului liber nu poate demonstra absența oricărei promisiuni, pentru orice formulare sau limbă. Confirmările suportate au autorizare structurată strictă; precizia textului liber și răspunsurile în limba oaspetelui necesită evaluare pe modele reale.
- Politica mock recunoaște intenții după cuvinte-cheie în engleză; nu spune nimic despre calitatea promptului.
- Refuzul e un text fix; nota hostului nu ajunge la oaspete.
- Calendarul OwnerRez este citibil după F1; Hospitable rămâne stub. Fără disponibilitate verificabilă nu se trimite automat confirmarea unei excepții orare.
- Confirmările canonice și refuzul fix sunt în engleză; localizarea lor controlată rămâne de extins.
- Lockurile și deduplicarea livrării deciziilor sunt locale procesului unic. Nu există reluare durabilă după restart; o trimitere întreruptă după acceptarea de către PMS poate necesita verificare manuală.
- Agentul comun poate lăsa o rulare anulată cu status `running` în trace. F3 eliberează lockul și notifică hostul; evaluatorul păstrează metricile deja persistate și marchează consumul incomplet. Nucleul nu a fost modificat.
- Scenariile au fost rulate pe un singur model real (`anthropic/claude-sonnet-5.5`); comparația pe 2–3 modele din `CLAUDE.md` §5 nu a fost făcută.

## Note pentru Claude
- Promptul descrie obiectivul și condiția de terminare; păstrează formatul „ai terminat când…”. Nu adăuga „ești un asistent”.
- Orice unealtă nouă își moștenește parametrii din `ToolArgs` (are `reason`) și întoarce puțin text. Erorile se ridică normal: bucla le întoarce modelului.
- Cifrele vin din unelte. Nu pune ore, prețuri sau date în prompt.
- Când adaugi o unealtă sau schimbi fluxul, actualizează și `mock_llm.py`, altfel demo-ul pe mock nu o folosește.
- Nu importa F2. Pentru incidente există evenimentele `IssueReported` și `GuestOfferAnswered` și subiectele din `ctx.guest_topics`.
