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

## Demo fără rețea cu fixture-ul inclus

Pentru rularea F5 reproducibilă, folosește extrasul inclus:

```dotenv
MARKET_PROVIDER=inside_airbnb
INSIDE_AIRBNB_LISTINGS_CSV=app/adapters/inside_airbnb/fixtures/athens_2026-09-22.sample.csv
MARKET_CITY=Athens
MARKET_CURRENCY=EUR
MARKET_USD_TO_CURRENCY_RATE=1
```

Fixture-ul păstrează opt anunțuri reale din Atena și patru rânduri suplimentare de
demo, documentate separat mai jos. Prețurile snapshotului Atena sunt în EUR.
Conform [dicționarului oficial Inside Airbnb](https://docs.google.com/spreadsheets/d/1iWCNJcSutYqpULSQHlNyGInUvHg2BoUGoNRIGa6Szc4/edit),
coloana `price` folosește moneda locală, iar simbolul `$` este un artefact al exportului.
Nu deducem moneda USD din acel simbol și nu reducem artificial prețurile EUR.
După schimbarea fișierului sau setărilor, repornește API-ul pentru reimportul automat.

## Snapshot complet (opțional)

Pentru datasetul complet, după descărcarea arhivei configurarea este:

```dotenv
PMS_PROVIDER=mock
LLM_PROVIDER=mock
REVIEWS_PROVIDER=mock
MARKET_PROVIDER=inside_airbnb
INSIDE_AIRBNB_LISTINGS_CSV=app/adapters/inside_airbnb/local_data/listings.csv.gz
MARKET_CITY=Athens
MARKET_CURRENCY=EUR
MARKET_USD_TO_CURRENCY_RATE=1
```

Calea relativă se rezolvă din directorul de lucru; `manage.py` pornește procesele din
rădăcina proiectului. Din alt director folosește o cale absolută. `MARKET_CURRENCY`
declară moneda locală a snapshotului (EUR pentru Atena). Parametrul existent
`MARKET_USD_TO_CURRENCY_RATE` este păstrat pentru compatibilitate, dar se aplică
numai rândurilor care declară explicit USD prin prefixul ISO `USD` sau o coloană
`currency=USD`. De exemplu `USD 50.00`, cu rata `0.92` și moneda țintă EUR, devine
46 EUR; `$50.00` din snapshotul Atena rămâne 50 EUR chiar dacă rata este configurată.
Nu se consultă un serviciu valutar. Monedele explicite incompatibile ori contradictorii
sunt ignorate, fără a fi etichetate drept moneda pieței.
La pornirea API-ului, adaptorul compară amprenta fișierului, setările (oraș, monedă,
curs) și versiunea mapării cu ultima importare. Dacă s-au schimbat, reimportă tranzacțional;
o modificare invalidă păstrează snapshotul anterior și raportează eroarea. Repornește
API-ul după ce salvezi CSV-ul.
Pentru rezerva fără rețea sau arhivă setează `MARKET_PROVIDER=mock`.

## Maparea facilităților

| Vocabular intern | Variante observate și acceptate |
|---|---|
| `wifi` | Wifi, Fast wifi – … Mbps, Pocket wifi |
| `kitchen` | Kitchen |
| `air_conditioning` | Air conditioning, Central/Portable air conditioning, AC - split type ductless system, Window AC unit |
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
sau dimensiuni nefinite sunt ignorate. O sumă imposibil de reprezentat monetar după
conversie este ignorată fără a pierde celelalte anunțuri valide. Un import invalid păstrează snapshotul anterior
și ridică `MarketDataError`. Comparabilele nu sunt ascunse de primele 300 de anunțuri
ieftine fără facilitățile cerute. Prețurile sunt referințe istorice, fără garanții de
disponibilitate live pentru intervalul cerut.

## Atribuire și teste

Datele provin de la **Inside Airbnb**, sub licența
[Creative Commons Attribution 4.0 International](https://creativecommons.org/licenses/by/4.0/).
Pagina F5 și sumarul aprobării afișează creditul; fiecare propunere păstrează sursa
și atribuirea chiar dacă se schimbă providerul ulterior.

`fixtures/athens_2026-09-22.sample.csv` conține 12 rânduri cu cele șapte coloane
necesare adaptorului. Opt rânduri au fost comparate câmp cu câmp cu arhiva reală:
`361393`, `401423`, `484925`, `469140`, `60394`, `536197`, `33945`, `691025`.
Pentru acestea, transformările sunt selecția de rânduri/coloane și serializarea CSV
UTF-8; valorile câmpurilor sunt păstrate, iar atribuirea CC BY 4.0 se aplică sursei.

Celelalte patru rânduri (`712390`, `824102`, `953140`, `1045211`) sunt cazuri sintetice
de demo, absente din snapshotul verificat. Sunt păstrate pentru compatibilitatea
scenariilor existente, dar nu reprezintă dovezi despre anunțuri sau prețuri reale.
Testele disting explicit cele două grupuri; maparea facilităților este verificată pe
cele opt rânduri reale, inclusiv variantele AC și Window AC.
SHA-256 al extrasului: `cb8e5bb1bca874100beb23471d3850cbda23281d86ea64355e97a0e7dc2bc117`.
Testele comprimă extrasul în directoare temporare și verifică și varianta gzip;
nu folosesc rețea și nu necesită arhiva completă.

```powershell
python manage.py test app/features/f5_relisting/tests/test_inside_airbnb.py
```
