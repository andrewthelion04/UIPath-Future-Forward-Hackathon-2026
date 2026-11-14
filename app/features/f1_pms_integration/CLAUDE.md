# F1 — Integrare PMS

## Scop
Transportul mesajelor dintre PMS și aplicație, în ambele direcții, fără logică de business. La intrare, primește webhook-urile conexiunii PMS (OwnerRez, Hospitable sau mock), le filtrează, le deduplică, le mapează la rezervarea internă și publică `GuestMessageReceived`. La ieșire, mesajele pleacă prin `ChannelGateway`, care alege adaptorul conexiunii PMS a rezervării.

## Contract
- Ascultă: niciun eveniment. Intrările sunt HTTP: `POST /webhooks/{provider}`, `POST /sim/guest-message` și `POST /f1/reprocess-unmapped`.
- Emite: `GuestMessageReceived` (același eveniment din webhook și din simulator; diferă doar `source`).
- Date deținute: `f1_webhook_log` (payload-urile brute și rezultatul fiecăruia), `f1_seen_messages` (cheile de deduplicare).
- Porturi folosite: `MessagingPort.parse_webhook` prin `ctx.pms`, `SimulationPort` prin `ctx.simulation`, `IdMapper` prin `ctx.ids` și `SyncService` prin `ctx.sync`.
- Cereri de aprobare: niciuna.

## Stare curentă
- Pipeline-ul din `inbound.py` rulează în ordine: salvare brută → parsare în adaptor → filtru `sender_type == "guest"` → deduplicare după ID-ul mesajului → mapare prin `IdMapper` → publicare.
- După autentificare, webhook-ul răspunde `200` cu rezultatul (`published`, `synced`, `duplicate`, `ignored_not_guest`, `ignored_not_message`, `unmapped`, `error`); handlerii evenimentelor rulează în background.
- Adaptoarele OwnerRez și Hospitable clasifică webhook-urile de rezervare. F1 sincronizează conexiunea; `SyncService` emite `BookingCancelled` o singură dată la tranziția unei rezervări cunoscute în `cancelled`.
- Mesajele `unmapped` se păstrează și notifică hostul. După sincronizare reușită sau mapare manuală se reîncearcă maparea în ordinea primirii, fără o nouă deduplicare; mesajele mapate ajung la F3 o singură dată.
- `WEBHOOK_TOKEN` păstrează verificarea `?token=`. `F1Settings`, încărcat prin `ctx.load_settings`, adaugă Basic Auth OwnerRez și filtrare CIDR opțională Hospitable; erorile de autentificare răspund `401`/`403`.
- `GET /f1/webhook-log` și UI arată payload-urile; testerul include mesaje și anulări pentru ambele PMS-uri reale. Butonul de reprocesare apelează `POST /f1/reprocess-unmapped`.
- Calendarul OwnerRez returnează fiecare noapte din `[start, end)`, ocuparea rezervărilor/blocărilor și tarifele spot, cu fallback `base_price` / `0.0`. `read_calendar=True`; `write_availability=False` rămâne explicit.
- Testele fără rețea acoperă autentificarea, concurența, reprocesarea și anulările cap-coadă pentru ambele adaptoare, plus calendarul cu `MockTransport`.
- Verificat la 14 noiembrie 2026: `python manage.py test` — **244 passed**. Smoke cu API și Streamlit pornite pe mock: mesaj → răspuns copilot, duplicat ignorat, mesaj nemapat → mapare manuală → publicare automată unică; butonul de reprocesare se actualizează corect.

