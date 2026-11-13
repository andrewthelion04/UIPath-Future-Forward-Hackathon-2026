# F3 — Copilot oaspeți

Mesaj de la oaspete → bucla agentului, cu unelte care citesc datele noastre → răspuns trimis.

| | |
|---|---|
| **Ascultă** | `GuestMessageReceived`; `ApprovalDecided` pentru aprobările proprii (`guest_request`) |
| **Emite** | `GuestMessageSent`, `IssueReported` (către F2), `ApprovalRequested` |
| **Date deținute** | niciun tabel propriu; promptul din `prompts/system.md` |
| **Endpointuri** | niciunul propriu; rulările se văd la `GET /agent/runs?feature=f3_guest_copilot` |
| **Porturi** | `llm`, `calendar`, `channel` |

Uneltele agentului: `get_booking_context`, `check_calendar`, `get_conversation`, `send_guest_reply`, `request_host_approval`, `report_issue`, `finish`.

Trei comportamente de încercat din *Guest chat*:

- o întrebare („What time is check-in?”) primește răspuns direct, din datele rezervării;
- o cerere de excepție („Could we check in early?”) ajunge la host ca aprobare, iar oaspetele află că hostul va confirma;
- o problemă („The air conditioning is broken”) pornește fluxul de incidente din F2.

Pe LLM-ul mock, ordinea pașilor vine din `mock_llm.py`. Cu `LLM_PROVIDER=openrouter` o alege modelul.

Teste: `python manage.py test app/features/f3_guest_copilot`
