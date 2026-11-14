# F5 — Relistare după anulare, faza 1

Anulare de ultim moment → snapshot de calendar → comparabile → tarife pe noapte →
aprobarea hostului → recitire calendar → scriere prin port, dacă propunerea mai este validă.

| Contract | Valoare |
|---|---|
| Ascultă | `BookingCancelled`; deciziile proprii `relist_price` |
| Emite | `ApprovalRequested` prin serviciul comun; notificări către host |
| Date deținute | `f5_relist_proposals`, inclusiv `nightly_prices` și `expires_at` |
| Endpoint | `GET /f5/proposals?limit=50` (1–200) |
| Porturi | `market`, `calendar` |
| Setări F5 | `F5_UNDERCUT_PCT=3`, `F5_MARKET_TIMEOUT_S=20`, `F5_CALENDAR_TIMEOUT_S=5`, `F5_TIMEZONE=Europe/Bucharest` |

Regulile pure sunt în `pricing.py`. Comparabilul are cel puțin facilitățile proprietății,
același număr de dormitoare, capacitate suficientă, aceeași monedă și preț finit pozitiv.
Factorul aplicat fiecărei nopți este:

```text
reference = round_to_cents(cheapest_comparable × (1 − undercut_pct / 100))
factor = min(1, max(1 − max_relist_discount_pct / 100, reference / property.base_price))
night_floor = ceil_to_cents(original_nightly_price × (1 − max_relist_discount_pct / 100))
night_price = min(original_nightly_price, max(night_floor, round_to_cents(original_nightly_price × factor)))
```

Sumele pozitive nu sunt rotunjite la zero. `base_price`, `floor_price` și `proposed_price`
rămân referințe compatibile cu API-ul vechi; **`nightly_prices` este programul efectiv
aprobat și aplicat**. UI-ul și sumarul aprobării arată fiecare noapte, limitele și expirarea.
Exemplu demo: `70, 70, 80.50` devine `65.96, 65.96, 75.85`; pentru o piață foarte ieftină,
pragurile sunt `52.50, 52.50, 60.38`. Rotunjirea la cent poate schimba ușor raportul exact.

Stări: `awaiting_approval` → `applied` / `rejected` / `failed` / `conflict` / `expired`;
`no_comparable` când niciun anunț nu corespunde. `conflict` apare și dacă tarifele au fost
schimbate între propunere și aprobare. Un calendar incomplet, cu altă monedă, nopți
duplicate sau prețuri invalide produce `failed`, fără scrieri. Nopțile sunt deja libere
după anulare, deci F5 nu le redeschide prin `set_availability`.

Expirarea folosește data check-in-ului și ora hostului (`check_in_time`, implicit 15:00)
în `F5_TIMEZONE`. Cu `FIXED_TODAY`, data simulată este combinată cu ora locală a ceasului.
Starea `expired` se persistă la citire sau procesarea deciziei; nu există worker de fundal.
Serviciul comun de aprobări păstrează propriile stări: o decizie târzie poate apărea
`approved` în inbox, dar propunerea F5 rămâne `expired`, fără efect în calendar.

Erorile și timeouturile au fallback explicit cu notificare și instrucțiuni manuale.
Scrierile pe nopți nu sunt tranzacționale în portul comun: dacă a doua scriere eșuează,
notificarea enumeră nopțile confirmate și cere verificarea PMS-ului. Nu se face rollback
sau retry automat. Verificarea înainte de scriere nu oferă CAS atomic cu un PMS extern;
o rezervare apărută după citire necesită suport nou în port, într-un PR separat.

Datele reale sunt configurate în [README-ul adaptorului](../../adapters/inside_airbnb/README.md),
cu arhiva în `app/adapters/inside_airbnb/local_data/listings.csv.gz`. Extrasul real compact
este versionat; arhiva completă rămâne locală. Atribuirea Inside Airbnb / CC BY 4.0
este afișată și păstrată în propunere.

Teste fără rețea: `python manage.py test app/features/f5_relisting`.
Demo: [docs/DEMO.md](../../../docs/DEMO.md).
Decizii și verificări: [IMPLEMENTATION_SUMMARY.md](IMPLEMENTATION_SUMMARY.md).
