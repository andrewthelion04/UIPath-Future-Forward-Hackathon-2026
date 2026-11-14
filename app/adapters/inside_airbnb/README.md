# Inside Airbnb — import local

Adaptorul citește **fișierul detaliat** `listings.csv.gz` sau varianta sa dezarhivată,
prin `INSIDE_AIRBNB_LISTINGS_CSV`. Nu caută automat un nume de fișier și nu descarcă
date la pornire. `listings.csv` sumar de pe site nu conține facilitățile și este respins.

## Fișierul real verificat

Am plasat snapshotul **Athens, 22 septembrie 2026**, în:

```text
app/adapters/inside_airbnb/local_data/listings.csv.gz
```

Folderul `local_data/` este ignorat de Git prin `.gitignore` local adaptorului.
Fișierul complet are 8.417.263 bytes și 15.134 rânduri; importul acceptă 14.214
anunțuri cu preț pozitiv și dimensiuni valide. Nu se comite baza de date sau arhiva mare.

Sursă: [Inside Airbnb — Get the Data](https://insideairbnb.com/get-the-data/).
[Arhiva exactă](https://data.insideairbnb.com/greece/attica/athens/2026-09-22/data/listings.csv.gz).
SHA-256 al arhivei: `192d5ffba10050bb01aa788332e86c2eb1dc25056514c2ecb114c49cfb7ccde1`.

Pentru reproducere din rădăcina proiectului, PowerShell:

```powershell
New-Item -ItemType Directory -Force app/adapters/inside_airbnb/local_data
Invoke-WebRequest -Uri 'https://data.insideairbnb.com/greece/attica/athens/2026-09-22/data/listings.csv.gz' -OutFile app/adapters/inside_airbnb/local_data/listings.csv.gz
```

Configurare în `.env` (sau variabile de mediu), fără modificări în nucleu:

```dotenv
PMS_PROVIDER=mock
LLM_PROVIDER=mock
REVIEWS_PROVIDER=mock
MARKET_PROVIDER=inside_airbnb
INSIDE_AIRBNB_LISTINGS_CSV=app/adapters/inside_airbnb/local_data/listings.csv.gz
MARKET_CITY=Athens
MARKET_CURRENCY=EUR
```

Calea relativă se rezolvă din directorul de lucru; `manage.py` pornește procesele din
rădăcina proiectului. Din alt director folosește o cale absolută. `MARKET_CURRENCY`
declară moneda snapshotului, nu convertește sume; simbolul `$` este formatul exportului.
Fișierul este importat când `market_listings` este gol, inclusiv după resetul lumii.
Schimbarea fișierului sau a orașului într-o bază deja populată cere reimport explicit
prin `import_listings()` sau o bază nouă (`DATABASE_PATH`); simplul restart păstrează cache-ul.
Pentru rezerva fără rețea sau arhivă setează `MARKET_PROVIDER=mock`.

## Maparea facilităților

| Vocabular intern | Variante observate și acceptate |
|---|---|
| `wifi` | Wifi, Fast wifi – … Mbps, Pocket wifi |
| `kitchen` | Kitchen |
| `air_conditioning` | Air conditioning, Central/Portable air conditioning |
| `washer` | Washer, Free/Paid washer – In unit / In building |
| `self_check_in` | Self check-in, Lockbox, Keypad, Smart lock |
| `balcony` | Patio or balcony, Private/Shared patio or balcony |
| `parking` | Free/Paid parking, street/driveway/valet/garage/lot |
| `heating` | Heating, Central/Radiant heating |
| `fireplace` | Indoor/Outdoor fireplace și variante cu combustibil |

Nu inferăm mașină de spălat din `Dishwasher`, șemineu din `Fireplace guards` sau
self check-in din `Lock on bedroom door`. Denumirile necunoscute rămân tokenuri
normalizate, fără a declara facilități suplimentare. Listele JSON invalide ca tip
nu produc facilități; formatul vechi `{...}` este acceptat.

CSV-ul este citit în flux; rândurile cu preț absent/nul/negativ, capacitate invalidă
sau dimensiuni nefinite sunt ignorate. Un import invalid păstrează snapshotul anterior
și ridică `MarketDataError`. Comparabilele nu sunt ascunse de primele 300 de anunțuri
ieftine fără facilitățile cerute. Prețurile sunt referințe istorice, fără garanții de
disponibilitate live pentru intervalul cerut.

## Atribuire și teste

Datele provin de la **Inside Airbnb**, sub licența
[Creative Commons Attribution 4.0 International](https://creativecommons.org/licenses/by/4.0/).
Pagina F5 și sumarul aprobării afișează creditul; fiecare propunere păstrează sursa
și atribuirea chiar dacă se schimbă providerul ulterior.

`fixtures/athens_2026-09-22.sample.csv` păstrează opt rânduri reale cu doar cele șapte
coloane necesare adaptorului. Modificări față de sursă: selecție de rânduri/coloane,
serializare CSV UTF-8; valorile câmpurilor sunt păstrate. Nu sunt date sintetice.
SHA-256 al extrasului: `c945af760f6cb076a73910fb8066df08c04837a55a18ebd3bc8ebf3b23d5e4cd`.
Testele comprimă extrasul în directoare temporare și verifică și varianta gzip;
nu folosesc rețea și nu necesită arhiva completă.

```powershell
python manage.py test app/features/f5_relisting/tests/test_inside_airbnb.py
```
