# F5 — Rezumatul implementării fazei 1

Verificat la 14 noiembrie 2026. Modificările sunt limitate la F5, adaptorul Inside
Airbnb, `docs/DEMO.md` și adăugări în `.env.example` / `requirements.txt`.
Nucleul, porturile, routerele comune și celelalte feature-uri nu au fost modificate.

## Cerințe realizate

1. **Preț proporțional pe fiecare noapte.** `pricing.py` conține funcții pure pentru
   comparabilitate, alegerea celui mai ieftin anunț, prag, preț, plafon, distribuție
   și validarea calendarului. Un factor comun se aplică tarifelor originale; fiecare
   noapte are pragul propriu. Snapshotul `nightly_prices` păstrează data, tariful
   original, pragul și suma aprobată. Referința scalară din API este păstrată pentru
   compatibilitate. Pentru seed: `70/70/80.50` → `65.96/65.96/75.85 EUR`.
2. **Verificare înainte de aplicare.** La aprobare se recitește întregul interval.
   Orice noapte rezervată/blocată produce `conflict`, notificare host și zero scrieri.
   Un snapshot incomplet, duplicat, cu altă proprietate/monedă sau tarife invalide
   produce `failed`. Sunt detectate și tarifele schimbate de host între timp. Nu se
   redeschide disponibilitatea: calendarul trebuie deja să confirme nopțile libere.
3. **Expirare la check-in.** `expires_at` folosește data rezervării, `check_in_time`
   al hostului și `F5_TIMEZONE`, implicit Europe/Bucharest. La limita exactă,
   propunerea devine `expired` și nu mai poate scrie. Expirarea este verificată la
   citire/decizie, după citiri lente și înainte de fiecare scriere. Persistarea și
   notificarea tranziției sunt idempotente. `FIXED_TODAY` păstrează data de demo.
4. **Plafon individual.** Nicio sumă aplicată nu depășește tariful original al nopții,
   chiar dacă piața este foarte scumpă. Pragurile sunt rotunjite în sus la cent, iar
   tarifele folosesc Decimal / ROUND_HALF_UP. Rotunjirea nu produce nopți gratuite.
5. **Inside Airbnb real.** Maparea este verificată pe snapshotul Atena din 22 septembrie
   2026, inclusiv Wifi/Fast wifi, variante de climatizare, Free/Paid washer, Lockbox,
   Keypad, Smart lock, balcon, parcare, încălzire și șemineu. Au fost eliminate
   potrivirile false Dishwasher→washer și Fireplace guards→fireplace. Creditul
   Inside Airbnb / CC BY 4.0 este vizibil în pagina F5 și în sumarul aprobării,
   plus păstrat în propunere pentru afișarea istoricului după schimbarea providerului.
6. **Demo complet.** `docs/DEMO.md` descrie pornirea, cele cinci fluxuri, acțiunile
   exacte și rezultatele, scenariile F5 de respingere/conflict/expirare și rezerva mock.
   Specificația `CLAUDE.md` și README-ul F5 sunt actualizate; planul este bifat.

## Poziția și configurarea arhivei reale

Arhiva completă a fost descărcată în:

```text
app/adapters/inside_airbnb/local_data/listings.csv.gz
```

Config pentru rularea reală de piață:

```dotenv
MARKET_PROVIDER=inside_airbnb
INSIDE_AIRBNB_LISTINGS_CSV=app/adapters/inside_airbnb/local_data/listings.csv.gz
MARKET_CITY=Athens
MARKET_CURRENCY=EUR
```

Adaptorul citește calea configurată; nu există un folder implicit obligatoriu.
`manage.py` folosește rădăcina proiectului ca director de lucru. Folderul local este
ignorat de Git. La integrare se descarcă arhiva folosind instrucțiunile din
[README-ul adaptorului](../../adapters/inside_airbnb/README.md), care păstrează URL-ul
exact, checksumul, licența și regulile de reimport. `.env` nu a fost creat sau modificat.

