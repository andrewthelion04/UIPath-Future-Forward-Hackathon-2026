# F1 — Integrare PMS

Actualizare relocări: adaptorul OwnerRez expune separat `move_reservations`, prin
`PATCH /v2/bookings/{id}` cu `property_id`. F2 verifică rezultatul și sincronizează
rezervarea înainte de confirmare. Aceasta nu activează scrierea generică a disponibilității.
Refuzurile canalelor conectate sunt tratate explicit, fără mutare fictivă locală.

Transportul mesajelor dintre PMS și aplicație. Doar transport, fără logică de business.

| | |
|---|---|
| **Ascultă** | niciun eveniment; intrările sunt `POST /webhooks/{provider}` și `POST /sim/guest-message` |
| **Emite** | `GuestMessageReceived` |
| **Date deținute** | `f1_webhook_log`, `f1_seen_messages` |
| **Endpointuri** | `POST /webhooks/{provider}`, `POST /sim/guest-message`, `GET /f1/webhook-log`, `POST /f1/reprocess-unmapped` |
| **Setări** | `WEBHOOK_TOKEN` (comun), `F1_OWNERREZ_WEBHOOK_USERNAME`, `F1_OWNERREZ_WEBHOOK_PASSWORD`, `F1_HOSPITABLE_WEBHOOK_ALLOW_CIDRS` (în `F1Settings`) |

Drumul unui payload: salvare brută → parsare în adaptorul conexiunii → filtru anti-buclă (`sender_type == "guest"`) → deduplicare după ID-ul mesajului → mapare la rezervarea internă → `GuestMessageReceived`.

Un webhook de rezervare recunoscut de adaptor pornește sincronizarea providerului și răspunde cu `status: "synced"`. Sincronizările manuale și cele din webhook au același lock per provider; o rezervare cunoscută care trece în `cancelled` produce un singur `BookingCancelled` prin serviciul comun. Mock păstrează comportamentul anterior pentru `reservation.updated` (`ignored_not_message`). Payload-urile autentificate, inclusiv cele invalide sau cu eroare de PMS, primesc în continuare HTTP `200`.

Mesajele `unmapped` se reprocesează automat după sincronizare reușită sau mapare manuală. Se recitesc în ordinea primirii, se reîncearcă direct maparea și se publică numai după ce există rezervarea internă. Cheile din `f1_seen_messages` rămân intacte; rândul inițial din jurnal devine `published`, cu `detail: "reprocessed after sync"` și `event_id`. Reprocesarea concurentă și repetată este protejată per provider în procesul unic al aplicației.

Reprocesare manuală, disponibilă și în UI când jurnalul afișează mesaje nemapate:

```bash
curl -X POST 'http://127.0.0.1:8000/f1/reprocess-unmapped?provider=ownerrez&wait=true'
```

Fără `provider`, se verifică toate conexiunile cu intrări nemapate. Răspunsul conține `provider`, `reprocessed` și `remaining`; `wait=true` adaugă `completed` după așteptarea handlerilor.

Autentificarea webhook-urilor se configurează în `.env`:

- `WEBHOOK_TOKEN`: păstrează verificarea parametrului `?token=` pentru toate conexiunile.
- `F1_OWNERREZ_WEBHOOK_USERNAME` și `F1_OWNERREZ_WEBHOOK_PASSWORD`: se setează împreună, la aceleași valori ca în aplicația OAuth OwnerRez. Cer `Authorization: Basic ...` pe `/webhooks/ownerrez`; credențialele invalide primesc `401`. Dacă există și token, ambele verificări sunt obligatorii.
- `F1_HOSPITABLE_WEBHOOK_ALLOW_CIDRS`: opțional, liste CIDR IPv4/IPv6 separate prin virgulă sau spații, de exemplu `38.80.170.0/24`. Clientul din afara listei primește `403`. Implicit filtrarea este dezactivată. Se verifică adresa conexiunii, fără încredere în antetele forwarded din request; în tunel/proxy trebuie verificată configurarea proxy-urilor de încredere.

Calendarul OwnerRez oferă câte un `CalendarDay` pentru fiecare noapte din `[start, end)`, cu ID intern de proprietate. Rezervările și blocările necancelate care se suprapun ocupă nopțile; și șederile proprietarului, quote holds și linked availability sunt luate în calcul. Tarifele vin din `/v2/spotrates`, cu fallback la `base_price` al proprietății sau `0.0`. O eroare la citirea ocupării oprește operația, fără a raporta nopți libere. Regulile sezoniere nu sunt calculate.

Scrierea disponibilității OwnerRez rămâne explicit stub (`CapabilityNotSupported`, `write_availability=False`), conform opțiunii din pasul 5: [API-ul permite crearea blocărilor, dar nu anularea/ștergerea lor](https://www.ownerrez.com/support/articles/api-quote-bookings), deci portul existent nu poate redeschide calendarul simetric. Disponibilitatea se modifică manual în OwnerRez; fluxul complet se poate demonstra pe mock. Citirea calendarului și scrierea prețului sunt active. Sincronizarea, câmpurile webhook-urilor, prețurile și moneda necesită încă validare cu contul Stage.

La o eroare temporară de citire a metadatelor, adaptorul păstrează ultima monedă și ultimul preț de bază confirmate. Dacă nu are încă metadate confirmate, folosește implicit EUR / `0.0`.

Ieșirea către oaspeți nu e aici: orice feature apelează `ctx.channel.send_to_guest(...)`, iar `ChannelGateway` din nucleu alege adaptorul conexiunii PMS a rezervării.

Încearcă din pagina *F1 · Pms Integration* a simulatorului sau direct:

```bash
curl -X POST http://127.0.0.1:8000/webhooks/mock -H "Content-Type: application/json" \
  -d '{"event": "message.created", "message": {"id": "M-1", "reservation_id": "MOCK-RES-1001", "sender_type": "guest", "body": "What time is check-in?"}}'
```

Testerul din UI include mesaje și anulări OwnerRez/Hospitable. Un payload de anulare cere ca rezervarea să fie deja anulată în PMS: webhook-ul declanșează citirea stării, nu modifică singur PMS-ul.

Teste fără rețea: `python manage.py test app/features/f1_pms_integration tests/test_real_adapters.py`. Verificare completă, inclusiv arhitectură: `python manage.py test`.
