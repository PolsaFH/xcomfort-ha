# xComfort MQTT-bro for Home Assistant

Styr et **Eaton / Sensio xComfort**-anlegg fra Home Assistant, helt lokalt.
Ingen eoptHome-app, ingen Eaton-sky, ingen internettilgang nødvendig.

Broen snakker direkte med xComfort **Home Manager** på det lokale nettverket
(samme protokoll som eoptHome-appen bruker) og legger alt inn i Home Assistant
automatisk via MQTT.

> [!WARNING]
> **Under utvikling.** Denne appen er ikke ferdig. Den er laget og testet på
> ett bestemt anlegg, og du kan oppleve feil eller ting som ikke fungerer på
> ditt. Vi jobber med å gjøre den klar og stabil for alle. Det vil komme
> endringer, også i innstillinger og enheter, så les
> [endringsloggen](xcomfort_bridge/CHANGELOG.md) før du oppdaterer, og ta en
> sikkerhetskopi av Home Assistant først. Si gjerne fra om problemer under
> *Issues*.
>
> **Work in progress.** This app is not finished yet and may not work on
> your installation. Expect changes, including to settings and entities.

> **English summary:** A Home Assistant app (add-on) that bridges an Eaton /
> Sensio xComfort *Home Manager* to Home Assistant over MQTT, fully local
> (TCP port 10023, no cloud). Lights, switches, sensors, thermostats, awnings
> and wall buttons show up automatically via MQTT discovery. The docs below
> are in Norwegian. Note: the device list is currently written for one
> specific installation, see [Tilpasse til ditt eget anlegg](#tilpasse-til-ditt-eget-anlegg).

---

## Innhold

- [Hva får du](#hva-får-du)
- [Slik fungerer det](#slik-fungerer-det)
- [Før du starter](#før-du-starter)
- [Installere](#installere)
- [Innstillinger](#innstillinger)
- [Nettsiden i Home Assistant](#nettsiden-i-home-assistant)
- [Veggknapper og automasjoner](#veggknapper-og-automasjoner)
- [Når du endrer noe i xSetup eller MRF](#når-du-endrer-noe-i-xsetup-eller-mrf)
- [Markiser](#markiser)
- [Tilpasse til ditt eget anlegg](#tilpasse-til-ditt-eget-anlegg)
- [Feilsøking](#feilsøking)
- [Sikkerhet](#sikkerhet)
- [For utviklere](#for-utviklere)

---

## Hva får du

| I xComfort | I Home Assistant | Merknad |
|---|---|---|
| Dimmere | `light` med lysstyrke | Endringer fra veggbryter vises i HA på under 1 sek |
| Av/på-aktuatorer | `switch` (valgfritt også `light`) | F.eks. terrassevarmere, vifte, taklamper |
| Markiser / jalousier | `cover` med Inn / Stopp / Ut og omtrentlig posisjon | Posisjon regnes ut fra kjøretid |
| Romtermostater | `climate` | Måltemperatur, romtemperatur og ventilåpning |
| Sol-, vind- og temperaturfølere | `sensor` | |
| Alarmer / innganger | `binary_sensor` | F.eks. ventilasjonsalarmer |
| Veggknapper (Generell inngang) | `event` | Kort trykk, langt trykk, slipp. Perfekt for automasjoner |

I tillegg får du en egen **nettside i sidepanelet** (xComfort) med status,
alle rom, knapper, logg og verktøy.

---

## Slik fungerer det

```
 Veggbrytere / radio            Lokalt nettverk                 Home Assistant
 ┌────────────┐  radio  ┌──────────────┐ TCP 10023 ┌──────────────┐ MQTT ┌───────────┐
 │ Dimmere,   │ ──────► │ xComfort     │ ◄───────► │ xComfort     │ ◄──► │ Mosquitto │ ◄──► HA-enheter
 │ knapper,   │         │ Home Manager │           │ MQTT-bro     │      │ (MQTT)    │
 │ markiser   │         │              │           │ (denne appen)│      └───────────┘
 └────────────┘         └──────────────┘           └──────────────┘
```

- Broen holder en forbindelse åpen mot Home Manager og får beskjed med en gang
  noe endrer seg (noen trykker på en bryter, et lys dimmes osv.).
- Kommandoer fra HA (slå på lys, kjør markise ut) sendes rett til Home Manager.
- Alt dukker opp i HA automatisk via **MQTT discovery**. Du trenger ikke skrive
  noe i `configuration.yaml`.
- Enhetene får faste `unique_id`-er, så du kan gi dem nye navn, rom og ikoner
  i HA uten at noe brekker.

---

## Før du starter

Du trenger:

1. **Home Assistant OS eller Supervised.** Det er der «Apper» (tidligere
   «tillegg» / add-ons) finnes. Container- og Core-installasjoner kan ikke kjøre
   apper.
2. **Mosquitto broker**-appen installert og startet, og **MQTT**-integrasjonen
   lagt til under *Innstillinger → Enheter og tjenester*.
3. En **egen MQTT-bruker** til broen. Enklest: lag en vanlig HA-bruker, for
   eksempel `xcomfort`, under *Innstillinger → Personer → Brukere*. Mosquitto
   godtar HA-brukere automatisk.
4. **Home Manager på samme nettverk** som Home Assistant, med **fast IP-adresse**
   (sett en DHCP-reservasjon i ruteren, ellers mister broen kontakten når
   adressen endrer seg).

Du trenger **ikke** eoptHome-appen, Eaton-kontoen eller internett for at
broen skal virke.

---

## Installere

1. I Home Assistant: **Innstillinger → Apper → Appbutikk**.
2. Trykk **⋮** øverst til høyre → **Repositories**.
3. Lim inn adressen til dette repoet:
   ```
   https://github.com/PolsaFH/xcomfort-ha
   ```
   og trykk **Legg til**.
4. Last inn siden på nytt. Finn **xComfort MQTT-bro** i listen og trykk
   **Installer**.
5. Gå til fanen **Konfigurasjon** og fyll inn minst:
   - **Home Manager IP**: IP-adressen til Home Manager.
   - **MQTT-brukernavn** og **MQTT-passord**: brukeren fra punkt 3 over.
6. Gå til fanen **Info**:
   - Slå på **Start ved oppstart**.
   - Slå på **Watchdog**, så starter HA broen på nytt hvis den skulle henge.
   - Slå på **Vis i sidepanelet** for å få nettsiden.
   - Trykk **Start**.
7. Åpne fanen **Logg**. Etter noen sekunder skal du se at broen kobler til Home
   Manager og MQTT, og at enhetene publiseres.
8. Gå til **Innstillinger → Enheter og tjenester → MQTT**. Der ligger nå alle
   rommene som enheter med lys, markiser osv.

### Oppdatere

Når en ny versjon er lagt ut, dukker det opp en **Oppdater**-knapp på appen.
Se [`xcomfort_bridge/CHANGELOG.md`](xcomfort_bridge/CHANGELOG.md) for hva som
er nytt. Ta gjerne en sikkerhetskopi av HA før store oppdateringer.

---

## Innstillinger

| Innstilling | Standard | Hva den gjør |
|---|---|---|
| Home Manager IP | | Lokal IP til Home Manager |
| Home Manager port | `10023` | Endres nesten aldri |
| MQTT-megler | `core-mosquitto` | Riktig hvis du bruker Mosquitto-appen |
| MQTT-port | `1883` | |
| MQTT-brukernavn / passord | | Brukeren broen logger inn med. Passordet lagres bare i appens innstillinger |
| Loggnivå | `info` | `debug` logger all trafikk til og fra Home Manager. Bruk ved feilsøking |
| Avlesningsintervall | `20` sek | Hvor ofte alt leses av som en ekstra sikkerhet |
| Hurtig status | på | Holder en forbindelse åpen og får endringer på under 1 sek. Kreves for knapper |
| Lysbrytere også som lys | av | Lager en ekstra `light` for hver av/på-bryter, så de havner under Lys i HA og Google Home |
| Markise-posisjon | på | Regner ut omtrentlig posisjon (0-100 %) fra kjøretid |
| Kjøretid per markise | | Sekunder fra helt inne til helt ute. Mål med stoppeklokke |
| Pause mellom markise-kommandoer | `0.5` sek | Hindrer at radiotelegrammer kolliderer |
| Send «stopp» to ganger | på | Alltid trygt, hjelper hvis det første telegrammet ikke kom fram |
| Gjenta «ut»/«inn» én gang | av | Test først. Noen motorer stopper hvis de får samme kommando to ganger |

---

## Nettsiden i Home Assistant

Trykk **xComfort** i sidepanelet. Siden har fem faner:

| Fane | Innhold |
|---|---|
| **Hjem** | «Alt fungerer» eller hva som er galt, nøkkeltall, markiser med Inn / Stopp / Ut, og «Hva skjer i huset»: en levende liste over trykk, lys og alarmer |
| **Rom** | Alle lys, brytere, markiser og termostater per rom. Trykk for av/på, dra for å dimme, «Slå av alt» per rom |
| **Knapper** | Alle veggknapper med siste trykk. Nye knapper legges til med ett trykk |
| **Logg** | Fargekodet logg med filter, søk, pause og nedlasting |
| **Verktøy** | Søk etter nye enheter, søk i alle objekter i Home Manager, sikkerhetskopi og rå trafikk |

Siden fungerer på mobil og har mørk modus.

---

## Veggknapper og automasjoner

xComfort-knapper kan brukes som «smarte knapper» i HA, så en knapp kan for
eksempel styre musikk, scener eller noe helt annet enn lys.

### 1. Gjør knappen synlig for Home Manager

En knapp blir bare synlig for broen hvis den har et **datapunkt** i Home
Manager:

1. I **MRF**: koble knappen (kanalen) til Home Manager eller en
   tilleggskontroller og overfør endringene.
2. I **xSetup**: under *Datapunkter*, sett typen til **Generell inngang** og
   **last opp** til anlegget.

### 2. Legg den til i broen

1. Trykk på knappen noen ganger.
2. Åpne nettsiden → **Knapper** (eller **Verktøy**). Under «Knapper du nettopp
   har trykket på» dukker den opp.
3. Trykk **Legg til**. Knappen blir en `event`-enhet i HA, i riktig rom.

### 3. Lag en automasjon

Lag en ny automasjon i HA, velg utløseren **Hendelse mottatt** på knappens
`event`-enhet, og velg hvilken trykk-type som skal reagere:

| Trykk-type (`event_type`) | Betyr |
|---|---|
| `kort_opp` | Kort trykk på øvre del |
| `kort_ned` | Kort trykk på nedre del |
| `langt_opp` | Holder inne øvre del |
| `langt_opp_slipp` | Slipper etter langt trykk opp |
| `langt_ned` | Holder inne nedre del |
| `langt_ned_slipp` | Slipper etter langt trykk ned |

Eksempel i YAML-modus (automasjonsredigereren → ⋮ → *Rediger i YAML*):

```yaml
alias: Stue knapp 4 - scene
triggers:
  - trigger: event.received
    target:
      entity_id: event.stue_brstue4
    options:
      event_type: [kort_opp]
actions:
  - action: scene.turn_on
    target:
      entity_id: scene.stue_dempet
mode: single
```

**Tips:**

- **Dobbeltrykk** finnes ikke i selve knappen, men kan lages i HA: vent
  0,8 sek med `wait_for_trigger` etter første trykk og se om det kommer ett
  til.
- **Volum eller dimming mens du holder:** start en løkke på `langt_opp` og
  stopp den på `langt_opp_slipp` (bruk `mode: restart`).
- Knappen gjør fortsatt det den er programmert til i xComfort (for eksempel
  styre et lys) i tillegg til automasjonen. Vil du bare bruke den i HA, ikke
  koble den til noen aktuator i MRF.

---

## Når du endrer noe i xSetup eller MRF

**Viktig:** Hver gang du laster opp fra xSetup, kan Home Manager gi mange
objekter **nye interne numre**. Det gjelder særlig innganger og knapper
(`IN_...`). Lys beholder som regel numrene sine.

Broen håndterer dette selv:

- Ved oppstart, hver 6. time og etter hvert søk sjekker den at alle knapper og
  alarmer fortsatt har riktig nummer.
- Har noe fått nytt nummer, finner den det igjen ut fra navnet og følger det
  opp automatisk. I loggen står det «har fått nytt nummer ... (fulgt opp
  automatisk)».
- Enhetene i HA (`entity_id`, `unique_id`) endres **ikke**, så automasjonene
  dine fortsetter å virke.

Etter en opplasting fra xSetup er det likevel lurt å:

1. Åpne nettsiden → **Verktøy** → **Start søk**.
2. Sjekke **Logg** for advarsler om noe som ikke ble funnet.
3. Ta en ny sikkerhetskopi (se under).

**Felle:** MRF kan vise et datapunkt som ledig selv om xSetup fortsatt bruker
det. Sjekk alltid i xSetup før du velger datapunktnummer i MRF.

### Sikkerhetskopi

- Nettsiden → **Verktøy** → **Sikkerhetskopi** laster ned broens kjente
  styre-objekter og listen over alle objekter i Home Manager.
- Ta også kopi av **MRF-prosjektet** og **xSetup-prosjektet** før du endrer noe
  i anlegget.

---

## Markiser

- Markisene har **ingen posisjonsmåler**. Broen regner ut posisjonen fra hvor
  lenge motoren har kjørt. Mål kjøretiden fra helt inne til helt ute og legg den
  inn under Innstillinger.
- Hvis noen bruker en veggbryter eller fjernkontroll som ikke går via Home
  Manager, vet ikke broen om det, og posisjonen kan bli feil. Kjør markisen helt
  inn eller helt ut én gang for å «nullstille» den.
- Kommandoer sendes med en liten pause imellom så radiotelegrammene ikke
  kolliderer.

---

## Tilpasse til ditt eget anlegg

Denne broen er laget og testet for **ett bestemt anlegg**. Protokollen er
generell for xComfort Home Manager, men listen over enheter er foreløpig
skrevet inn i koden:

| Hva | Hvor |
|---|---|
| Dimbare lys `(Sone, Navn, smuxId)` | `LIGHTS` i `xcomfort_bridge/xcomfort_local_control.py` |
| Av/på-aktuatorer | `SWITCHES` i samme fil |
| Sensorer og alarmer | `SENSORS` i samme fil |
| Markiser | `MARKISER` i samme fil, pluss kjøretider i innstillingene |
| Innganger som følges ekstra nøye | `OBJEKT_NAVN` i `xcomfort_bridge/xcomfort_mqtt_bridge.py` |

`smuxId` er enhetens nummer i Home Manager (objektet `D_<navn>`). Du finner
dem med **Verktøy → Start søk** og søkefeltet på nettsiden. Søket viser
alle objekter med navn og nummer.

**Knapper** trenger du ikke skrive inn i koden. De legges til fra nettsiden.

Vil du bruke broen på et annet anlegg og få dette gjort enklere (for eksempel
at enhetene oppdages automatisk), opprett gjerne en *issue*.

---

## Feilsøking

| Problem | Prøv dette |
|---|---|
| Ingen enheter dukker opp i HA | Sjekk at MQTT-integrasjonen er lagt til og at brukernavn/passord stemmer. Se appens **Logg** |
| Får ikke kontakt med Home Manager | Sjekk IP-adressen og prøv å pinge den. Har Home Manager fått ny IP? |
| Lys reagerer tregt eller ikke | Nettsiden → Hjem viser responstid. Gjentatte tidsavbrudd betyr ofte dårlig radiodekning til den dimmeren |
| En knapp sender ikke noe | Er den lagt inn som **Generell inngang** i xSetup og lastet opp? Står den under «Knapper du nettopp har trykket på»? |
| Knapp eller alarm virket før, men ikke etter en xSetup-opplasting | Verktøy → **Start søk**, og se Logg. Broen finner som regel igjen objektet selv |
| Markisen står på feil prosent | Kjør den helt inn eller helt ut én gang. Sjekk kjøretiden i innstillingene |
| Trenger mer detaljer | Sett **Loggnivå** til `debug`, gjenta problemet, og last ned loggen fra nettsiden |

---

## Sikkerhet

- Protokollen mellom broen og Home Manager er **ukryptert** (slik Eaton har
  laget den). Hold Home Manager på et lukket hjemmenett og **aldri** åpne port
  10023 mot internett.
- Bruk en egen MQTT-bruker til broen.
- **Ikke legg private filer i dette repoet.** MRF-eksporter
  (`Anlagenkonfiguration*.txt`) inneholder MRF-passordet i klartekst, og
  innloggings- og anleggsfiler fra Eaton-skyen inneholder nøkler.
  `.gitignore` stopper de vanligste, men sjekk alltid hva du laster opp.

---

## For utviklere

```
xcomfort-ha/
├── repository.yaml                  App-repo for Home Assistant
└── xcomfort_bridge/
    ├── config.yaml                  Versjon, innstillinger, nettside (ingress)
    ├── xcomfort_mqtt_bridge.py      Hovedprogrammet: MQTT, discovery, kommandoer
    ├── xcomfort_local_control.py    Protokollen mot Home Manager + enhetslister
    ├── hm_link.py                   Lytte-forbindelsen (hurtig status)
    ├── idmap.py                     Følger objekter som får nytt nummer
    ├── scanner.py                   Søk etter alle objekter i Home Manager
    ├── web.py, web/index.html       Nettsiden i sidepanelet
    ├── seed_cache.json              Kjente objekt-numre for raskere første start
    ├── translations/                Tekster til innstillingene
    ├── tests/test_bro.py            Tester (med falsk Home Manager)
    └── CHANGELOG.md
```

### Protokollen i korte trekk

Ren tekst over TCP port 10023. Hver melding er pakket inn i `0x01` (start) og
`0x02` (slutt):

| Retning | Melding | Betyr |
|---|---|---|
| → | `d_obj <id>` | Abonner på et objekt (Home Manager svarer med verdien og sender endringer) |
| ← | `SSN <id> <navn> <type> ... <verdi>` | Verdi eller endring |
| → | `set_value <id> <verdi>` | Sett en verdi (f.eks. lysstyrke 0-100) |
| → | `new_state <id> <verdi>` | «Utfør» |

Home Manager sender svar og endringer til **alle** åpne forbindelser.

### Kjøre testene på PC

```
cd xcomfort_bridge
pip install paho-mqtt
python -m unittest discover -s tests -v
```

Testene kjører også automatisk på GitHub ved hver opplasting (fanen
*Actions*). De bruker en falsk Home Manager, så du trenger ikke tilgang til
et ekte anlegg.

---

*Dette prosjektet er ikke laget av, støttet av eller knyttet til Eaton eller
Sensio. xComfort og eoptHome er varemerker for sine respektive eiere. Bruk på
eget ansvar.*
