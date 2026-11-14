# F5 — Relistare după anulare

## Scop
La o anulare de ultim moment, caută în datele de piață cel mai ieftin anunț comparabil, cu aceleași facilități, și propune relistarea nopților eliberate la un preț puțin mai mic. Prețul e limitat de regulile hostului. După aprobarea hostului actualizează calendarul prin adaptor.

## Contract
- Ascultă: `BookingCancelled`, `ApprovalDecided` pentru aprobările proprii de tip `relist_price`.
- Emite: `ApprovalRequested` (prin serviciul de aprobări); rezultatul final ajunge la host ca notificare.
- Date deținute: `f5_relist_proposals`.
- Porturi folosite: `market` (comparabile), `calendar` (disponibilitate și preț).
- Cereri de aprobare: `relist_price` — schimbarea prețului pentru nopțile eliberate.

## Stare curentă
- Faza 1 implementată: eveniment de anulare → agent F5 (AgentRunner) → verificarea ferestrei/calendarului → căutare comparabile → propunere → aprobare → recitire calendar → aplicare.
- Agentul are unelte pentru inspectarea anulării, căutarea comparabilelor și pregătirea propunerii; timeline-ul păstrează apelurile și rezultatele. Politica LLM mock oferă același flux determinist fără chei.
- Rezultatele uneltelor sunt compacte; un model care termină prematur produce notificare de fallback. Sunt testate liste cu 100 de comparabile fără trunchierea JSON-ului.
- `F5_LAST_MINUTE_WINDOW_HOURS` suprascrie în ore fereastra hostului (implicit 7 zile); compararea folosește ora locală de check-in.
- Comparabil (`is_comparable`): are cel puțin facilitățile noastre, același număr de dormitoare, loc suficient, aceeași monedă.
- Preț: comparabilul minus `F5_UNDERCUT_PCT` stabilește factorul față de prețul de bază; fiecare tarif original este înmulțit cu același factor, cu prag și plafon propriu. Programul exact este în `nightly_prices`; câmpurile scalare rămân referințe compatibile cu API-ul vechi.
- Aprobare → verificare integrală a calendarului → `calendar.set_price` pe fiecare noapte → `applied`. Respingere → `rejected`. Nopțile ocupate sau tarifele schimbate produc `conflict`, fără scrieri. Calendarul imposibil de verificat produce `failed`.
- `expired` de la check-in, la ora hostului în `F5_TIMEZONE`; starea este persistată la citire sau decizie. Deciziile târzii nu modifică tarifele.
- Fallback-uri explicite, fiecare cu notificare: anulare care nu e de ultim moment, date de piață indisponibile sau timeout (`failed`), niciun comparabil (`no_comparable`), PMS care nu poate scrie prețul (`failed`, cu instrucțiuni de aplicare manuală).
- `GET /f5/proposals` și pagina de UI arată propunerile; pagina afișează și creditul sursei de date.
- Setări proprii: `F5_UNDERCUT_PCT`, `F5_MARKET_TIMEOUT_S`, `F5_CALENDAR_TIMEOUT_S`, `F5_TIMEZONE` (implicit Europe/Bucharest).
- Snapshot real Inside Airbnb Atena, 22 septembrie 2026, verificat: `app/adapters/inside_airbnb/local_data/listings.csv.gz`; opt rânduri reale reduse la coloanele necesare sunt versionate în `fixtures/`. Detalii de configurare și licență în README-ul adaptorului.
- Fixture-ul local curent mai conține patru rânduri sintetice de demo, identificate în README-ul adaptorului; nu sunt prezentate ca extrase din snapshot.
- **După auditul de integrare (14 noiembrie 2026)** (`tests/test_integration_guards.py`):
  - la aprobare se recitește limita de reducere a hostului: dacă între timp a scăzut și o noapte propusă ar coborî sub noul prag, propunerea devine `conflict` și nu se scrie nimic;
  - termenul de expirare urmează ora de check-in curentă când aceasta a fost mutată mai devreme (nu se prelungește dacă a fost mutată mai târziu);
  - la expirare, aprobarea din inbox e retrasă (`ctx.approvals.withdraw`), deci hostul nu mai poate aproba ceva fără efect;
  - un handler oprit la jumătate (timeoutul event bus-ului) consemnează `failed` cu nopțile deja scrise, în loc să lase propunerea în `awaiting_approval`;
  - recitirea calendarului și scrierea prețurilor stau sub lockul comun `ctx.calendar.lock(property_id)`, deci nu se intercalează cu o relocare F2 pe aceleași nopți;
  - o noapte cu preț pozitiv nu devine gratuită prin rotunjire: prețul propus e cel puțin 0,01;
  - un `ApprovalDecided` fără decizia salvată în `approvals` nu mai ajunge la handler (filtru comun în nucleu).

