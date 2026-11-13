# CLAUDE.md — StayPilot AI

Proză în română, cod și identificatori în engleză.

Acest fișier descrie **ce** construim, **constrângerile** și **ce am verificat deja**. Deciziile de software engineering (structura exactă a folderelor, numele modulelor, pattern-urile concrete, schema bazei de date) sunt ale tale. Când iei o decizie arhitecturală importantă, documenteaz-o scurt în `docs/ARCHITECTURE.md`.

### Libertate și judecată

Fișierul e un ghid, nu o cușcă. Echipa poate să fi ratat lucruri.

- **Dacă vezi o variantă mai bună, mai simplă sau mai robustă, folosește-o.** Asta include completarea a ce lipsește, corectarea a ce e greșit sau neclar și ignorarea detaliilor care nu au sens în practică.
- **Dacă te abați de la o constrângere din §3 sau de la o recomandare din §5:**
  - notează în `docs/ARCHITECTURE.md` ce ai schimbat și de ce;
  - menționează schimbarea în rezumatul de la finalul sarcinii.
- **Semnalează proactiv** riscurile, informațiile lipsă și lucrurile care ar trebui verificate (ex. un API care se comportă altfel decât e descris în §4).
- **Ce rămâne neschimbat**, pentru că ține de scopul proiectului, nu de implementare:
  - aplicația rulează complet pe mock, fără chei;
  - cei 5 pot lucra în paralel fără să-și strice codul unul altuia;
  - un feature care cade nu le afectează pe celelalte;
  - acțiunile importante trec prin aprobarea hostului;
  - comutarea mock ↔ real se face din configurație.

  Dacă ai un motiv serios să schimbi chiar și unul dintre acestea, întreabă întâi.

---

## 1. Produsul

**StayPilot AI** este un copilot pentru hosturile care gestionează rezervări printr-un PMS. Ține hostul la curent tot timpul și îi cere aprobarea pentru acțiunile importante (human-in-the-loop).

| # | Feature | Ce face |
|---|---|---|
| F1 | **Integrare PMS** | Mesagerie bidirecțională (webhook de intrare + trimitere de mesaje) prin adaptorul configurat: OwnerRez, Hospitable sau mock. **Doar transport**, fără logică de business. |
| F2 | **Incidente** | Primește sesizări (ex. de la doamna de curățenie, prin aplicație) și propune fie **remedierea la timp**, fie **relocarea** oaspetelui într-o proprietate liberă compatibilă. |
| F3 | **Copilot oaspeți** | Răspunde la cererile oaspeților consultând datele noastre: rezervarea, proprietatea, regulile hostului, calendarul. Exemplu: early check-in. |
| F4 | **Analiza recenziilor** | Preia recenziile (mock sau Apify), le analizează și trimite hostului recomandări de îmbunătățire. |
| F5 | **Relistare după anulare** | La o anulare de ultim moment, caută cel mai ieftin anunț comparabil (aceleași facilități) și propune relistarea la un preț puțin mai mic. |

**Nu modelăm tarife pentru personal sau pentru reparații.** Nu sunt relevante.

## 2. Etapizare

1. **Scheletul.** Aplicația rulează local, complet, pe un mediu simulat (mock), fără niciun API extern și fără nicio cheie. Fiecare feature are o implementare minimă care funcționează cap-coadă. ← **Prima sarcină.**
2. **Lucru în paralel.** Fiecare dintre cei 5 dezvoltatori își extinde feature-ul independent.
3. **Integrări reale.** Legarea la OwnerRez (inițial în Stage) sau Hospitable, Apify și date de piață, **doar prin schimbarea configurației**, fără modificări în logica feature-urilor.

## 3. Constrângeri de arhitectură (obligatorii)

