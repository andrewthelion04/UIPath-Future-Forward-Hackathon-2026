# Demo StayPilot AI — cele cinci fluxuri

## Demo-ul de prezentare: agentul pe model real (aproximativ 8 minute)

Acesta e scenariul recomandat pentru prezentare. Agenții rulează pe modelul real
(OpenRouter), iar PMS-ul, recenziile și piața rămân simulate, deci nimic nu ajunge la
oaspeți reali. Întregul scenariu costă aproximativ **0,35 USD** de credit și a fost rulat
cap-coadă pe API-ul pornit efectiv (14 noiembrie 2026, `anthropic/claude-sonnet-5.5`).
Secțiunile numerotate de mai jos descriu aceleași fluxuri în detaliu, pe mock.

**Pregătire.** În `.env`:

```
LLM_PROVIDER=openrouter
OPENROUTER_API_KEY=<cheia ta>
LLM_MODEL=anthropic/claude-sonnet-5.5
FIXED_TODAY=2026-06-01
F2_SIM_GUEST_ANSWER=off      # oaspetele răspunde din chat, nu simulat
F2_SIM_STAFF_DELAY_S=2
F2_SIM_TASK_DELAY_S=2
```

Pornește `python manage.py api` și, în alt terminal, `python manage.py ui`. Înainte de
prezentare apasă **Simulator → Reset world**. Ține deschise două file: **Simulator** și
**Approvals** (sau extensia Chrome pentru aprobări). Un răspuns al agentului durează
5–15 secunde; un incident ajunge la decizia hostului în aproximativ 20 de secunde.

| # | Ce faci | Ce arăți |
|---|---|---|
| 1 | **Guest chat → Emma Johnson**: `Hi! What time is check-in, and is there wifi?` | Răspuns din datele rezervării (ora, lockbox, wifi), fără aprobare. Agentul spune singur ce nu știe (parola). |
| 2 | Emma: `Could we check in early, around noon?` Apoi **Approvals → Approve**. | Agentul nu promite nimic: cere aprobarea, cu noaptea dinainte verificată în calendar. După aprobare pleacă textul exact pe care l-a văzut hostul. |
| 3 | **Staff report**: Maria Ionescu, **Calea Victoriei Apartment**, `The air conditioning is not working`. | În **F2 · Incidents**: agentul întreabă mentenanța, primește „about 30 hours” și propune relocarea. |
| 4 | **Approvals → Incident at Calea Victoriei Apartment**: citește recomandarea și textele, alege **Offer relocation to Herastrau Family Flat**, **Approve**. | Recomandarea e calculată în cod (30 h față de 3 h tolerabile). Hostul aprobă textele exacte, inclusiv linkul anunțului. |
| 5 | **Guest chat → Lukas Mueller**: `Do you have photos of the other apartment? And does it have parking?` | Agentul răspunde din detaliile ofertei, trimite linkul, spune ce nu știe despre parcare. Nu decide nimic: incidentul rămâne `awaiting_guest`. |
| 6 | Lukas: `Honestly, I'd rather get a refund for tonight.` (opțional) | Fără promisiuni: îi spune că îl contactează gazda și notifică hostul cu ce vrea oaspetele. |
| 7 | Lukas: `Ok, sounds good, let's move there.` | Confirmare de primire de la copilot, apoi confirmarea mutării cu textul aprobat de host. Incidentul devine `relocated`; calendarul țintei e blocat. |
| 8 | **Approvals → Close the incident…**, **Close the incident**. Apoi Lukas: `What's the address of the new place again?` | Rezumatul de închidere scris de agent. După mutare, agentul răspunde cu noua adresă. |
| 9 | **Review analysis → Old Town Studio → Analyse reviews**, apoi **F4**. | Recomandări prioritizate, fiecare cu citate verificate în cod. |
| 10 | **Cancel booking → Emma Johnson**, apoi **Approvals → Relist at 65.96–75.85 EUR/night**. | Preț calculat în cod din comparabile, cu limita hostului; fără LLM. |
| 11 | **Agent dashboard**. | Indicatorii (răspunsuri autonome față de mesaje aprobate, intervenții ale regulilor din cod, timp median de răspuns, cost). La **Flow story** alege incidentul: fiecare pas cu motivul lui, ce a văzut și ce a ales hostul, legătura cu mesajul din chat care a acceptat oferta. **Decision log** arată deciziile din toate fluxurile; filtrează pe **Code rule** pentru corecțiile făcute de cod. |

**De reținut pentru prezentare:**