Arhiva are 8.417.263 bytes / 15.134 rânduri; au fost importate 14.214 anunțuri valide.
Un extras real de opt rânduri și șapte coloane este versionat în `fixtures/`, cu
proveniență și transformări documentate. Testele îl folosesc fără rețea, atât CSV,
cât și gzip. Snapshotul complet nu este necesar testelor.

## Îmbunătățiri suplimentare

- Valori finite/pozitive pentru prețuri, procente validate și timeouturi strict pozitive.
- Verificare completă a nopților și monedei, fără a inventa tarife când citirea PMS
  nu este disponibilă. Datele de piață invalide nu pot deveni cel mai ieftin comparabil.
- Blocare locală pe proprietate, deduplicare reală a propunerii și protecție împotriva
  reluării unei decizii finalizate, inclusiv evenimente repetate.
- Migrare aditivă a celor două coloane F5 pe baze existente. Datele celorlalte
  feature-uri sunt păstrate; o propunere veche fără snapshot poate fi doar tratată manual.
- Detectarea prețurilor calendarului modificate între propunere și decizie, fără scrieri.
- Timeout explicit pentru citirile și scrierile calendarului; fallback local și notificare.
  La eșec în timpul scrierii, sunt enumerate nopțile confirmate, iar hostul este informat
  că apelul eșuat poate avea rezultat incert în PMS.
- Import CSV în flux, validare de antet și mesaj clar pentru CSV sumar, rânduri invalide
  ignorate, import atomic care păstrează snapshotul anterior la eșec.
- Comparabilele cu facilitățile cerute sunt găsite chiar dacă peste 300 de anunțuri
  mai ieftine nu corespund. Ordonarea folosește și referința pentru egalități stabile.
- UI cu tabel pe nopți, limite, expirare și mesaje explicite pentru conflict/expirare.
  Endpointul validează `limit` între 1 și 200.
- `tzdata` adăugat explicit pentru fusuri IANA pe Windows; setările F5 sunt documentate
  prin adăugări minimale în `.env.example`.

## Verificări efectuate

- **`python manage.py test`: 232 teste trecute**, inclusiv testele tuturor feature-urilor,
  arhitectura, aprobările, adaptoarele și paginile Streamlit. Testele vechi pentru tarif
  uniform au fost actualizate la sumele exacte pe nopți; limita hostului nu a fost slăbită.
  Testul PMS fără citire verifică acum fallbackul mai devreme, înainte de aprobarea
  unui preț fără snapshot. Au fost adăugate teste pentru scrieri eșuate și timeouturi reale.
- **API și UI pornite efectiv** cu `python manage.py api --port 8015 --no-reload` și
  `python manage.py ui --server.port 8515`, pe o bază de demo separată și toate porturile mock.
  Streamlit a răspuns `ok` la health check. AppTest a parcurs paginile prin HTTP real:
  F1 transport și răspuns, F3 early check-in cu aprobare, F2 sesizare și relocare,
  F4 analiză, F5 anulare și aplicarea tarifelor pe nopți. Au fost verificate și paginile
  World data, Notifications, Agent timeline și Events.
- **Arhivă reală și F5 cap-coadă**, prin API separat pe portul 8016: cel mai ieftin
  comparabil ales are referința `1690057213123330001`, tarif 27.50 în moneda configurată.
  Host floor a limitat programul la **52.50 / 52.50 / 60.38 EUR**. Aprobarea a produs
  `applied`, calendarul a fost verificat, iar pagina Streamlit a afișat creditul CC BY 4.0.
- `git diff --check` și verificarea zonei modificate; niciun fișier de implementare
  comun sau al altui feature nu este atins.

## Limite și decizii față de planul inițial

- Verificarea înaintea scrierii respectă portul disponibil, dar **nu poate fi atomică
  cu rezervările dintr-un PMS extern**. Portul nu are CAS sau operație tranzacțională
  pe întregul interval. Pentru un eșec după prima noapte, rezultatul este `failed`
  cu progres parțial, fără rollback/retry care ar putea suprascrie schimbări noi.
