# Demo StayPilot AI — cele cinci fluxuri

## Pregătire reproductibilă

Din rădăcina proiectului, instalează dependențele cu `python manage.py setup` dacă
mediul lipsește. Pe Windows folosește un Python nativ; în mediul verificat am creat
`.venv` cu `py -3.11 -m venv .venv`, apoi am instalat `requirements-dev.txt`.

În terminalul API, PowerShell:

```powershell
$env:PMS_PROVIDER='mock'
$env:LLM_PROVIDER='mock'
$env:REVIEWS_PROVIDER='mock'
$env:MARKET_PROVIDER='mock'
$env:FIXED_TODAY='2026-06-01'
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

În **Simulator → Reset world → Reset world**, resetează exclusiv lumea de demo.
În **World data**, verifică patru proprietăți, șapte rezervări și data simulată
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
   Se trimite întâi oferta, apoi se așteaptă răspunsul oaspetelui (`awaiting_guest`).
   Cu setările de mai sus răspunsul este automat `accept`; nu răspunde în **Guest chat**,
   deoarece acel chat este procesat de F3. În conversația lui Lukas apar oferta și
   confirmarea exactă din aprobare.
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
2. În **F4 · Review analysis**, apar analizele celor patru proprietăți. Cardurile
   prezintă recomandări structurate cu temă, prioritate **HIGH/MEDIUM/LOW**, acțiune și
   citate verificabile din recenzii. Sunt sortate după prioritate. Pentru **Old Town Studio**,
   arată **5 reviews**, media **3.8** și temele **Wi-Fi**, **Check-in**, **Noise**.
3. Prima analiză afișează **First analysis for this property.** În
   **Saved review snapshots (4)** se văd sursele, datele și numărul de recenzii.
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
3. În pagina **F5**, propunerea este `awaiting_approval`. Cel mai ieftin comparabil
   este **Lipscani Cozy Studio**, **68 EUR**. Anunțul de 45 EUR nu are facilitățile cerute.
   Tabelul pe nopți arată:

   | Noapte | Tarif original | Prag host | Tarif propus |
   |---|---:|---:|---:|
   | 2026-06-03 | 70.00 | 52.50 | 65.96 |
   | 2026-06-04 | 70.00 | 52.50 | 65.96 |
   | 2026-06-05 | 80.50 | 60.38 | 75.85 |

4. În **Approvals**, titlul include **65.96–75.85 EUR/night**; sumarul enumeră
   nopțile, limitele, sursa și expirarea la **2026-06-03 15:00 Europe/Bucharest**.
   Până la aprobare, calendarul rămâne la tarifele originale, cu nopțile eliberate.
5. Apasă **Relist at 65.96–75.85 EUR/night**. F5 recitește calendarul și aplică exact
   tarifele aprobate pe nopți. Propunerea devine `applied`, iar **Notifications**
   confirmă nopțile și prețurile. În **World data → Calendar**, verifică cele trei sume.
6. Repetarea aceleiași decizii nu produce alte scrieri. Pentru respingere, resetează
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

## Varianta cu Inside Airbnb și rezerva mock

Configurarea și descărcarea exactă sunt în
[README-ul adaptorului](../app/adapters/inside_airbnb/README.md).
Arhiva verificată este în `app/adapters/inside_airbnb/local_data/listings.csv.gz`.
Folosește `MARKET_PROVIDER=inside_airbnb`, calea respectivă,
`MARKET_CITY=Athens`, `MARKET_CURRENCY=EUR` și o bază nouă sau un reimport.
Restul porturilor pot rămâne mock.

Repetă F5: comparabilul și prețurile diferă de scenariul mock, dar fiecare noapte
rămâne între prag și plafon. UI-ul și sumarul aprobării afișează explicit
**Inside Airbnb … CC BY 4.0**. Sunt snapshoturi istorice pentru orașul configurat,
nu prețuri live din București.

Dacă arhiva lipsește sau un serviciu real nu este disponibil, setează toate cele patru
providere pe `mock`, repornește API-ul și resetează lumea de demo. Nu sunt necesare
internet, chei PMS, OpenRouter sau Apify. Nu comite `.env` ori baze de date locale.

## Verificare

`python manage.py test` rulează întreaga suită, inclusiv testele Streamlit.
Parcurgerea celor cinci fluxuri a fost verificată și cu `AppTest` prin HTTP real,
cu procesele pornite prin `python manage.py api` și `python manage.py ui`;
nu a fost verificat vizual într-un browser interactiv. Integrările PMS de producție
rămân de verificat cu credențiale și capabilități reale.