- Pasul 2 trebuie făcut înaintea pasului 10: anularea Emmei îi închide conversația.
- Mentenanța e simulată: duratele vin din scenariul simulării (AC ≈ 30 h, scurgere ≈ 2 h,
  bec ≈ 30 min), iar modelul doar le formulează. Pentru ramura „reparăm pe loc”, raportează
  `Water is leaking under the kitchen sink.`: recomandarea devine **Fix it in place**.
- Răspunsurile modelului diferă ușor de la o rulare la alta; efectele (aprobări, stări,
  mesaje aprobate) sunt aceleași. `python scripts/eval_models.py --models
  anthropic/claude-sonnet-5.5` rulează 14 scenarii și le verifică după efecte.
- **Rezervă fără rețea:** `LLM_PROVIDER=mock` și repornește API-ul. Aceiași pași merg pe
  politica deterministă, cu aceleași fraze ale oaspetelui.

## Pregătire reproductibilă

Din rădăcina proiectului, instalează dependențele cu `python manage.py setup` dacă
mediul lipsește. Pe Windows folosește un Python nativ; în mediul verificat am creat
`.venv` cu `py -3.11 -m venv .venv`, apoi am instalat `requirements-dev.txt`.

Pentru reproducere, suprascrie configurația doar în terminalul de demo. Nu sunt
necesare chei sau servicii externe și nu trebuie modificat `.env`. Fixture-ul conține
opt rânduri reale din Atena și patru sintetice. Prețurile Atena sunt în EUR;
simbolul `$` este un artefact al exportului, nu o indicație USD.

```powershell
$env:PMS_PROVIDER='mock'
$env:LLM_PROVIDER='mock'
$env:REVIEWS_PROVIDER='mock'
$env:MARKET_PROVIDER='inside_airbnb'
$env:INSIDE_AIRBNB_LISTINGS_CSV='app/adapters/inside_airbnb/fixtures/athens_2026-09-22.sample.csv'
$env:MARKET_CITY='Athens'
$env:MARKET_CURRENCY='EUR'
$env:MARKET_USD_TO_CURRENCY_RATE='1'
$env:FIXED_TODAY='2026-06-01'
$env:F5_LAST_MINUTE_WINDOW_HOURS='168'
$env:F2_SIM_ENABLED='true'
$env:F2_SIM_STAFF_DELAY_S='0'
$env:F2_SIM_GUEST_DELAY_S='0'
$env:F2_SIM_TASK_DELAY_S='0'
$env:F2_SIM_GUEST_ANSWER='accept'
python manage.py api
```

În alt terminal: `python manage.py ui`. Deschide `http://localhost:8501`; API-ul este
la `http://127.0.0.1:8000`, cu documentație la `/docs`. Pentru porturi alternative:
`python manage.py api --port 8015 --no-reload`, apoi setează
`$env:STAYPILOT_API_URL='http://127.0.0.1:8015'` în terminalul UI și pornește
`python manage.py ui --server.port 8515`. Aceste porturi au fost folosite la verificare.

În **Simulator → Reset world**, confirmă și apasă **Reset world**. Se resetează lumea
demo și datele generate de fluxuri, inclusiv propunerile/statusurile F5 și aprobările.
Reîncarcă alte pagini deja deschise ca să nu mai afișeze informații dinaintea resetului.
În **World data**, verifică șase proprietăți (toate în București), șapte rezervări și data simulată
1 iunie 2026. Păstrează ordinea de mai jos; Emma trebuie să rămână activă până la F5.
Setările F2 de mai sus fac personalul, răspunsul oaspetelui și reparația simulate
imediat: după fiecare acțiune rămâne de luat doar următoarea decizie a hostului.
Pentru a vedea separat pașii intermediari, poți reporni API-ul cu întârzieri de
5/8/10 secunde pentru personal/oaspete/sarcină și reîncărca paginile pe parcurs.

## 1. F1 — transport prin PMS

1. În **Simulator → Guest chat**, alege **Emma Johnson / Old Town Studio**.
2. Apasă **Ask about check-in**. Apare mesajul `Hi! What time is check-in, and is there wifi?`
   și răspunsul copilotului, calculat din datele proprietății.
3. În pagina **F1**, arată jurnalul de webhook: mesajul intră prin adaptorul mock,
   este filtrat și mapat la ID-uri interne. `source_channel=airbnb` descrie canalul,
   iar `pms_provider=mock` descrie transportul.
4. În **Events**, arată `GuestMessageReceived` și `GuestMessageSent`. Transportul
   este responsabilitatea F1; răspunsul din conversație este produs de F3.

