# F2 — Incidente

## Scop
Primește sesizări despre probleme la o proprietate, de la personal (prin aplicație) sau de la oaspete (prin F3), și deschide un incident. Propune hostului fie remedierea la timp, fie relocarea oaspetelui într-o proprietate liberă compatibilă. După decizia hostului execută acțiunea aleasă și anunță oaspetele.

## Contract
- Ascultă: `IssueReported` (sursă `staff` sau `guest`), `ApprovalDecided` pentru aprobările proprii de tip `incident_resolution`.
- Emite: `ApprovalRequested` (prin serviciul de aprobări), `GuestMessageSent` (prin `ChannelGateway`), `IncidentResolved` (eveniment privat, definit în `events.py`).
- Date deținute: `f2_incidents`.
- Porturi folosite: `calendar` (disponibilitatea candidaților, blocarea la relocare), `channel` (mesajul către oaspete).
- Cereri de aprobare: `incident_resolution`, cu opțiunile `remediate` și `relocate`. `relocate` apare doar dacă există o proprietate compatibilă liberă.

## Stare curentă
- Sesizare → incident `awaiting_decision` → cerere de aprobare.
- Rezervarea afectată e cea din eveniment sau, în lipsă, rezervarea confirmată în curs ori următoarea de la acea proprietate.
- Compatibilitate (`is_compatible`): același oraș, loc pentru toți oaspeții, cel puțin la fel de multe dormitoare. Dintre candidați se alege cel cu cele mai multe facilități comune, liber pe tot restul sejurului.
- `remediate` → `remediation_scheduled`, mesaj către oaspete, notificare cu persoana de contact (întâi personalul de mentenanță alocat proprietății).
- `relocate` → blocare în calendar la proprietatea țintă, mesaj către oaspete, `relocated`.
- Respingere → `dismissed`, fără nicio acțiune.
- `GET /incidents` și pagina de UI listează incidentele.

## Decizii
- Fără agent LLM în schelet — firul minim e determinist și ușor de testat; un agent se poate adăuga pentru formularea mesajelor sau alegerea candidatului.
- Nimic nu pleacă spre oaspete înainte de decizia hostului — relocarea și promisiunile cer aprobare (`CLAUDE.md` §3.7).
- Un calendar indisponibil sare candidatul, nu oprește fluxul — incidentul se deschide oricum, doar fără opțiunea de relocare.
- `IncidentResolved` rămâne privat până îl consumă alt feature.

## Plan faza 1

**Zona permisă:** `app/features/f2_incidents/`, `extension/`.

**De implementat, în ordine:**

1. [ ] **Deduplicarea sesizărilor.** O sesizare pentru aceeași proprietate, cât timp există un incident deschis cu descriere asemănătoare, se atașează incidentului existent în loc să deschidă altul.
2. [ ] **„Remediere la timp”.** Calculează timpul rămas până la check-in (sau marchează că oaspetele e deja cazat) și pune-l în rezumatul aprobării, cu o recomandare explicită: remediere dacă e timp, relocare dacă nu. Pragul e o setare proprie (`F2_...`).
3. [ ] **Starea finală „rezolvat”.** Endpoint `POST /incidents/{id}/resolve` și buton în pagina de UI; emite `IncidentResolved` și anunță oaspetele afectat.
4. [ ] **Sarcină pentru personal.** La remediere, creează o sarcină (tabel propriu) pentru persoana de contact, vizibilă în pagina F2 și marcabilă ca terminată. Fără tarife.
5. [ ] **Extensia Chrome:** adaugă un tab „Incidents” care citește `GET /incidents`.

**Criterii de acceptare:**
- două sesizări identice la rând produc un singur incident și o singură aprobare;
- aprobarea arată timpul până la check-in și recomandarea;
- nimic nu pleacă spre oaspete înainte de decizia hostului;
- `cd extension && npm run build && npm run compile` trec.

**De verificat manual:** extensia încărcată în Chrome (`chrome://extensions` → Load unpacked), aprobare și respingere din panou. Relocarea pe o conexiune PMS reală depinde de citirea calendarului (F1).

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
- Relocarea nu mută rezervarea în PMS; hostul trebuie să o mute manual (notificarea îi spune asta).
- Pe o conexiune PMS reală calendarul nu se poate citi încă, deci relocarea nu va fi oferită.
- Nu există „remediere la timp” măsurată: nu se estimează dacă reparația se termină înainte de check-in.
- Nu există deduplicare a sesizărilor: aceeași problemă raportată de două ori deschide două incidente.
- Personalul primește doar o mențiune în notificarea hostului, nu o sarcină proprie.
- Incidentul nu are o stare finală „rezolvat” confirmată de cineva.

## Note pentru Claude
- Nu modela tarife pentru personal sau reparații (`CLAUDE.md` §1).
- Regula de compatibilitate e o funcție pură (`is_compatible`): schimb-o acolo și adaugă cazul în `tests/`.
- Mesajele către oaspete pleacă cu `sender="host"`, fiindcă urmează unei decizii a hostului.
- F3 nu e cunoscut de acest feature: sesizările oaspeților ajung aici doar ca `IssueReported`.