## Decizii
- AgentRunner orchestrează inspectarea, căutarea și propunerea; mock-ul reproduce selecția de unelte pentru demo.
- Modelul nu stabilește sume și nu scrie în calendar. Regulile sunt funcții pure în `pricing.py`: comparabilitate, preț, plafon, distribuție pe nopți și validarea snapshotului.
- Orice propunere cere aprobarea hostului; numai handlerul de decizie poate scrie tarifele după o recitire validă a calendarului.
- Se verifică aprobarea persistentă, scopul și opțiunea; se recitesc limita hostului și fiecare noapte înainte de scriere. Un check-in mutat mai devreme scurtează termenul. Timeoutul total persistă eșecul și progresul parțial înainte de propagare.
- Tariful minim este un cent inclusiv la reducere maximă de 100%. Simbolul `$` din Inside Airbnb nu înseamnă USD: Atena folosește EUR, iar conversia cere monedă USD explicită.
- Nu scriem disponibilitatea: la aprobare calendarul trebuie să confirme deja nopțile libere. Citirea nesuportată duce la fallback manual înainte de cererea de aprobare; nu inventăm tarife originale.
- Rotunjire monetară cu Decimal: la cent pentru tarife, în sus pentru praguri. Nu sunt acceptate sume negative/nefinite, nopți duplicate, monede diferite sau snapshoturi incomplete.
- Blocare locală pe proprietate și verificarea stării finale împiedică propuneri/scrieri repetate în procesul unic. Migrarea celor două coloane F5 este aditivă; propunerile vechi fără snapshot nu pot scrie tarife.
- Scrierile pe nopți nu sunt atomice în port; eșecul raportează progresul parțial, fără rollback/retry automat. Nu modificăm contractul comun.
- `dedupe_key` pe rezervare — o anulare produce o singură propunere, chiar dacă evenimentul ar sosi de două ori.

## Plan faza 1

**Zona permisă:** `app/features/f5_relisting/`, `app/adapters/inside_airbnb/`, `docs/DEMO.md` (scenariul de demo).

**De implementat, în ordine:**

1. [x] **Preț pe noapte.** Propunerea păstrează raportul dintre nopți din calendar (weekend mai scump) în loc de un preț unic; pragul hostului se aplică fiecărei nopți.
2. [x] **Verificare înainte de aplicare.** La aprobare, citește calendarul; dacă nopțile au fost rezervate între timp, nu scrie nimic și anunță hostul.
3. [x] **Expirarea propunerilor.** O propunere neaprobată până la check-in devine `expired`; aprobarea ei ulterioară nu mai schimbă calendarul.
4. [x] **Plafon la prețul obișnuit.** Prețul propus nu depășește prețul obișnuit al nopții (o relistare de ultim moment nu scumpește).
5. [x] **Inside Airbnb:** maparea facilităților completată pe un fișier real; creditul sursei rămâne vizibil.
6. [x] **Scenariul de demo** (`docs/DEMO.md`): pașii exacți prin cele cinci fluxuri, ce se arată la fiecare, și varianta mock ca rezervă.

**Criterii de acceptare:**
- vinerea și sâmbăta rămân mai scumpe decât nopțile din timpul săptămânii, iar nicio noapte nu coboară sub prag și nu urcă peste prețul ei obișnuit;
- o aprobare dată după ce nopțile s-au ocupat nu modifică calendarul;
- regulile noi sunt funcții pure, cu teste.

**Verificat:** importul arhivei reale Atena și maparea pe extrasul versionat, plus fluxurile mock prin API/UI. Scrierea prețului într-un PMS real depinde de adaptoarele din F1 și rămâne de verificat cu credențiale reale.

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
- Scrierea în calendarul Hospitable e stub; la OwnerRez doar prețul (`spotrates`), netestat.
- Datele de piață reale (Inside Airbnb) sunt snapshot-uri trimestriale dintr-un oraș configurat, nu prețuri live pentru orașul proprietății.
- Portul comun nu oferă verificare și scriere atomică (CAS) sau tranzacții între nopți; nu putem exclude o rezervare externă apărută după recitire. Suportul ar necesita un PR separat în contractele comune.
- Expirarea este evaluată la acces/decizie, fără worker: aprobarea dispare din inbox abia când propunerea e citită sau decisă după termen.
- Verificarea UI a fost automatizată cu Streamlit AppTest; aspectul vizual în browser și conexiunile PMS de producție rămân de verificat manual.
- Raporturile dintre nopți se păstrează în limita rotunjirii la cent; pragul rotunjit în sus are prioritate.

Implementarea și rezultatele verificărilor sunt documentate în `IMPLEMENTATION_SUMMARY.md`; scenariile sunt în `docs/DEMO.md`.

## Note pentru Claude
- Limitele de bani se impun în cod, nu într-un prompt. Orice regulă nouă de preț devine o funcție pură cu test.
- Cifrele vin din portul `market` și din calendar; nu le estima.
- Dacă sursa de piață are atribuire (`ctx.market.attribution`), ea trebuie să rămână vizibilă în UI (Inside Airbnb e CC BY 4.0).
- Eșecul unui adaptor se tratează aici, cu notificare; handlerul nu trebuie să ridice excepții pentru el.
