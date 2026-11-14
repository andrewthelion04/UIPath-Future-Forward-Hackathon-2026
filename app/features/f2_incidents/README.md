# F2 — Incidente

Sesizare → triere → agentul întreabă mentenanța cât durează → propunere către host (relocare sau remediere, cu mesajele exacte pentru oaspete) → oferta de relocare și răspunsul oaspetelui → sarcina de reparație → aprobarea închiderii.

| | |
|---|---|
| **Ascultă** | `IssueReported`; `ApprovalDecided` pentru aprobările proprii (`incident_resolution`, `incident_closure`); `GuestOfferAnswered` (răspunsul oaspetelui la oferta de relocare, venit din conversație); `GuestMessageReceived` (doar ca să prelungească termenul ofertei); evenimentele interne din `events.py` |
| **Emite** | `ApprovalRequested`, `GuestMessageSent`, `IssueReported` (din `POST /incidents/report`); private: `IncidentDecided`, `IncidentResolved` și pașii interni |
| **Date deținute** | `f2_incidents`, `f2_incident_reports`, `f2_staff_messages`, `f2_tasks`; publică starea pentru oaspete în `guest_topics` (nucleu) |
| **Porturi** | `calendar`, `channel`, `llm` (prin `ctx.agent`) |
| **Agenți** | `f2_incidents.triage`, `.staff_liaison`, `.proposal`, `.closure`, `.sim_staff` (doar simulare); prompturile în `prompts/`, politicile mock în `mock_llm.py` |

## Endpointuri

| Endpoint | Scop |
|---|---|
| `GET /incidents` | Incidentele, cu rapoartele, conversația cu personalul și sarcinile (UI, extensie) |
| `GET /incidents/{id}` | Un incident |
| `POST /incidents/report` | Sesizare de la personal (aplicația personalului) |
| `POST /incidents/{id}/staff-reply` | Răspunsul personalului în conversație |
| `POST /incidents/{id}/guest-answer` | Răspunsul oaspetelui la oferta de relocare (`accept` / `stay`) |
| `POST /incidents/tasks/{id}/complete` | Sarcină terminată (idempotent) |

Toate primesc `?wait=true`, ca simulatorul.

## Stări

`awaiting_staff` → `awaiting_decision` → (`awaiting_guest`) → `relocated` | `remediation_scheduled` → `resolved`; respingerea propunerii → `dismissed`.

## Setări (`F2_*`, opționale)

| Setare | Implicit | Ce face |
|---|---|---|
| `F2_SIM_ENABLED` | `true` | Personalul și oaspetele sunt simulați automat |
| `F2_SIM_STAFF_DELAY_S`, `F2_SIM_GUEST_DELAY_S`, `F2_SIM_TASK_DELAY_S` | `0` | Pauza înainte de răspunsul simulat |
| `F2_SIM_GUEST_ANSWER` | `accept` | Ce răspunde oaspetele simulat: `accept`, `stay`, `off` |
| `F2_STAFF_REPLY_TIMEOUT_MIN` | `30` | După cât timp fără răspuns de la personal se propune fără estimare |
| `F2_GUEST_ANSWER_TIMEOUT_MIN` | `60` | După cât timp fără răspuns oaspetele e considerat că rămâne |
| `F2_FIX_MARGIN_HOURS` | `2` | Marja dintre sfârșitul reparației și check-in |
| `F2_INHOUSE_TOLERABLE_HOURS` | `3` | Cât poate aștepta un oaspete deja cazat |

**Pentru demo** recomandăm în `.env`: `F2_SIM_STAFF_DELAY_S=5`, `F2_SIM_GUEST_DELAY_S=8`, `F2_SIM_TASK_DELAY_S=10`. Pașii se văd atunci unul după altul în extensie și în pagina F2, iar UI-ul nu stă blocat.

## Scenarii de demo (pe mock)

- *Guest chat* → Lukas: „The air conditioning is broken” → mentenanța estimează ~30 h → recomandare: relocare la Herastrau → hostul aprobă → oaspetele (simulat) acceptă → reparația → aprobarea închiderii.
- *Staff report* → Calea Victoriei: „Water is leaking under the kitchen sink.” → ~2 h → recomandare: remediere, cu scuzele și estimarea pentru oaspete.
- Aceeași problemă raportată din nou (chiar în alte cuvinte) se atașează incidentului deschis.
- Cu `F2_SIM_GUEST_ANSWER=off`, oaspetele răspunde la ofertă din chat: copilotul (F3) îi răspunde la întrebări din ce publică F2 în `guest_topics` (detaliile proprietății, linkul anunțului, estimarea aprobată) și transmite un „da” sau „nu” clar prin `GuestOfferAnswered`. Cu valoarea implicită `accept`, răspunsul vine din simulare.
- Dacă discuția a fost predată hostului, pagina F2 are două butoane pe incidentele în `awaiting_guest`, cu care hostul notează ce a hotărât oaspetele.

Teste: `python manage.py test app/features/f2_incidents`
