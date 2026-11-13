# F1 — Integrare PMS

Transportul mesajelor dintre PMS și aplicație. Doar transport, fără logică de business.

| | |
|---|---|
| **Ascultă** | niciun eveniment; intrările sunt `POST /webhooks/{provider}` și `POST /sim/guest-message` |
| **Emite** | `GuestMessageReceived` |
| **Date deținute** | `f1_webhook_log`, `f1_seen_messages` |
| **Endpointuri** | `POST /webhooks/{provider}`, `POST /sim/guest-message`, `GET /f1/webhook-log` |
| **Setări** | `WEBHOOK_TOKEN` (comun, în `app/config.py`) |

Drumul unui payload: salvare brută → parsare în adaptorul conexiunii → filtru anti-buclă (`sender_type == "guest"`) → deduplicare după ID-ul mesajului → mapare la rezervarea internă → `GuestMessageReceived`.

Ieșirea către oaspeți nu e aici: orice feature apelează `ctx.channel.send_to_guest(...)`, iar `ChannelGateway` din nucleu alege adaptorul conexiunii PMS a rezervării.

Încearcă din pagina *F1 · Pms Integration* a simulatorului sau direct:

```bash
curl -X POST http://127.0.0.1:8000/webhooks/mock -H "Content-Type: application/json" \
  -d '{"event": "message.created", "message": {"id": "M-1", "reservation_id": "MOCK-RES-1001", "sender_type": "guest", "body": "What time is check-in?"}}'
```

Teste: `python manage.py test app/features/f1_pms_integration`