## Decizii
- `ChannelGateway` stă în nucleu (`app/core/channel_gateway.py`), nu aici — îl folosesc toate feature-urile, iar un feature nu poate importa alt feature.
- Mesajul din simulator trece prin PMS-ul mock și prin același pipeline — așa simulatorul și un PMS real produc același eveniment pe același drum.
- Deduplicarea vine după filtru și înaintea mapării; o retrimitere rămâne `duplicate`. Reprocesarea citește numai rândurile `unmapped`, ocolește `f1_seen_messages` și actualizează condiționat rândul original la `published` cu `event_id`.
- Forma payload-ului o cunoaște doar adaptorul (`parse_webhook`, respectiv metoda opțională `is_reservation_webhook`), fără modificări în porturile comune. Mock ignoră în continuare `reservation.updated`.
- F1 instalează la commit wrapper-e pe instanțele `ctx.sync.sync` și `ctx.ids.link`. Lockul per provider se aplică tuturor sincronizărilor; un `ContextVar` suprimă reprocesările în timpul unui sync parțial/eșuat. După succes, sync-ul așteaptă reprocesarea.
- Mapările manuale păstrează API-ul sincron `link` și programează reprocesări urmărite, reunite per provider. O mapare sosită în timpul reprocesării cere încă o trecere; lifespan-ul routerului așteaptă taskurile la închidere.
- Reprocesările au lock separat per provider; publicarea și jurnalul folosesc numai ID-uri interne în eveniment. Garanția de publicare unică este pentru procesul unic existent; un crash între publicare și actualizarea jurnalului nu are recuperare tranzacțională.
- Calendarul citește `/v2/bookings` cu `property_ids`, `from`, `to`, filtrează local suprapunerea și nu declară nopți libere dacă ocuparea nu poate fi citită complet. `/v2/spotrates` are limite inclusive și override-uri rare; lipsa tarifelor are fallback explicit.
- Dacă metadatele OwnerRez sunt temporar indisponibile, se păstrează ultima monedă și ultimul tarif de bază confirmate în adaptor. Fără metadate confirmate, valorile implicite sunt EUR / `0.0`.
- Scrierea disponibilității rămâne stub conform pasului 5: [OwnerRez permite crearea blocărilor, dar nu anularea/ștergerea lor prin API](https://www.ownerrez.com/support/articles/api-quote-bookings). Nu expunem o operație care ar închide calendarul fără posibilitatea de redeschidere simetrică prin portul existent.

## Plan faza 1

**Zona permisă:** `app/features/f1_pms_integration/`, `app/adapters/ownerrez/`, `app/adapters/hospitable/`, plus testele lor din `tests/test_real_adapters.py`.

**De implementat, în ordine:**

1. [x] **Webhook-uri de rezervare.** Când adaptorul recunoaște un webhook de rezervare (creare, modificare, anulare), F1 pornește `ctx.sync.sync(provider)`. Așa `BookingCancelled` apare singur și F5 pornește fără sincronizare manuală. Clasificarea payload-ului (mesaj / rezervare / altceva) stă în adaptor; dacă e nevoie de o metodă nouă pe port, vezi regula 3.
2. [x] **Reprocesarea mesajelor nemapate.** Un mesaj `unmapped` se păstrează; după o sincronizare reușită a acelei conexiuni, se reîncearcă maparea și, dacă reușește, evenimentul se publică o singură dată.
3. [x] **Citirea calendarului pe PMS real** (`get_days`) în adaptorul OwnerRez, apoi activarea capabilității `read_calendar`. Blochează F2, F3 și F5 pe date reale, deci are prioritate.
4. [x] **Autentificarea webhook-urilor OwnerRez** (Basic auth configurat pe aplicația OAuth), pe lângă `WEBHOOK_TOKEN`.
5. [x] **Scrierea disponibilității — evaluată.** Rămâne stub conform opțiunii din plan: OwnerRez permite crearea blocărilor, dar nu anularea/ștergerea lor prin API. Decizia și limitarea sunt documentate la Decizii și Probleme deschise.

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

1. **Respectarea strictă a `tests/test_architecture.py` și a zonei permise:**
   - Nu modifica niciun fișier din `app/core/`, `app/ports/`, `app/routers/`, `app/adapters/mock/`, `app/config.py` sau `app/container.py`.
   - Orice setare nouă specifică F1 (ex. credențiale Basic Auth pentru webhook-ul OwnerRez sau subnetul IP opțional pentru Hospitable) se definește într-o subclasă `F1Settings(FeatureSettings)` cu `env_prefix="F1_"` în interiorul `app/features/f1_pms_integration/`, încărcată prin `ctx.load_settings(F1Settings)` (exact cum procedează F4 și F5).

2. **Pasul 1 (Webhook-uri de rezervare) — fără modificarea `app/ports/pms.py`:**
   - Adaugă pe `OwnerRezAdapter` și `HospitableAdapter` o metodă `is_reservation_webhook(self, payload: dict[str, Any]) -> bool`:
     - La **OwnerRez**: returnează `True` când `entity_type in ("booking", "reservation")` și `action in ("entity_create", "entity_update", "entity_delete")` (și doar dacă `self.capabilities.receive_webhooks` este `True`).
     - La **Hospitable**: returnează `True` când `action` / `event` / `type` începe cu `"reservation."` sau `"booking."` (ex. `reservation.created`, `reservation.updated`, `reservation.cancelled`).
   - În `InboundPipeline._process`: dacă `connection.parse_webhook(payload)` întoarce `None`, verifică dacă `getattr(connection, "is_reservation_webhook", None)` întoarce `True` (sau dacă payload-ul are `event` în `("reservation.created", "reservation.cancelled")`).
   - Când un webhook de rezervare este detectat, rulează `await ctx.sync.sync(provider)` (protejat cu un `asyncio.Lock` per provider ca să eviți sincronizări concurente) și apoi declanșează reprocesarea mesajelor `unmapped` pentru acel provider.
   - **Atenție la `test_inbound.py`:** în `test_other_payloads_are_ignored_but_always_answered_with_200`, `{"event": "reservation.updated"}` pe providerul `mock` așteaptă `status == "ignored_not_message"`. Păstrează acel test verde (fie activând sync-ul pe mock doar pentru `reservation.cancelled` / `reservation.created` sau când adaptorul expune explicit `is_reservation_webhook`, fie păstrând outcome-ul extern compatibil).

3. **Pasul 2 (Reprocesarea mesajelor `unmapped` după sincronizare) — fără editarea `SyncService`:**
   - Deoarece `SyncService.sync` (`app/core/sync_service.py`) nu emite un eveniment `SyncCompleted` și nu poate fi editat:
     - În `register(ctx)` din `app/features/f1_pms_integration/feature.py`, interceptează (wrap) metoda `ctx.sync.sync` (și opțional `ctx.ids.link`) astfel încât, după fiecare apel reușit la `ctx.sync.sync(provider)`, să se apeleze automat `await pipeline.reprocess_unmapped(provider)`.
     - Expune suplimentar și `POST /f1/reprocess-unmapped` în routerul F1 pentru declanșare manuală din UI.
   - **Cum trebuie să funcționeze `reprocess_unmapped(provider)`:**
     - Caută în `f1_webhook_log` intrările cu `provider = ?` și `outcome = 'unmapped'`, în ordinea primirii (`ORDER BY id ASC`).
     - Pentru fiecare intrare, re-parsează payload-ul cu `connection.parse_webhook(...)`, caută acum rezervarea prin `ctx.ids.to_internal(provider, RESERVATION, inbound.external_reservation_id)` și `ctx.world.get_booking(booking_id)`.
     - **Important:** NU mai trece prin `INSERT OR IGNORE INTO f1_seen_messages` (deoarece mesajul a fost deja pus în `f1_seen_messages` la prima sosire și ar ieși `duplicate`!). În schimb, dacă rezervarea este acum găsită, înregistrează mesajul în `ctx.conversations.record(...)`, publică `GuestMessageReceived` o singură dată și actualizează rândul din `f1_webhook_log` (`SET outcome = 'published', detail = 'reprocessed after sync', event_id = ? WHERE id = ? AND outcome = 'unmapped'`). Dacă tot nu e mapată, lasă `outcome = 'unmapped'`.

4. **Pasul 3 & 5 (Calendarul OwnerRez în `app/adapters/ownerrez/adapter.py`):**
   - Implementează `get_days(self, property_id: str, start: date, end: date) -> list[CalendarDay]` și setează `read_calendar=True` în `self.capabilities` pentru `OwnerRezAdapter`.
   - Returnează câte un `CalendarDay` pentru fiecare noapte din `[start, end)` (unde `nights = (end - start).days`).
   - Pentru a determina disponibilitatea și prețul în OwnerRez (compatibil cu Stage și ușor de testat cu `MockTransport`):
     - Tradu `property_id` intern în `external_id` prin `self._external(PROPERTY, property_id)`.
     - Citește rezervările/blocările proprietății în intervalul `[start, end)` (ex. `GET /v2/bookings` cu `property_ids`, `from`/`since_utc` sau filtrare pe `arrival < end` și `departure > start` pentru orice item al cărui `status` nu este `canceled` — atenție că aici atât `type == "booking"` cât și `type == "block"` ocupă calendarul!).
     - Citește prețurile pe zi (dacă există un endpoint dedicat precum `GET /v2/spotrates` sau `GET /v2/listings/{id}` / tariful din proprietate, cu fallback la `base_price` / `0.0` dacă nu e returnat), astfel încât F5 să primească prețuri valide pe fiecare zi.
   - Dacă scrierea disponibilității (`set_availability`) se poate face prin blocări în OwnerRez (sau dacă rămâne stub conform punctului 5 din plan), documentează clar decizia în `CLAUDE.md`.

5. **Pasul 4 (Autentificarea webhook-urilor în `router.py`):**
   - Păstrează verificarea actuală a parametrului `?token=` cu `ctx.settings.webhook_token` (nu strica `test_webhook_token_is_enforced_when_configured`).
   - În plus, adaugă în `F1Settings` (`env_prefix="F1_"`):
     - `ownerrez_webhook_username: str | None = None` și `ownerrez_webhook_password: SecretStr | None = None`: când sunt setate și `provider == "ownerrez"`, cere antetul HTTP `Authorization: Basic ...` (verificat cu `hmac.compare_digest`), altfel răspunde cu `401`/`403`.
     - `hospitable_webhook_allow_cidrs: str | None = None` (ex. `"38.80.170.0/24"`): când e setat și `provider == "hospitable"`, verifică IP-ul clientului cu modulul standard `ipaddress`, altfel răspunde cu `403`. Implicit e `None` (dezactivat) ca să nu blocheze testele și tunelurile locale.

6. **UI (`ui_page.py`) și Teste:**
   - În `app/features/f1_pms_integration/ui_page.py`, adaugă în `SAMPLES` payload-uri de test și pentru webhook-uri OwnerRez și Hospitable (mesaj și rezervare anulată) și un buton pentru `POST /f1/reprocess-unmapped` când există intrări cu `outcome == "unmapped"`.
   - Adaugă teste unitare și de integrare în `app/features/f1_pms_integration/tests/test_inbound.py` și `tests/test_real_adapters.py` pentru toate cele 5 puncte din plan.

## Probleme deschise / TODO
- Structura payload-ului de webhook Hospitable nu e confirmată: ajustează `parse_webhook` din adaptor după primul webhook real (payload-urile brute sunt în `f1_webhook_log`).
- OwnerRez: valorile câmpului `from_role` sunt presupuse; de verificat în Stage cu „Send as Guest”.
- OwnerRez Stage: validarea live a sincronizării, mesageriei, calendarului și `PATCH /v2/spotrates` necesită credențiale reale.
- Tarifele OwnerRez fără override folosesc `base_price` sau `0.0`; nu se calculează regulile sezoniere. Verificați prețul efectiv și moneda în Stage înaintea relistării reale.
- `set_availability` OwnerRez rămâne `CapabilityNotSupported`, cu `write_availability=False`: portul cere și redeschidere, iar API-ul nu permite anularea/ștergerea blocărilor create. Disponibilitatea se gestionează manual în PMS; operația completă se demonstrează pe mock.
- Filtrarea CIDR folosește `request.client.host`, nu antete forwarded. În spatele unui tunel/proxy, validați configurarea proxy-urilor de încredere înainte de activare.

## Note pentru Claude
- Fără logică de business aici: F1 nu decide ce se răspunde și nu citește reguli ale hostului.
- Nu importa adaptoare. Tot ce ține de forma unui PMS se schimbă în `app/adapters/<pms>/`, nu în acest folder.
- După autentificare, endpointul de webhook întoarce `200` inclusiv pentru payload invalid sau eroare de sync. Excepții: token greșit (`403`), Basic Auth invalid (`401`), IP Hospitable nepermis (`403`).
- ID-urile externe pot apărea doar în `f1_webhook_log` și `f1_seen_messages`, niciodată în evenimentul publicat.