- Expirarea nu are worker de fundal; este materializată la acces sau decizie. Inboxul
  comun are doar pending/approved/rejected: o decizie târzie poate fi `approved` acolo,
  dar propunerea F5 rămâne `expired`, cu notificare și zero scrieri de la acea decizie.
- PMS-urile fără citirea calendarului ajung acum direct la fallback manual, înainte de
  aprobare. Aceasta este necesară pentru tarife originale reale și verificarea disponibilității.
- Scrierea disponibilității a fost eliminată deoarece nopțile trebuie să fie deja libere.
- Verificarea UI a fost automatizată cu AppTest; nu am inspectat vizual un browser
  interactiv. Nu au fost testate autentificări sau scrieri în PMS de producție, conexiuni
  OpenRouter/Apify reale ori capabilități indisponibile în adaptoarele actuale.
- Deciziile sunt documentate local F5 deoarece cerința interzice modificarea
  `docs/ARCHITECTURE.md` și a nucleului în această sarcină.

La livrarea inițială a fazei 1 nu s-au creat commituri și nu s-a făcut push.
Integrarea ulterioară, cerută explicit, este descrisă mai jos; istoricul nu a fost rescris.

## Integrare cu implementările F1–F4

La 14 noiembrie 2026, modificările F5 au fost salvate în commitul `60ec270`, apoi
`git pull --no-rebase origin main` a adus implementările colegilor până la `e4cf5fe`.
Merge-ul `1c3f9a6` păstrează istoricul ambelor ramuri. Singurul conflict a fost în
`.env.example`; au fost păstrate atât setările F1/F2, cât și setările F5. Fișierul
local `.env`, secretele, bazele demo și arhiva completă Inside Airbnb rămân ignorate.

Revizia înainte de publicare a identificat și corectat un caz de expirare lângă
miezul nopții: fără `FIXED_TODAY`, data trebuie să provină din `F5_TIMEZONE`, nu din
fusul orar al serverului. Două regresii acoperă București și Los Angeles, astfel
încât diferența de zi față de UTC să nu întârzie sau să grăbească expirarea.

Verificări pe codul integrat:

- Suita completă inițială după merge: **511 teste trecute**; cele două regresii noi
  de fus orar au trecut și separat. Rularea completă finală, incluzând corecția:
  **513 teste trecute**, în 105,36 secunde.
- Evaluatorul mock F3: **8/8 scenarii reușite**.
- Extensia: `npm ci`, `npm run compile` și `npm run build` au trecut, fără modificarea
  lockfile-ului; artefactul Chrome MV3 este în `extension/.output/chrome-mv3`.
- API-ul și Streamlit au fost pornite separat pe porturile 8015/8515, cu bază izolată
  și toate porturile pe mock. AppTest a parcurs prin HTTP real toate cele cinci fluxuri,
  inclusiv cererea F3 repetată, mentenanța/oferta/acceptarea/închiderea separată din F2,
  recomandările structurate și reutilizarea snapshotului F4, aprobarea și tarifele F5.
  Paginile de audit s-au afișat fără erori și handlerii verificați au avut status `ok`.
- `docs/DEMO.md` a fost adaptat fluxurilor F1–F4 actuale, păstrând pașii F5.

Push-ul HTTPS local a fost respins cu 403 pentru contul `mihaelatudor2211`.
Conexiunea GitHub din aplicație, autentificată ca `PopanicaTudor`, are drept de scriere;
publicarea folosește această conexiune și același arbore Git verificat. Commiturile
locale de integrare sunt păstrate în ramura `integration/f5-local-20261114`, iar
commitul publicat are ca părinte vârful curent al colegilor. Actualizarea lui `main`
verifică SHA-ul așteptat și folosește `force=false`; **nu se folosește force push**.
