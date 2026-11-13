# F1 — Integrare PMS

## Scop
Transportul mesajelor dintre PMS și aplicație, în ambele direcții, fără logică de business. La intrare, primește webhook-urile conexiunii PMS (OwnerRez, Hospitable sau mock), le filtrează, le deduplică, le mapează la rezervarea internă și publică `GuestMessageReceived`. La ieșire, mesajele pleacă prin `ChannelGateway`, care alege adaptorul conexiunii PMS a rezervării.

## Contract
- Ascultă: niciun eveniment. Intrările sunt HTTP: `POST /webhooks/{provider}` și `POST /sim/guest-message`.
- Emite: `GuestMessageReceived` (același eveniment din webhook și din simulator; diferă doar `source`).
- Date deținute: `f1_webhook_log` (payload-urile brute și rezultatul fiecăruia), `f1_seen_messages` (cheile de deduplicare).
- Porturi folosite: `MessagingPort.parse_webhook` prin `ctx.pms`, `SimulationPort` prin `ctx.simulation`, `IdMapper` prin `ctx.ids`.
- Cereri de aprobare: niciuna.

## Stare curentă
- Pipeline-ul din `inbound.py` rulează în ordine: salvare brută → parsare în adaptor → filtru `sender_type == "guest"` → deduplicare după ID-ul mesajului → mapare prin `IdMapper` → publicare.
- Webhook-ul răspunde mereu `200` cu rezultatul (`published`, `duplicate`, `ignored_not_guest`, `ignored_not_message`, `unmapped`, `error`); handlerii rulează în background.
- Un mesaj pe o rezervare nemapată se loghează și hostul e notificat; nu se publică niciun eveniment.
- `WEBHOOK_TOKEN`, dacă e setat, e cerut ca `?token=` pe webhook.
- `GET /f1/webhook-log` și pagina de UI arată payload-urile primite; pagina are și un tester cu payload-uri de exemplu.
- Testat cu payload-uri mock în `tests/` și cu un payload Hospitable de exemplu în `tests/test_real_adapters.py`.

## Decizii
- `ChannelGateway` stă în nucleu (`app/core/channel_gateway.py`), nu aici — îl folosesc toate feature-urile, iar un feature nu poate importa alt feature.
- Mesajul din simulator trece prin PMS-ul mock și prin același pipeline — așa simulatorul și un PMS real produc același eveniment pe același drum.
- Deduplicarea vine după filtru și înaintea mapării — ordinea din `CLAUDE.md` §6; un mesaj nemapat nu mai e procesat la o retrimitere.
- Forma payload-ului o cunoaște doar adaptorul (`parse_webhook`) — F1 rămâne independent de PMS.

## Plan faza 1

**Zona permisă:** `app/features/f1_pms_integration/`, `app/adapters/ownerrez/`, `app/adapters/hospitable/`, plus testele lor din `tests/test_real_adapters.py`.

**De implementat, în ordine:**

1. [ ] **Webhook-uri de rezervare.** Când adaptorul recunoaște un webhook de rezervare (creare, modificare, anulare), F1 pornește `ctx.sync.sync(provider)`. Așa `BookingCancelled` apare singur și F5 pornește fără sincronizare manuală. Clasificarea payload-ului (mesaj / rezervare / altceva) stă în adaptor; dacă e nevoie de o metodă nouă pe port, vezi regula 3.
2. [ ] **Reprocesarea mesajelor nemapate.** Un mesaj `unmapped` se păstrează; după o sincronizare reușită a acelei conexiuni, se reîncearcă maparea și, dacă reușește, evenimentul se publică o singură dată.
3. [ ] **Citirea calendarului pe PMS real** (`get_days`) în adaptorul OwnerRez, apoi activarea capabilității `read_calendar`. Blochează F2, F3 și F5 pe date reale, deci are prioritate.
4. [ ] **Autentificarea webhook-urilor OwnerRez** (Basic auth configurat pe aplicația OAuth), pe lângă `WEBHOOK_TOKEN`.
5. [ ] **Scrierea disponibilității** în OwnerRez, dacă documentația o permite (blocări prin `POST /v2/bookings`); altfel rămâne stub, notat la probleme deschise.

**Criterii de acceptare:**
- o anulare sosită ca webhook produce exact un `BookingCancelled`, fără sincronizare manuală;
- un mesaj nemapat ajunge la F3 după sincronizare și nu se publică de două ori;
- webhook-ul răspunde în continuare `200` pentru orice payload primit;
- niciun ID extern nu apare în evenimente.

**De verificat manual, cu cont OwnerRez Stage:** sincronizarea, trimiterea unui mesaj, webhook-ul cu „Send as Guest”, valorile reale ale lui `from_role`, citirea calendarului, scrierea prețului. Ajustează câmpurile marcate „neconfirmat” în antetul adaptorului.

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
- Structura payload-ului de webhook Hospitable nu e confirmată: ajustează `parse_webhook` din adaptor după primul webhook real (payload-urile brute sunt în `f1_webhook_log`).
- OwnerRez: valorile câmpului `from_role` sunt presupuse; de verificat în Stage cu „Send as Guest”.
- OwnerRez autentifică webhook-urile cu Basic auth configurat pe aplicație; aici se verifică doar `WEBHOOK_TOKEN`.
- Un mesaj nemapat nu se reprocesează după ce apare maparea.
- Webhook-urile de rezervare (creare, modificare, anulare) nu sunt tratate; anulările se observă doar la sincronizare.
- Filtrarea pe IP-urile Hospitable (`38.80.170.0/24`) nu e implementată.

## Note pentru Claude
- Fără logică de business aici: F1 nu decide ce se răspunde și nu citește reguli ale hostului.
- Nu importa adaptoare. Tot ce ține de forma unui PMS se schimbă în `app/adapters/<pms>/`, nu în acest folder.
- Endpointul de webhook nu are voie să întoarcă altceva decât `200` pentru un payload primit (altfel PMS-ul retrimite). Singura excepție e tokenul greșit (`403`).
- ID-urile externe pot apărea doar în `f1_webhook_log` și `f1_seen_messages`, niciodată în evenimentul publicat.