1. **Hexagonal (Ports & Adapters) + Dependency Injection.**
   - Orice dependență externă stă în spatele unei interfețe și are cel puțin un adaptor mock și unul real. Dependențele externe sunt: canalul de mesaje, proprietățile și rezervările din PMS, recenziile, datele de piață, calendarul/prețurile și LLM-ul.
   - Comutarea mock ↔ real se face dintr-un **singur fișier de config**.
   - Logica feature-urilor nu cunoaște adaptoarele concrete.
2. **Event-driven.**
   - Intrările (webhook, simulator, UI) doar normalizează datele într-un eveniment intern și îl publică.
   - Feature-urile se abonează la evenimente.
   - **Un feature nu importă alt feature.** Comunicarea dintre feature-uri se face prin evenimente. Exemplu: oaspetele semnalează o defecțiune, F3 emite un eveniment, F2 îl preia.
   - Un mesaj venit din simulator și unul primit printr-un PMS produc **același** eveniment.
3. **Separation of Concerns / SRP.**
   - Fiecare feature e izolat în propriul modul.
   - Contractele dintre componente sunt modele de date explicite (Pydantic).
4. **KISS.**
   - Python, FastAPI, Pydantic, SQLite local sau JSON pentru lumea simulată.
   - **Fără framework-uri de agenți** (LangChain, LangGraph etc.) și fără infrastructură grea (cozi externe, ORM-uri complexe), dacă nu e strict necesar.
   - Bucla agentului se scrie de mână (vezi §5).
5. **Fail-fast și graceful degradation.**
   - **La pornire:** config invalid sau secret lipsă pentru un adaptor real → oprire cu un mesaj clar.
   - **La rulare:** dacă F4 sau F5 (sau orice adaptor extern) dă eroare sau timeout, F1, F2 și F3 rămân complet neafectate. Fiecare handler e izolat. Fallback-urile sunt explicite, iar hostul e notificat.
6. **5 dezvoltatori în paralel, fără merge conflicts.**
   - Adăugarea sau extinderea unui feature nu trebuie să ceară editarea unui fișier central (folosește auto-discovery sau un mecanism echivalent).
   - Fiecare feature are codul, tabelele, prompturile, testele și pagina de UI în zona lui.
   - Contractele comune (evenimente, modele de domeniu) sunt puține, stabile și se modifică doar prin adăugare.
7. **Human-in-the-loop comun.**
   - Un singur mecanism de aprobări și unul de notificări, folosite de toate feature-urile.
   - Orice acțiune cu impact (relocare, schimbare de preț, excepție de la regulile hostului, promisiuni către oaspete) cere aprobarea hostului.
   - Decizia ajunge înapoi la feature-ul care a cerut-o.
8. **UI.** Streamlit pentru simulare și depanare, plus extensia Chrome pentru host. Ambele consumă același API REST (JSON). FastAPI, logica de business, agenții, apelurile LLM și secretele rămân în backend. UI-ul trebuie să ofere:
   - un simulator: chat cu oaspetele, sesizare de la personal, anulare de rezervare, cerere de analiză a recenziilor, reset al lumii;
   - inboxul de aprobări;
   - notificările;
   - timeline-ul agentului (vezi §5).

### API REST comun

| Endpoint | Scop |
|---|---|
| `GET /properties` | Proprietățile hostului |
| `GET /bookings` | Rezervările hostului |
| `GET /notifications` | Notificări |
| `GET /approvals` | Aprobări în așteptare |
| `POST /approvals/{id}/decision` | Aprobă sau respinge |
| `GET /incidents` | Incidente |
| `POST /sim/guest-message` | Mesaj simulat pentru demo |
| `POST /integrations/{pms_provider}/sync` | Sincronizează proprietățile și rezervările conexiunii selectate (ex. `/integrations/ownerrez/sync`) |

Streamlit și extensia folosesc aceleași modele de răspuns și ID-uri interne. Extensia afișează proprietățile, notificările și aprobările în panoul lateral Chrome (Manifest V3). Simulatorul rămâne în Streamlit.

## 4. Ce am verificat despre integrările externe

### OwnerRez Stage (F1)

