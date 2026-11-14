# F5 — Reluarea și verificarea fazei 1

Data: 14 noiembrie 2026. Această reluare păstrează modificările locale deja existente,
inclusiv agentul F5 și fișierele comune modificate anterior. Nu a fost schimbat nucleul,
containerul, porturile, alte feature-uri sau simulatorul comun.

## Rezolvarea cerințelor

1. **Tarife proporționale individuale.** `pricing.py` păstrează factorul comun aplicat
   tarifelor originale, pragul fiecărei nopți și plafonul original. Rotunjirea folosește
   Decimal. A fost corectat cazul limită `base_price=70`, noapte de `0.01`, reducere 100%
   și undercut 99.9%: tariful rămâne cel puțin un cent. Rotunjirea monetară limitează
   precizia raporturilor dintre nopți, fără încălcarea pragului sau plafonului.
2. **Verificare la aprobare.** Calendarul complet este recitit înainte de scrieri;
   nopțile ocupate sau prețurile schimbate produc `conflict`, fără scrieri. În plus,
   fiecare noapte este recitită imediat înainte de actualizare. Dacă schimbarea apare
   după aplicarea unor nopți, se raportează `failed` și progresul confirmat.
3. **Expirare.** Se verifică termenul inițial și ora curentă de check-in a hostului.
   Un termen mutat mai devreme scurtează valabilitatea și este afișat corect; mutarea
   ulterioară nu prelungește acordul inițial. Aprobarea târzie nu modifică tarifele.
4. **Plafon.** Fiecare sumă rămâne sub tariful original al nopții, inclusiv când
   piața este mult mai scumpă. Înainte de scrieri se verifică și limita curentă de
   reducere a hostului. O politică nouă mai strictă invalidează propunerea, fără a
   modifica automat sumele pe care le-a aprobat hostul.
5. **Inside Airbnb.** Au fost completate variantele reale `AC - split type ductless
   system` și `Window AC unit`. Snapshotul complet a fost importat și verificat:
   14.214 anunțuri valide. Creditul Inside Airbnb / CC BY 4.0 rămâne vizibil în pagina
   F5 și în sumarul aprobării. Proveniența fixture-ului este descrisă exact: opt rânduri
   reale și patru rânduri sintetice adăugate anterior pentru demo; conținutul nu a fost schimbat.
6. **Demo.** `docs/DEMO.md` conține configurare reproductibilă fără chei, cei cinci
   pași de produs, opțiunile de protecție F5 și rezerva `MARKET_PROVIDER=mock`.
   Valorile și etichetele UI sunt corectate la moneda locală și formatul afișat.

## Arhiva și moneda sursei

Arhiva completă este plasată în:

```text
app/adapters/inside_airbnb/local_data/listings.csv.gz
```

`local_data/` este ignorat de Git. Adaptorul citește calea configurată, nu un folder
implicit obligatoriu. Configurația explicită pentru arhiva Atena este:

```dotenv
MARKET_PROVIDER=inside_airbnb
INSIDE_AIRBNB_LISTINGS_CSV=app/adapters/inside_airbnb/local_data/listings.csv.gz
MARKET_CITY=Athens
MARKET_CURRENCY=EUR
MARKET_USD_TO_CURRENCY_RATE=1
```

Pentru demo fără arhiva mare, calea este
`app/adapters/inside_airbnb/fixtures/athens_2026-09-22.sample.csv`.
Fișierul complet și fixture-ul rămân neschimbate în această reluare. Configurarea,
checksumurile și licența sunt în [README-ul adaptorului](../../adapters/inside_airbnb/README.md).

Conform dicționarului oficial al sursei, `price` este în **moneda locală**, iar `$`
este un artefact de export. Atena și Viena folosesc EUR; `$130.50` nu trebuie redus
artificial la 120.06 EUR. Parametrul de conversie existent este păstrat pentru
compatibilitate, dar se aplică doar unui rând care declară explicit USD prin prefix
ISO sau coloană `currency`. Monedele incompatibile sau contradictorii sunt respinse.
Importul este revalidat la schimbarea fișierului, setărilor sau versiunii mapării.

## Robustețe suplimentară

- Evenimentul de decizie este verificat față de **aprobarea persistentă**: status,
  feature, tip, opțiune, propunere, rezervare și proprietate. Un eveniment intern
  construit manual cu o aprobare încă pending nu poate scrie în calendar.
- Timeoutul total al handlerului și `CancelledError` persistă `failed` și nopțile
  confirmate înainte de propagare. Rezultatul apelului întrerupt poate fi incert în
  PMS; hostul primește instrucțiuni explicite de verificare. Nu există retry/rollback automat.
- Agentul primește o listă compactă cu minimul validat, iar lista completă rămâne în
  starea rulării. Au fost verificate 100 de comparabile fără trunchierea JSON-ului.
- Un agent care încheie înainte de inspectare nu pierde anularea în tăcere: hostul
  primește o notificare de fallback. Regulile de preț rămân în cod, nu în prompt.
- UI-ul afișează explicit eșecul, păstrează atribuirea și include un nume accesibil
  pentru tabelul tarifelor pe nopți.
- Conversiile cu overflow și rândurile cu monedă invalidă sunt respinse local;
  reimportul invalid nu distruge snapshotul anterior.

## Verificare și limita de scop

- **117 teste F5 trecute**, fără rețea, incluzând cele 14 regresii noi pentru aprobări,
  reguli curente, expirare, timeout total, prețuri foarte mici și rezultate ale agentului.
- Import complet real: 14.214 rânduri; cel mai ieftin comparabil verificat este
  `1690057213123330001`, la **27.50 EUR**, inclusiv cu vechea rată `.92` configurată.
- API-ul și Streamlit au fost pornite prin `python manage.py api` și `python manage.py ui`,
  separat pe porturile 8017/8765, pe o bază izolată. API-ul agent Streamlit a parcurs
  F1 transport, F3 early check-in aprobat, F2 mentenanță/relocare/acceptare/închidere,
  F4 recomandări și F5 propunere/aprobare/calendar, plus paginile de audit.
- Fixture-ul Atena a produs comparabilul **Apartment with Leafy Backyard Steps from
  the Acropolis**, **130.50 EUR**; plafonul a păstrat **70 / 70 / 80.50 EUR**. Agentul
  F5 a terminat, atribuirea CC BY 4.0 a fost vizibilă și propunerea a devenit `applied`.
- Rezerva mock a fost verificată și în simulatorul real după restart pe o bază separată:
  propunere, aprobare și calendar la **65.96 / 65.96 / 75.85 EUR**.
- Rularea finală `python manage.py test`: **555 trecute, 2 eșuate** (130,87 secunde).
  Cele două variante
  CSV/gzip ale testului comun `tests/test_real_adapters.py` așteaptă încă greșit
  `41.40/62.56` în loc de `45/68` EUR pentru exportul Viena. Utilizatorul a cerut explicit
  păstrarea zonei stricte, deci aceste așteptări comune nu au fost modificate sau mascate.

Nu s-au verificat credențiale sau scrieri în PMS de producție și nici aspectul vizual
într-un browser interactiv. Verificarea înainte de scriere micșorează fereastra de
concurență, dar portul comun nu oferă CAS/tranzacții între citire și scriere; suportul
atomic rămâne un PR separat. Expirarea este evaluată la acces/decizie, fără worker.

Nu s-au creat commituri, nu s-a făcut push și nu s-a rescris istoricul în această
reluare. Modificările se integrează normal; **nu este necesar `git push --force`**.
