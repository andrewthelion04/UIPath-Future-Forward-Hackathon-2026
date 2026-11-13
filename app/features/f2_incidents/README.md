# F2 — Incidente

Sesizare → incident → aprobarea hostului (remediere sau relocare) → acțiune.

| | |
|---|---|
| **Ascultă** | `IssueReported`; `ApprovalDecided` pentru aprobările proprii (`incident_resolution`) |
| **Emite** | `ApprovalRequested`, `GuestMessageSent`, `IncidentResolved` (privat) |
| **Date deținute** | `f2_incidents` |
| **Endpointuri** | `GET /incidents` |
| **Porturi** | `calendar`, `channel` |

Sesizarea vine de la personal (`POST /sim/staff-report`) sau de la un oaspete, prin F3. Opțiunea de relocare apare doar dacă există o proprietate compatibilă (același oraș, loc suficient, cel puțin la fel de multe dormitoare) liberă pe restul sejurului.

Stările unui incident: `awaiting_decision` → `remediation_scheduled` | `relocated` | `dismissed`.

Teste: `python manage.py test app/features/f2_incidents`
