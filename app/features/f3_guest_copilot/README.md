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

Pentru demonstrația cu rezervările seed din iunie 2026, pornește API-ul cu `FIXED_TODAY=2026-06-01` în mediul demo. Cererile pentru o sosire deja trecută sau o ședere încheiată nu primesc permisiuni noi automat.

Pe LLM-ul mock, ordinea pașilor vine din `mock_llm.py`. Cu `LLM_PROVIDER=openrouter` o alege modelul.

F3 procesează mesajele și deciziile hostului consecutiv pentru aceeași rezervare, cu un lock comun; rezervările diferite pot rula în paralel. Contextul este recitit după așteptare. Un timeout sau o anulare eliberează lockul și notifică hostul pentru intervenție manuală.

Cererile au parametri structurați în `requests.py`:

| Tip | Parametri și limite |
|---|---|
| `early_check_in` | Data sosirii; oră între limita minimă a hostului și ora standard, exclusiv ora standard |
| `late_check_out` | Data plecării; oră după ora standard, până la limita maximă a hostului |
| `extra_guests` | Numărul total de oaspeți, peste cel rezervat și cel mult capacitatea proprietății |
| `pets` | Numărul de animale pentru ședere; regula negativă cere excepție explicită de la host |

O limită early/late `None` dezactivează cererea. Pentru animale și oaspeți suplimentari se folosește șederea rezervată; datele care nu corespund ei cer clarificare sau follow-up manual. Permisiunea pentru oaspeți suplimentari nu actualizează rezervarea PMS: hostul face această modificare separat.

`get_booking_context` arată cererile F3 ale rezervării. O reformulare cu aceiași parametri reutilizează aprobarea, fără altă notificare de aprobare. După aprobare sau refuz, aceeași cerere reutilizează decizia; schimbarea parametrilor ori a condițiilor relevante produce o aprobare distinctă. Identitatea include un snapshot al rezervării și regulilor relevante.

Confirmările folosesc textul canonic prezentat hostului. `send_guest_reply` cere `approval_id`, cererea structurată exactă și același text; verifică statusul, feature-ul, rezervarea și scopul aprobării. La decizia hostului se verifică înregistrarea persistentă, fără a considera payload-ul evenimentului drept autoritate. Rezervarea și condițiile sunt recitite și după apelul de calendar, pentru a detecta modificări concurente din PMS.

Pentru early/late, confirmarea automată cere o noapte adiacentă verificată ca liberă. Un calendar ocupat, indisponibil sau incomplet împiedică această confirmare și trimite cazul la host. Citirea OwnerRez este disponibilă prin portul comun; calendarul Hospitable rămâne stub.

Textul liber informativ are un filtru suplimentar pentru promisiuni cunoscute, inclusiv ore standard atribuite greșit. Filtrul este conservator și euristic; nu garantează interpretarea oricărei parafraze sau limbi. Confirmările canonice au control strict al autorizării și sunt momentan în engleză. Promptul cere răspunsuri informative în limba oaspetelui.

Parcarea, accesul și Wi-Fi se descriu numai din informațiile disponibile. O facilitate listată nu dovedește gratuitatea, alocarea unui loc sau existența unui ghid cu parola. Datele lipsă și cererile nesuportate, precum discounturile, cer `host_follow_up=true` și notifică hostul. Un mesaj cu incident și cerere de excepție păstrează ambele intenții. Limitele rămân: două răspunsuri, o cerere de aprobare și cel mult 14 nopți de calendar per apel. Alte excepții din același mesaj cer follow-up manual.

Evaluare fără cheie, din rădăcina proiectului, cu Python-ul mediului virtual:

```powershell
.\.venv\Scripts\python.exe scripts/eval_models.py --models mock
.\.venv\Scripts\python.exe scripts/eval_models.py --models mock --format json --output f3-eval.json
```

Pe macOS/Linux, folosește `.venv/bin/python` în locul executabilului Windows. Sunt 8 scenarii: informații, cerere repetată, aprobare, refuz, animale, capacitate, date lipsă despre parcare și incident combinat cu early check-in. Fiecare scenariu folosește o bază temporară și data fixă `2026-06-01`, cu toate serviciile externe mock. Succesul este verificat prin mesaje, aprobări și evenimente, pe lângă terminarea rulărilor. Codul de ieșire este `0` la succes, `1` la scenarii nereușite și `2` la configurație invalidă.

Pentru evaluare reală, setează `OPENROUTER_API_KEY` și transmite explicit ID-urile modelelor prin `--models`. Numai LLM-ul devine real; PMS-ul, recenziile și datele de piață rămân mock, iar baza aplicației nu este atinsă. Rapoartele tabel/JSON/CSV includ pașii, costul cunoscut și timpul. Dacă o rulare este întreruptă, pașii LLM deja persistați sunt contabilizați, iar consumul necunoscut este marcat `usage_complete=false` / `partial`.

Verificarea pe modele reale rămâne manuală; politica mock nu validează calitatea promptului. Lockurile și deduplicarea deciziilor funcționează în procesul unic existent, fără recuperare durabilă după restart. O rulare anulată poate rămâne `running` în trace-ul agentului comun, dar hostul este notificat și conversația este deblocată.

Teste F3: `python manage.py test app/features/f3_guest_copilot`. Verificare completă, inclusiv arhitectură: `python manage.py test`.