Verificare REST echivalentă: `POST /sim/guest-message?wait=true`, cu
`{"booking_id":"<Emma internal ID>","text":"Hi! What time is check-in, and is there wifi?"}`.
La succes apare `outcome=published`; în istoricul rezervării există mesaj de la
`guest`, urmat de `copilot`.

Opțional, în **F1 · PMS integration → Webhook tester**, păstrează conexiunea `mock`
și trimite prin **POST to webhook**, pe rând, **Guest message**,
**Same message again (duplicate)** și **Our own message echoed back (anti-loop)**.
În **Inbound log** rezultatele sunt `published`, `duplicate` și `ignored_not_guest`.
**Unmapped reservation** produce `unmapped` și o notificare; în UI apare
**Reprocess unmapped messages**. Reprocesarea fără o mapare validă lasă mesajul
nemapat; după o sincronizare sau mapare care îl identifică, intrarea originală se
publică o singură dată. Exemplele de anulare OwnerRez/Hospitable sincronizează
starea PMS-ului; rezervarea trebuie anulată mai întâi în PMS. Mock ignoră în continuare
**Not a message event**, iar anularea mock din scenariul F5 folosește simulatorul.

## 2. F2 — incident, mentenanță, relocare și închidere

1. În **Simulator → Staff report**, alege **Maria Ionescu** la **Reported by** și
   **Calea Victoriei Apartment** la **Property**. În **What is wrong?** scrie
   `The air conditioning is not working`, apoi apasă **Send report**.
2. În **F2 · Incidents**, incidentul ajunge la `awaiting_decision`. Agentul a întrebat
   mentenanța, iar **Conversation with staff** arată întrebarea către **Andrei Stan**
   și răspunsul simulat. **Fix estimate** este **30 h**, oaspetele **Lukas Mueller**
   este deja cazat, iar recomandarea este `relocate`. Cu întârzieri de simulare se
   poate vedea mai întâi `awaiting_staff`.
3. În **Approvals**, deschide **Incident at Calea Victoriei Apartment**, de tip
   `incident_resolution`. Sumarul arată răspunsul mentenanței, estimarea, sejurul
   afectat și **Recommendation: Offer relocation**. Prima opțiune este
   **Offer relocation to Herastrau Family Flat**; alternativa este **Fix it in place**.
   Citește textele exacte **Guest will receive**, **If the guest accepts** și
   **If the guest stays**: acestea sunt mesajele pe care le autorizează hostul.
4. Selectează **Offer relocation to Herastrau Family Flat** și apasă **Approve**.
   Se trimite întâi oferta (cu linkul anunțului), apoi se așteaptă răspunsul oaspetelui
   (`awaiting_guest`). Cu setările de mai sus răspunsul este automat `accept`. Cu
   `F2_SIM_GUEST_ANSWER=off`, răspunsul vine din **Guest chat → Lukas Mueller**: copilotul
   răspunde la întrebări despre proprietatea oferită și, la un „da” sau „nu” clar,
   transmite răspunsul către F2. În conversația lui Lukas apar oferta și confirmarea
   exactă din aprobare.
5. În F2, incidentul devine `relocated`, cu **Guest accepted the move**. În **Tasks**,
   sarcina de reparație pentru Andrei este creată și, pe simularea fără întârzieri,
   apare deja terminată, cu nota de finalizare. În **World data → Calendar**, alege
   **Herastrau Family Flat**: nopțile 1–3 iunie sunt blocate, 4 iunie rămâne liberă.
   Mutarea rezervării în PMS real rămâne manuală, conform notificării.
6. Revino în **Approvals**: există acum o **a doua aprobare**, de tip `incident_closure`,
   cu titlul **Close the incident at Calea Victoriei Apartment?**. Sumarul reunește
   istoricul, alegerea hostului, acceptarea oaspetelui și sarcina terminată. Apasă
   **Close the incident**. Abia acum F2 arată `resolved` și emite `IncidentResolved`.
   Oaspetele relocat nu mai primește un mesaj despre repararea locuinței originale.

Pentru ramura de remediere, într-o demonstrație separată după cele cinci fluxuri,
resetează lumea și raportează `Water is leaking under the kitchen sink.` la Calea
Victoriei. Estimarea este **2 h**, iar **Fix it in place** este prima opțiune recomandată.
Aprobarea trimite scuzele cu estimarea și creează sarcina; starea rămâne
`remediation_scheduled` până la aprobarea separată **Close the incident**. Această
închidere trimite și mesajul aprobat că problema a fost reparată. Respingerea închiderii
cu o notă creează o sarcină de follow-up, nu închide incidentul.

