# F5 — Relistare după anulare

Anulare de ultim moment → comparabile din adaptor → preț propus → aprobarea hostului → calendar actualizat.

| | |
|---|---|
| **Ascultă** | `BookingCancelled`; `ApprovalDecided` pentru aprobările proprii (`relist_price`) |
| **Emite** | `ApprovalRequested`; rezultatul final e o notificare către host |
| **Date deținute** | `f5_relist_proposals` |
| **Endpointuri** | `GET /f5/proposals` |
| **Porturi** | `market`, `calendar` |
| **Setări** | `F5_UNDERCUT_PCT` (implicit 3), `F5_MARKET_TIMEOUT_S` (implicit 20) |

Prețul propus este cel mai ieftin anunț comparabil (aceleași facilități, același număr de dormitoare) minus `F5_UNDERCUT_PCT`, dar niciodată sub pragul dat de `max_relist_discount_pct` din regulile hostului. Calculul e în cod, în funcții pure.

Stările unei propuneri: `awaiting_approval` → `applied` | `rejected` | `failed`; `no_comparable` când piața nu are niciun anunț potrivit.

Când sursa de piață e Inside Airbnb, pagina de UI afișează creditul cerut de licența CC BY 4.0.

Teste: `python manage.py test app/features/f5_relisting`