- În configul comun selectează `pms_provider=ownerrez`, mediul `stage` și URL-ul de bază al API-ului Stage. Pune credențialele în `.env`. Păstrează `hospitable` ca adaptor separat și `mock` ca opțiune implicită pentru demo local.
- Creează un cont în [OwnerRez Stage](https://appstage.ownerrez.com/join) și pregătește o proprietate, o rezervare și un oaspete de test. Folosește „Send as Guest” în Sandbox Messaging pentru a porni conversația. [Sandbox Messaging](https://www.ownerrez.com/support/articles/api-sandbox-messaging)
- Mesageria și webhook-urile cer o aplicație OAuth. Pentru contul propriu folosește „Grant Access To Me”. Pentru alte conturi poate fi necesar un acord de parteneriat. Configurează un URL HTTPS public pentru webhook-uri. [OAuth Apps](https://www.ownerrez.com/support/articles/api-oauth-app) · [Webhooks](https://www.ownerrez.com/support/articles/api-webhooks)
- În API for Apps verifică întâi autentificarea și citirea proprietăților și a rezervărilor. Testează apoi separat citirea și trimiterea mesajelor, recepția webhook-urilor și scrierea tarifelor sau a disponibilității, în funcție de permisiunile conexiunii și de operațiile disponibile în Stage.
- Demonstrează în mock operațiile indisponibile în Stage. Testează separat adaptorul Hospitable.

### Hospitable Public API v2 (F1)

- Adresa de bază: `https://public.api.hospitable.com/v2`.
- Autentificare: `Authorization: Bearer <HOSPITABLE_API_TOKEN>` (Personal Access Token, din `.env`).
- Accesul la API cere un plan plătit; trialul de 14 zile îl include.
- **Trimitere mesaj:** `POST /v2/reservations/{reservation_uuid}/messages`, body `{"body": "<text>"}`, răspuns `201`. Hospitable transmite mesajul pe canalul rezervării.
- **Citire mesaje:** `GET /v2/reservations/{uuid}/messages`. Câmpurile includ `reservation_id`, `conversation_id`, `platform`, `body`, `sender_type` (`host` | `guest`) și `sender`.
- **Webhook `message.created`:**
  - Se trimite pentru mesajele **ambelor** părți, inclusiv pentru cele trimise de noi prin API. Procesăm **doar** `sender_type == "guest"`, altfel apare o buclă infinită.
  - Răspundem imediat cu `200` și procesăm în background. Orice alt răspuns declanșează până la 5 retry-uri, deci **deduplicați după ID-ul mesajului**.
  - Mesajele mai vechi de 12 ore nu sunt trimise prin webhook.
- **Structura exactă a payload-ului de webhook nu e confirmată în documentație.** Parsați permisiv și salvați payload-urile brute ca să ajustăm parserul după primul webhook real. Butonul „Test” din Hospitable trimite ultimul element.
- Webhook-ul are nevoie de un URL public. Local se folosește un tunel (ngrok sau cloudflared).
- Nu e documentată o semnătură HMAC. IP-urile sursă indicate sunt în `38.80.170.0/24`; filtrarea pe IP e opțională.
- Mesajele există doar pe rezervări reale. Fără o rezervare reală, F1 rulează pe canalul simulat.
- **Endpoint-ul de scriere a prețului sau disponibilității (pentru F5) nu e confirmat.** Adaptorul real de calendar rămâne stub până la confirmare.

### Apify (F4, opțional F5)

- Actorii se rulează sincron prin `POST https://api.apify.com/v2/acts/{actor_id}/run-sync-get-dataset-items?token=...`, cu input JSON (de regulă URL-uri de anunț Airbnb).
- ID-ul actorului și forma inputului vin din config, pentru că actorii diferă între ei. Output-ul se normalizează în modelul nostru.
- Planul gratuit oferă $5 pe lună. Setați timeout explicit și tratați eșecul ca graceful degradation (folosiți ultimul snapshot salvat).
- Anunțul nostru demo e fictiv, deci nu are recenzii pe Airbnb. Adaptorul real folosește URL-ul unui anunț real comparabil, mapat la proprietatea noastră, iar recenziile se salvează ca snapshot cu sursa și data.

### Inside Airbnb (F5)

- Oferă CSV-uri per oraș: `listings.csv.gz`, `calendar.csv.gz`, `reviews.csv.gz`.
- Licența e CC BY 4.0, deci creditați sursa în UI.
- Sunt snapshot-uri trimestriale, nu prețuri live. Le folosim ca referință de piață, importate într-un tabel local.
- **Nu există orașe din România.** Orașul de piață e configurabil.

### Maparea ID-urilor (principiul central)

- În interiorul aplicației circulă **doar ID-uri interne**. ID-urile externe (ID-uri OwnerRez, UUID-uri Hospitable, URL-uri Airbnb) există doar în adaptoare, traduse printr-o tabelă de corespondențe.
- Fiecare rezervare separă `pms_provider` (`ownerrez`, `hospitable`, `mock`) de `source_channel` (`airbnb`, `booking_com`, `vrbo`, `direct`, `simulated`). `ChannelGateway` rutează mesajele prin adaptorul conexiunii PMS asociate rezervării. `source_channel` descrie originea rezervării.
- Un mesaj de pe o rezervare nemapată nu intră în fluxul de business: se loghează și se notifică hostul.
- `PropertiesPort.list_properties()` și `ReservationsPort.list_reservations()` sunt interfețe asincrone. Adaptoarele OwnerRez, Hospitable și mock implementează aceste porturi și declară capabilitățile disponibile pentru conexiunea folosită.
- `SyncService` citește și normalizează prin aceste porturi mai întâi proprietățile, apoi rezervările. Face upsert în SQLite și salvează corespondențele prin `IdMapper`, folosind conexiunea PMS, tipul entității și ID-ul extern. Sincronizările repetate actualizează aceleași înregistrări. Maparea manuală poate fi folosită la depanare.

## 5. LLM și agentul

### Ce înseamnă „agent” aici

Un agent e o **buclă scrisă de noi** în jurul modelului:

1. Trimitem: system prompt (obiectivul), istoricul și lista de unelte. Fiecare unealtă are nume, descriere și schemă JSON a parametrilor.
2. Modelul răspunde fie cu text, fie cu un `tool_call`.
3. **Codul nostru execută unealta, nu modelul.** Rezultatul se adaugă în istoric ca mesaj `role: "tool"`.
4. Apelăm modelul din nou. Acum vede rezultatul și decide singur pasul următor.
5. Repetăm până când modelul declară că a terminat sau ajungem la o limită de pași.

Bucla e una singură, reutilizabilă, în nucleul comun. Feature-urile își definesc doar prompturile și uneltele.

### Reguli de design

- **System promptul descrie obiectivul și condiția de terminare**, nu „ești un asistent”. Formatul: „obiectivul tău e X; ai terminat când Y e adevărat”.
- Agentul are o unealtă **`finish(result, evidence)`** pe care trebuie s-o apeleze explicit ca să încheie.
- **Execuție în mai mulți pași, cu unelte reale:** citiri din baza de date, verificări de calendar, apeluri de adaptoare, cereri de aprobare. Uneltele citesc datele noastre, nu le inventează.
- **Decizie dinamică.** Ordinea uneltelor **nu** e scrisă în cod. O alege modelul în funcție de rezultate.
- **Erorile se întorc modelului ca rezultat de unealtă**, nu se ascund. Așa agentul se poate adapta: încearcă altceva, cere informația lipsă, schimbă strategia.
- **Raționament inspectabil.**
  - Fiecare unealtă are un parametru obligatoriu `reason` („de ce fac asta acum”).
  - La fiecare pas se loghează: mesajele, unealta aleasă, argumentele, rezultatul, tokenii și costul.
  - UI-ul afișează totul ca timeline.
- Cifrele (prețuri, disponibilitate) vin **din unelte**, nu din model.
- Acțiunile cu impact trec prin mecanismul de aprobare (§3.7). Limitele de bani și regulile hostului se impun **în cod**, nu în prompt.

### Modelul nu are memorie

- Fiecare request e independent. Modelul vede doar ce îi trimitem în acel apel. La fiecare pas trimitem lista întreagă de mesaje.
- Starea agentului (istoric, pași, aprobări în așteptare) se păstrează de noi, nu de model.
- **La modelele cu reasoning**, detaliile de reasoning returnate trebuie păstrate în istoric între rundele de tool calling, altfel calitatea scade. Verificați în documentația OpenRouter cum se transmit înapoi.

### Optimizarea contextului

Contextul crește la fiecare pas, așa că trebuie ținut sub control:

1. **Scurtați output-urile uneltelor** (cel mai mare câștig). Limitați la câteva mii de caractere sau întoarceți doar ce contează. Preferați unelte țintite (căutare, interval, filtru) în locul celor care întorc tot.
2. **Fereastră glisantă.**
   - Păstrați mereu system promptul și obiectivul, plus ultimii N pași.
   - Pașii vechi îi înlocuiți cu o notă scurtă (ex. „pasul 3: verificat calendarul, noaptea anterioară e liberă”).
   - **Un `tool_call` și răspunsul lui se scot întotdeauna împreună**, altfel API-ul dă eroare.

### Alegerea modelului (OpenRouter)

- Comunicarea cu modelul se face prin OpenRouter, cu un client compatibil OpenAI.
- **Modelul e configurabil** (ex. variabila de mediu `MODEL=...`). Schimbarea lui înseamnă un singur string.
- Criteriile de alegere:
  - suport pentru tool calling (obligatoriu);
  - fiabilitate la tool calling: respectă schema, nu inventează argumente, nu intră în bucle;
  - context window;
  - latență;
  - cost.
- Planul de alegere:
  1. Pornim cu un model puternic, de gamă medie-sus, ca să depanăm logica noastră, nu limitele modelului.
  2. Pregătim 5–8 scenarii fixe de test din domeniul nostru și le rulăm pe 2–3 modele. Comparăm rata de succes, numărul de pași, costul și timpul. Facilitează asta cu un script de evaluare simplu.
  3. Eventual folosim două modele configurabile separat: unul puternic pentru decizii, unul ieftin pentru subtaskuri mecanice (rezumate, extragere de date).
- Trebuie să existe și un **LLM mock determinist**, ca aplicația și testele să ruleze fără cheie API.

## 6. Prima sarcină: scheletul

**Livrabil:** aplicația pornește local cu totul pe mock și fără niciun secret. Toate cele 5 fluxuri se pot declanșa din simulatorul din UI și se pot urmări (evenimente, timeline-ul agentului, aprobări, notificări).

Pentru fiecare feature, implementarea minimă dovedește doar că firul trece cap-coadă:

| Feature | Comportament minim |
|---|---|
| F1 | Webhook care filtrează, deduplică, mapează și publică evenimentul (testabil cu un payload de exemplu). La ieșire, `ChannelGateway` trimite prin adaptorul conexiunii PMS. |
| F2 | Sesizare → incident → cerere de aprobare cu cele două opțiuni (remediere / relocare) → acțiune după decizie. |
| F3 | Mesaj de la oaspete → bucla agentului cu cel puțin o unealtă care citește datele → răspuns trimis. |
| F4 | Cerere de analiză → recenzii din adaptor → recomandări trimise hostului. |
| F5 | Anulare → comparabile din adaptor → preț propus → aprobare → actualizare de calendar prin adaptor. |

**Mai cuprinde:**
- **Seed-ul lumii simulate** (date coerente relațional): un host cu regulile lui, câteva proprietăți cu facilități, calendar, rezervări (cel puțin una cu `pms_provider=mock` și `source_channel=airbnb`, pentru maparea demo), personal, recenzii mock, comparabile de piață mock. Seed-ul se validează la încărcare.
- **Teste pentru garanțiile critice:**
  - izolarea handlerilor (o eroare sau un timeout nu afectează alt feature);
  - filtrul anti-buclă și deduplicarea din F1;
  - maparea ID-urilor;
  - idempotența aprobărilor;
  - un test cap-coadă pe mock pentru fiecare flux.
- **Comenzi simple** pentru seed, pornire API, pornire UI și teste.
- **Documentație:** `README.md` (cum rulezi, cum comuți mock/real, cum faci tunelul), `docs/ARCHITECTURE.md` (deciziile tale) și un `README.md` scurt în fiecare feature (ce evenimente ascultă, ce emite, ce date deține).

### Structură pentru integrarea PMS și extensia Chrome

```text
staypilot/
├── app/                     # FastAPI, logica de business și agenții LLM
│   ├── core/                # SyncService, IdMapper
│   ├── ports/               # PropertiesPort, ReservationsPort și porturile existente
│   ├── adapters/
│   │   ├── mock/
│   │   ├── ownerrez/
│   │   └── hospitable/
│   └── routers/             # API REST comun
├── ui/                      # Simulator Streamlit
└── extension/               # React + TypeScript + WXT
    ├── entrypoints/
    │   ├── background.ts
    │   └── sidepanel/
    │       ├── index.html
    │       ├── main.tsx
    │       └── App.tsx
    ├── components/
    ├── lib/
    │   └── api.ts
    ├── wxt.config.ts
    └── package.json
```

## 7. Notițele fiecărui feature (fișiere CLAUDE.md per feature)

Fiecare feature are propriul `CLAUDE.md` **în folderul lui**. Claude Code îl citește când lucrează în acel folder.

- **Proprietarul feature-ului îl actualizează** pe măsură ce dezvoltă. Ceilalți nu îl editează.
- **Fișierul acesta (din rădăcină) rămâne comun și stabil.** Notițele specifice unui feature nu se scriu aici.
- **Lucrul la un feature:** citește întâi `CLAUDE.md`-ul feature-ului. Dacă afectează contractele comune (evenimente, modele) sau alt feature, consultă și `CLAUDE.md`-ul acelui feature.
- **Când închei o sarcină într-un feature:** propune actualizări pentru `CLAUDE.md`-ul lui (decizii, evenimente noi, probleme deschise).

La construirea scheletului, creează aceste 5 fișiere (F1–F5) după șablonul de mai jos:
- completează secțiunile „Scop” și „Contract”;
- lasă `Owner: neasignat`. Membrul care preia feature-ul își trece numele **în fișierul feature-ului**, nu aici, ca fișierul din rădăcină să nu fie modificat de fiecare.

Șablonul:

```markdown
# <Fx> — <nume feature>
Owner: neasignat

## Scop
<ce face, în 2–3 propoziții>

## Contract
- Ascultă: <evenimente>
- Emite: <evenimente>
- Date deținute: <tabele / fișiere proprii>
- Porturi folosite: <ex. llm, reviews>
- Cereri de aprobare: <tipuri>

## Stare curentă
<ce funcționează acum>

## Decizii
<decizie — motiv>

## Probleme deschise / TODO

## Note pentru Claude
<convenții, prompturi, capcane specifice acestui feature>
```

## 8. Reguli permanente pentru oricine scrie cod aici

- Un feature nu importă alt feature și nu importă adaptoare concrete.
- Contractele comune se modifică doar prin adăugare, în PR-uri mici, separate.
- Fără secrete în cod sau în git. Secretele stau în `.env`, iar `.env.example` conține doar numele variabilelor.
- Toate apelurile externe au timeout explicit.
- Când ceva din acest fișier e neclar sau intră în conflict cu o decizie tehnică mai bună, alege varianta mai simplă care respectă §3 și notează decizia în `docs/ARCHITECTURE.md`.