Pentru un oaspete care refuză mutarea, repornește demo-ul cu
`F2_SIM_GUEST_ANSWER=stay`: după oferta aprobată, oaspetele primește scuzele și estimarea,
se repară în loc și se cere tot aprobarea de închidere. O sesizare repetată despre
aceeași problemă se atașează incidentului deschis și nu reia conversația cu mentenanța.

## 3. F3 — cerere cu aprobare

1. Revino la **Guest chat → Emma Johnson** și apasă **Request early check-in**.
2. Conversația arată cererea; **Agent timeline** arată citirile de rezervare,
   reguli și calendar, motivele uneltelor și cererea de aprobare. Nu se promite
   check-in anticipat înaintea deciziei hostului.
3. În **Approvals**, aprobă cererea de early check-in. Înapoi în conversație apare
   confirmarea hostului (`confirmed an early check-in`) pentru **12:00**, după recitirea
   condițiilor și verificarea nopții adiacente. În inbox cererea dispare
   din lista pending și apare în **Decided approvals**.

O reformulare cu aceiași parametri reutilizează cererea și decizia, fără aprobare
duplicată. Un calendar ocupat/incomplet sau schimbarea condițiilor împiedică confirmarea
automată și trimite cazul la host. Pentru o problemă combinată cu o cerere, F3 poate
emite și `IssueReported` către F2; folosește altă rezervare decât Emma dacă vrei să
păstrezi scenariul F5 de mai jos neschimbat.

## 4. F4 — analiza recenziilor

1. În **Simulator → Review analysis**, păstrează **All properties** și apasă **Analyse reviews**.
2. În **F4 · Review analysis**, apar analizele celor șase proprietăți. Cardurile
   prezintă recomandări structurate cu temă, prioritate **HIGH/MEDIUM/LOW**, acțiune și
   citate verificabile din recenzii. Sunt sortate după prioritate. Pentru **Old Town Studio**,
   arată **5 reviews**, media **3.8** și temele **Wi-Fi**, **Check-in**, **Noise**.
3. Prima analiză afișează **First analysis for this property.** În
   **Saved review snapshots (6)** se văd sursele, datele și numărul de recenzii.
   În **Notifications**, arată recomandările hostului; în **Agent timeline**, uneltele
   `get_rating_stats`, `get_reviews`, `add_recommendation` și `finish`.
   Nu se cere aprobare: sunt recomandări informative.
4. Apasă din nou **Analyse reviews** fără să schimbi recenziile. În **Notifications**
   apar mesaje **No new reviews for …**; se reutilizează analiza existentă, fără snapshot
   nou și fără altă rulare de agent. Lista snapshoturilor și numărul analizelor rămân
   neschimbate. Pe mock nu sunt necesare chei sau rețea.
5. Dacă sursa aduce recenzii schimbate, o analiză nouă compară temele cu analiza anterioară
   și arată **New**, **Still present**, **No longer mentioned**. Ultima categorie înseamnă
   că tema nu mai apare în noile recenzii, nu că aplicația a confirmat repararea problemei.
   Simulatorul nu are editor de recenzii; această variantă este acoperită de testele F4.

La eroare/timeout al sursei se folosește ultimul snapshot, cu marcajul
**based on the last saved snapshot** și notificare explicită; fără snapshot analiza
este sărită și hostul este avertizat. Fiecare proprietate este analizată independent,
deci una indisponibilă nu le oprește pe celelalte.

## 5. F5 — relistare pe nopți după anulare

1. În **World data → Calendar**, alege **Old Town Studio**. Pentru 3–5 iunie
   tarifele obișnuite sunt **70, 70, 80.50 EUR**; 5 iunie este vineri.
2. În **Simulator → Cancel booking**, alege **Emma Johnson**, apoi **Cancel this booking**.
   PMS-ul simulat anulează rezervarea, iar sincronizarea emite `BookingCancelled`.
3. În **Agent timeline**, selectează `f5_relisting`; urmărește `inspect_cancellation`,
   `search_comparables` și `prepare_relisting`. În **Notifications** apar progresul
   căutării și propunerea care așteaptă aprobarea hostului.
4. În pagina **F5**, propunerea este `awaiting_approval`. Sursa este fixture-ul
   independent Inside Airbnb din Atena. Pentru criteriile Old Town Studio, cel mai ieftin
   comparabil din eșantion este **Apartment with Leafy Backyard Steps from the Acropolis**:
   **130.50 EUR/noapte**, fără conversie din simbolul `$`. Celelalte rânduri nu trec toate filtrele.
   Deoarece comparabilul este mai scump decât tariful proprietății, plafonul F5 nu permite
   o creștere; propunerea păstrează tarifele obișnuite:

   | Noapte | Tarif original | Prag host | Tarif propus |
   |---|---:|---:|---:|
   | 2026-06-03 | 70.00 | 52.50 | 70.00 |
   | 2026-06-04 | 70.00 | 52.50 | 70.00 |
   | 2026-06-05 | 80.50 | 60.38 | 80.50 |

