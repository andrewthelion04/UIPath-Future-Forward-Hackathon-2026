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
- Anulare → verificarea ferestrei de ultim moment (`last_minute_window_days` din regulile hostului) → comparabile din port → cel mai ieftin comparabil → preț propus → aprobare → calendar.
- Comparabil (`is_comparable`): are cel puțin facilitățile noastre, același număr de dormitoare, loc suficient, aceeași monedă.
- Preț (`propose_price`): comparabilul minus `F5_UNDERCUT_PCT` (implicit 3%), dar nu sub pragul `preț de bază × (1 − max_relist_discount_pct)`.
- Aprobare → `calendar.set_availability` + `calendar.set_price` → `applied`. Respingere → `rejected`, calendar neschimbat.
- Fallback-uri explicite, fiecare cu notificare: anulare care nu e de ultim moment, date de piață indisponibile sau timeout (`failed`), niciun comparabil (`no_comparable`), PMS care nu poate scrie prețul (`failed`, cu instrucțiuni de aplicare manuală).
- `GET /f5/proposals` și pagina de UI arată propunerile; pagina afișează și creditul sursei de date.
- Setări proprii: `F5_UNDERCUT_PCT`, `F5_MARKET_TIMEOUT_S`.

## Decizii
- Fără LLM în schelet — prețul trebuie să vină din date și din reguli, deci e calculat în cod.
- Regulile de preț sunt funcții pure (`is_comparable`, `cheapest_comparable`, `floor_price`, `propose_price`) — ușor de testat și de schimbat.
- Dacă PMS-ul nu poate seta disponibilitatea, se continuă cu prețul — la un PMS real nopțile se redeschid singure la anulare.
- `dedupe_key` pe rezervare — o anulare produce o singură propunere, chiar dacă evenimentul ar sosi de două ori.

## Plan faza 1

**Zona permisă:** `app/features/f5_relisting/`, `app/adapters/inside_airbnb/`, `docs/DEMO.md` (scenariul de demo).

**De implementat, în ordine:**

1. [ ] **Preț pe noapte.** Propunerea păstrează raportul dintre nopți din calendar (weekend mai scump) în loc de un preț unic; pragul hostului se aplică fiecărei nopți.
2. [ ] **Verificare înainte de aplicare.** La aprobare, citește calendarul; dacă nopțile au fost rezervate între timp, nu scrie nimic și anunță hostul.
3. [ ] **Expirarea propunerilor.** O propunere neaprobată până la check-in devine `expired`; aprobarea ei ulterioară nu mai schimbă calendarul.
4. [ ] **Plafon la prețul obișnuit.** Prețul propus nu depășește prețul obișnuit al nopții (o relistare de ultim moment nu scumpește).
5. [ ] **Inside Airbnb:** maparea facilităților completată pe un fișier real; creditul sursei rămâne vizibil.
6. [ ] **Scenariul de demo** (`docs/DEMO.md`): pașii exacți prin cele cinci fluxuri, ce se arată la fiecare, și varianta mock ca rezervă.

**Criterii de acceptare:**
- vinerea și sâmbăta rămân mai scumpe decât nopțile din timpul săptămânii, iar nicio noapte nu coboară sub prag și nu urcă peste prețul ei obișnuit;
- o aprobare dată după ce nopțile s-au ocupat nu modifică calendarul;
- regulile noi sunt funcții pure, cu teste.

**De verificat manual:** un `listings.csv.gz` real de la Inside Airbnb pentru orașul ales (`MARKET_PROVIDER=inside_airbnb`). Scrierea prețului într-un PMS real depinde de adaptoarele din F1.

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
- Prețul propus e același pentru toate nopțile; nu ține cont de weekend sau de sezon.
- Nu se verifică dacă nopțile au fost între timp rezervate din nou înainte de aplicare.
- O propunere neaprobată nu expiră.
- Dacă cel mai ieftin comparabil e mai scump decât prețul nostru de bază, propunerea poate fi peste prețul obișnuit.

## Note pentru Claude
- Limitele de bani se impun în cod, nu într-un prompt. Orice regulă nouă de preț devine o funcție pură cu test.
- Cifrele vin din portul `market` și din calendar; nu le estima.
- Dacă sursa de piață are atribuire (`ctx.market.attribution`), ea trebuie să rămână vizibilă în UI (Inside Airbnb e CC BY 4.0).
- Eșecul unui adaptor se tratează aici, cu notificare; handlerul nu trebuie să ridice excepții pentru el.