5. În **Approvals**, titlul include **70–80.5 EUR/night**; sumarul enumeră
   nopțile, limitele, sursa și expirarea la **2026-06-03 15:00 Europe/Bucharest**.
   Până la aprobare, calendarul rămâne la tarifele originale, cu nopțile eliberate.
6. Apasă **Relist at 70–80.5 EUR/night**. F5 recitește calendarul și aplică exact
   tarifele aprobate pe nopți. Propunerea devine `applied`, iar **Notifications**
   confirmă nopțile și prețurile. În **World data → Calendar**, verifică cele trei sume.
7. Repetarea aceleiași decizii nu produce alte scrieri. Pentru respingere, resetează
   și repetă până la pasul 4, apoi **Reject**: `rejected`, prețuri originale păstrate.

### Variante de protecție F5

- **Anulare îndepărtată:** resetează și anulează **Marie Dubois** (check-in peste
  20 de zile). Apare notificarea **not last-minute**, fără propunere sau aprobare F5.
- **Conflict înainte de aprobare:** testul
  `python manage.py test app/features/f5_relisting/tests/test_phase1.py -k calendar_conflicts`
  ocupă o noapte sau îi schimbă tariful înainte de decizie. Rezultatul este `conflict`,
  notificare host și zero apeluri de scriere din F5. Simulatorul comun nu are buton
  de creare rezervare; nu am introdus un endpoint de test care să modifice calendarul.
- **Expirare:** creează propunerea cu data fixă de mai sus; oprește API-ul și repornește-l
  pe aceeași bază cu `$env:FIXED_TODAY='2026-06-04'`. Reîncarcă F5: `expired` și notificare.
  Aprobarea veche poate apărea `approved` în inboxul comun, însă calendarul rămâne neschimbat.
  Testele verifică și limita exactă de la ora 15:00, plus expirarea fără o citire prealabilă.
- **Piață foarte ieftină/scumpă și eșecuri de adaptor:** testele locale verifică pragurile
  individuale, plafonul original, timeouturile, lipsa datelor și progresul parțial la scriere.
  Nu sunt necesare servicii externe sau modificări de seed pentru aceste scenarii.

## Sursa de piață F5: fixture-ul Atena și snapshotul complet

Demo-ul folosește opt rânduri reale și patru sintetice incluse în
`app/adapters/inside_airbnb/fixtures/athens_2026-09-22.sample.csv`. Prețurile Atena sunt
în EUR, chiar dacă exportul afișează `$`; o conversie cere monedă USD explicită.
Recomandarea pentru acest snapshot este `MARKET_USD_TO_CURRENCY_RATE=1`. Datele sunt din Atena,
nu din București, și nu trebuie tratate ca prețuri reprezentative pentru piața locală.
UI-ul și sumarul aprobării afișează **Inside Airbnb … CC BY 4.0**.

Pentru un eșantion de piață mai mare, configurarea și descărcarea arhivei complete sunt în
[README-ul adaptorului](../app/adapters/inside_airbnb/README.md). Înlocuiește calea CSV
cu `app/adapters/inside_airbnb/local_data/listings.csv.gz`, păstrează orașul Atena și
cursul explicit, apoi repornește API-ul. Schimbarea fișierului sau a cursului declanșează
reimportul automat al pieței.

Pentru rezerva fără fixture, setează `MARKET_PROVIDER=mock` și repornește API-ul.
După resetul lumii, anulează Emma și urmărește același flux F5: comparabilul devine
**Lipscani Cozy Studio**, **68 EUR/noapte**, iar propunerea este
**65.96 / 65.96 / 75.85 EUR**. Aprobarea aplică acele tarife pe fiecare noapte.

Dacă fixture-ul lipsește sau un serviciu real nu este disponibil, setează providerul
respectiv pe `mock`, repornește API-ul și resetează lumea de demo. Nu sunt necesare
internet, chei PMS, OpenRouter sau Apify. Nu comite `.env` ori baze de date locale.

## Verificare

`python manage.py test` rulează întreaga suită, inclusiv testele Streamlit.
Parcurgerea celor cinci fluxuri a fost verificată și cu `AppTest` prin HTTP real,
cu procesele pornite prin `python manage.py api` și `python manage.py ui`;
nu a fost verificat vizual într-un browser interactiv. Integrările PMS de producție
rămân de verificat cu credențiale și capabilități reale.
