<p align="center">
  <img src="xcomfort_bridge/logo.png" alt="xComfort for Home Assistant" width="320">
</p>

<h3 align="center">Eaton / Sensio xComfort i Home Assistant. Helt lokalt.</h3>

<p align="center">
  Ingen sky. Ingen app. Ingen ventetid.<br>
  Lys, brytere, markiser, termostater, sensorer og veggknapper rett inn i Home Assistant,<br>
  med en valgfri USB-pinne som styrer lysene direkte over radio.
</p>

<p align="center">
  <a href="README.md">🇬🇧 English</a> · <b>🇳🇴 Norsk</b>
</p>

<p align="center">
  <a href="https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2FPolsaFH%2Fxcomfort-ha"><img src="https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg" alt="Legg til i Home Assistant"></a>
</p>

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="images/home-dark.png">
    <img src="images/home.png" alt="Nettsiden i Home Assistant" width="900">
  </picture>
</p>

> [!WARNING]
> **Tidlig versjon.** Appen er laget på og testet grundig i ett hus med ca. 100 xComfort-enheter. Nå åpnes den
> for flere. Den gjør ingenting med anlegget ditt før du selv velger hva som skal inn, men ta en sikkerhetskopi av
> Home Assistant først, og si gjerne fra under [Issues](https://github.com/PolsaFH/xcomfort-ha/issues) hvis noe
> ikke virker.

> **Språk:** Nettsiden i appen er på engelsk. Beskrivelsene av innstillingene vises på norsk når Home Assistant
> står på norsk. Under står navnene på knapper og faner slik de vises i appen.

---

## Innhold

- [Hvorfor](#hvorfor)
- [Dette får du](#dette-får-du)
- [Kom i gang på 10 minutter](#kom-i-gang-på-10-minutter)
- [Docker (uten Home Assistant OS)](#docker-uten-home-assistant-os)
- [Nettsiden](#nettsiden)
- [USB-pinnen: lys på et øyeblikk](#usb-pinnen-lys-på-et-øyeblikk)
- [Veggknapper i automasjoner](#veggknapper-i-automasjoner)
- [Innstillinger](#innstillinger)
- [Slik virker det](#slik-virker-det)
- [Når du endrer noe i xSetup eller MRF](#når-du-endrer-noe-i-xsetup-eller-mrf)
- [Feilsøking](#feilsøking)
- [Begrensninger](#begrensninger)
- [Sikkerhet](#sikkerhet)
- [For utviklere](#for-utviklere)

---

## Hvorfor

xComfort er et solid og pålitelig radioanlegg med egen app og skytjeneste. Mange vil også at det skal virke
sammen med resten av smarthjemmet, og mange foretrekker å holde alt på det lokale nettverket.

Denne appen kobler **Home Manager** til Home Assistant over ditt lokale nettverk, på samme måte som eoptHome-appen
snakker med den. Resultatet er at xComfort blir en naturlig del av Home Assistant: i dashbord, automasjoner,
stemmestyring og Google Home eller Apple Home. eoptHome-appen og alt annet du bruker i dag virker som før.

**Hvilken kontroller?** Appen er laget for **Sensio X1 / xComfort Home Manager** som brukes med eoptHome og
xSetup. Har du Eatons nyere **xComfort Bridge** eller den eldre **Smart Home Controller (SHC)**, finnes det
andre integrasjoner for dem.

---

## Dette får du

| | |
|---|---|
| 💡 **Lys med lysstyrke** | Dimmere blir `light` i Home Assistant. Endringer fra veggbryteren vises på under ett sekund. |
| 🔌 **Av/på-brytere** | Terrassevarmere, vifter, lyskastere og andre aktuatorer blir `switch`. |
| ☀️ **Markiser** | Ut, inn og stopp, med posisjonen beregnet fra kjøretiden du setter. |
| 🌡️ **Termostater og sensorer** | Romtemperatur og ønsket temperatur som `climate`. Temperatur, lys, vind og andre verdier som sensorer. Innganger som dørkontakter kan bli av/på-sensorer. |
| 🔘 **Veggknapper som smarte knapper** | Kort trykk, langt trykk og slipp blir hendelser. Bruk dem til musikk, scener eller hva som helst. |
| 📡 **USB-pinne (valgfritt)** | Lysene styres direkte over radio og reagerer med en gang. Home Manager tar over hvis en dimmer ikke svarer. |
| 🧠 **Lærer selv** | Finner enhetene i Home Manager, og finner hvilke dimmere hvert lys styrer. Ingen nummer å skrive inn. |
| 🔁 **Tåler endringer** | Laster du opp fra xSetup og objektene får nye numre, finner appen dem igjen på navn. Automasjonene dine fortsetter å virke. |
| 🖥️ **Egen nettside** | Status, rom, knapper, logg og verktøy, rett i sidepanelet i Home Assistant. Mørk modus og mobil. |
| 📊 **Diagnostikk** | Responstid, radiotrafikk og feil som egne sensorer, så du kan få varsel hvis noe går galt. |
| 🔒 **100 % lokalt** | Ingen konto, ingen sky, ingen internett. Virker også når nettet er nede. |

---

## Kom i gang på 10 minutter

### Dette trenger du

- **Home Assistant OS eller Supervised.** Det er der apper (add-ons) finnes. Kjører du Home Assistant i
  Docker? Se [Docker](#docker-uten-home-assistant-os).
- **Mosquitto broker**-appen og **MQTT**-integrasjonen. Har du ikke det: *Innstillinger → Apper → Appbutikk →
  Mosquitto broker → Installer → Start*. HA foreslår så MQTT-integrasjonen av seg selv.
- **En egen bruker til appen**, f.eks. `xcomfort`: *Innstillinger → Personer → Brukere → Legg til*. Mosquitto
  godtar Home Assistant-brukere automatisk.
- **IP-adressen til Home Manager.** Gi den fast adresse i ruteren (DHCP-reservasjon), ellers mister appen
  kontakten den dagen adressen endrer seg.

Du trenger **ikke** eoptHome-appen, Eaton-kontoen eller internett.

### 1. Installer

Trykk på knappen øverst, eller gjør det for hånd:

1. *Innstillinger → Apper → Appbutikk → ⋮ → Repositories*
2. Lim inn `https://github.com/PolsaFH/xcomfort-ha` og trykk **Legg til**.
3. Finn **xComfort MQTT Bridge** i listen og trykk **Installer**.

### 2. Fyll inn tre ting

Under fanen **Konfigurasjon**:

| Felt | Eksempel |
|---|---|
| Home Manager IP | `192.168.1.50` |
| MQTT-brukernavn | `xcomfort` |
| MQTT-passord | passordet til brukeren over |

Resten kan stå som det er.

### 3. Start

Under fanen **Info**: slå på **Start ved oppstart**, **Watchdog** og **Vis i sidepanelet**, og trykk **Start**.

### 4. Finn enhetene dine

Åpne **xComfort** i sidepanelet. Første gang møter du en veiviser:

<p align="center"><img src="images/devices.png" alt="Legg til enheter" width="820"></p>

1. Trykk **Start search**. Appen går gjennom alt Home Manager har (10 til 20 minutter, påvirker ikke radioen).
2. Gi hver enhet et navn og et rom. Lys, brytere, markiser, termostater, sensorer og knapper står hver for seg.
   For markiser setter du også kjøretiden, og for sensorer enheten.
3. Trykk **Add selected to Home Assistant**.

Ferdig. Enhetene ligger nå under *Innstillinger → Enheter og tjenester → MQTT*, sortert per rom, og du kan
bruke dem som alt annet i Home Assistant.

---

## Docker (uten Home Assistant OS)

Kjører du Home Assistant i Docker (Container)? Da finnes ikke apper, men broen er en helt vanlig container og
kan kjøre for seg selv. Du trenger en MQTT-broker (for eksempel Mosquitto i Docker) og **MQTT**-integrasjonen i
Home Assistant.

> Denne måten å kjøre den på er ny. Si fra i en issue hvis noe ikke virker.

**1.** Lag en mappe, f.eks. `xcomfort`, med en undermappe `data` og fila `data/options.json`:

```json
{
  "xcomfort_ip": "192.168.1.50",
  "mqtt_host": "192.168.1.10",
  "mqtt_port": 1883,
  "mqtt_username": "xcomfort",
  "mqtt_password": "mqtt-passordet-ditt",
  "light_control": "hybrid"
}
```

`xcomfort_ip` er Home Manager / Sensio X1, og `mqtt_host` er maskinen som kjører MQTT-brokeren (ikke
`core-mosquitto`, det navnet finnes bare i Home Assistant OS). Alt annet fra [Innstillinger](#innstillinger)
kan legges til med de samme navnene som i [config.yaml](xcomfort_bridge/config.yaml). Det du utelater får
standardverdien.

**2.** Lag `docker-compose.yml` i samme mappe:

```yaml
services:
  xcomfort:
    build: https://github.com/PolsaFH/xcomfort-ha.git#main:xcomfort_bridge
    container_name: xcomfort-bridge
    restart: unless-stopped
    ports:
      - "8099:8099"
    volumes:
      - ./data:/data
      - /etc/localtime:/etc/localtime:ro
    # Bare hvis du har USB-pinnen (CKOZ-00/13):
    # devices:
    #   - /dev/bus/usb:/dev/bus/usb
```

**3.** Start den:

```bash
docker compose up -d --build
docker compose logs -f
```

Docker bygger imaget rett fra dette repoet, så du trenger ikke laste ned noe først.

**4.** Åpne `http://<ip-til-docker-maskinen>:8099` og følg veiviseren under **Devices**, akkurat som i
Home Assistant OS.

**Oppdater** til ny versjon med `docker compose up -d --build` igjen. Enhetene og innstillingene dine ligger i
`data`-mappen og blir med videre.

Greit å vite:

- **Nettsiden har ingen innlogging** når den kjører utenfor Home Assistant. Alle på nettverket ditt kan åpne
  den og styre lysene. Ikke gjør port 8099 tilgjengelig fra internett. Vil du bare nå den fra selve
  Docker-maskinen, bruk `"127.0.0.1:8099:8099"`.
- **USB-pinnen** må stå i før containeren starter. Setter du den i senere, start containeren på nytt med
  `docker compose restart`.
- **Vanlig `docker run`** virker også:
  `docker run -d --name xcomfort-bridge --restart unless-stopped -p 8099:8099 -v "$PWD/data:/data" xcomfort-bridge`
  etter `docker build -t xcomfort-bridge https://github.com/PolsaFH/xcomfort-ha.git#main:xcomfort_bridge`.

---

## Nettsiden

| Fane | Hva du finner |
|---|---|
| **Home** | «Everything is working», eller nøyaktig hva som er galt. Nøkkeltall, markiser og en levende liste over alt som skjer i huset. |
| **Rooms** | Alle enheter per rom, med filter for lys, brytere, markiser, varme og sensorer. Trykk for av/på, dra for å dimme, «Turn all off» per rom. Blyanten endrer navn og rom, og for markiser kjøretid og retning. |
| **Buttons** | Alle veggknapper med siste trykk. Trykker du på en ny knapp i huset, dukker den opp her og kan legges til med ett trykk. |
| **Devices** | Søk i Home Manager, legg til nye enheter, finn objekter og last ned sikkerhetskopi. |
| **USB stick** | Status for pinnen, automatisk læring av hvilke dimmere hvert lys styrer, og feilsøking. |
| **Log** | Fargekodet logg med filter, søk, pause og nedlasting, og rå trafikk fra Home Manager. |

<p align="center"><img src="images/rooms.png" alt="Rom" width="820"></p>

---

## USB-pinnen: lys på et øyeblikk

Home Manager sender lyskommandoer videre over radio, og mange anlegg er satt opp med flere sekunders dimmetid.
Med xComfort-pinnen **CKOZ-00/13** (den samme USB-pinnen som MRF bruker) i maskinen som kjører Home Assistant,
sender appen kommandoene **rett til dimmeren**. Lyset reagerer med en gang.

<p align="center"><img src="images/usb.png" alt="USB-pinne" width="820"></p>

1. Sett pinnen i Home Assistant-maskinen, helst midt i huset. Den kan settes inn og tas ut mens appen kjører.
2. Åpne **USB stick** på nettsiden og trykk **Find dimmers**. Appen sender en kommando via Home Manager til ett
   lys om gangen og lytter på hvilke dimmere som får den. Lysene endrer seg ikke synlig.
3. Ferdig. Lys med dimmere i kartet styres nå direkte.

**Trygt som standard.** Innstillingen **Lysstyring** (`light_control`) bestemmer hva som brukes:

| Valg | Betyr |
|---|---|
| `hybrid` (standard) | Pinnen først. Hver dimmer får 0,35 sekunder på å svare, og ett nytt forsøk. Har verken pinnen eller Home Manager sett at lyset endret seg etter 3 sekunder, sender Home Manager kommandoen. Bolker til mer enn 6 dimmere (f.eks. alle utelys) går rett via Home Manager, fordi det er raskere. Uten pinne virker alt som før. |
| `usb_stick` | Bare pinnen, uten reserve. |
| `home_manager` | Bare Home Manager. Pinnen brukes ikke. |

Brytere, termostater og markiser går alltid via Home Manager.

> Pinnen kan bare brukes av ett program om gangen. Skal du bruke MRF, flytt pinnen til PC-en og sett den
> tilbake etterpå. Appen bruker Home Manager så lenge pinnen er borte, og finner den igjen av seg selv.

<details>
<summary>Lage kartet selv fra MRF (for spesielt interesserte)</summary>

Har du MRF-eksporten og en xSetup-backup, kan kartet også lages med
[`tools/make_usb_map.py`](tools/make_usb_map.py) og lastes opp under *USB stick → Upload a map from a file*.
Formatet er `{"lys": {"<smuxId>": [serienummer, ...]}}`.
</details>

---

## Veggknapper i automasjoner

En xComfort-knapp kan styre helt andre ting enn lys: musikk, scener, garasjeport, hva som helst.

**1. Gjør knappen synlig for Home Manager.** I MRF: koble knappen til Home Manager og overfør. I xSetup: sett
datapunktet til **Generell inngang** og last opp.

**2. Legg den til.** Trykk på knappen i huset. Den dukker opp under **Buttons** på nettsiden. Trykk **Add to HA**.

**3. Lag en automasjon.** Knappen er en `event`-enhet med disse trykk-typene:

| `event_type` | Betyr |
|---|---|
| `short_up` / `short_down` | Kort trykk på øvre / nedre del |
| `long_up` / `long_down` | Holder inne øvre / nedre del |
| `long_up_release` / `long_down_release` | Slipper etter langt trykk |

```yaml
alias: Stue knapp 4 - kinomodus
triggers:
  - trigger: event.received
    target:
      entity_id: event.stue_brstue4
    options:
      event_type: [short_up]
actions:
  - action: scene.turn_on
    target:
      entity_id: scene.kino
mode: single
```

**Tips:**
- **Dobbeltrykk:** vent 0,8 sekunder med `wait_for_trigger` etter første trykk og se om det kommer ett til.
- **Volum eller dimming mens du holder inne:** start en løkke på `long_up` og stopp den på `long_up_release` (`mode: restart`).
- Knappen gjør fortsatt det den er programmert til i xComfort i tillegg. Vil du bare bruke den i Home
  Assistant, ikke koble den til noen aktuator i MRF.

---

## Innstillinger

| Innstilling | Standard | Hva den gjør |
|---|---|---|
| Home Manager IP | | Lokal IP til Home Manager. Må fylles inn. |
| Home Manager port | `10023` | Endres nesten aldri. |
| MQTT-megler / port | `core-mosquitto` / `1883` | Riktig hvis du bruker Mosquitto-appen. |
| MQTT-brukernavn / passord | | Brukeren appen logger inn med. Lagres bare i appens innstillinger. |
| Lysstyring (`light_control`) | `hybrid` | `hybrid`, `usb_stick` eller `home_manager`, se [USB-pinnen](#usb-pinnen-lys-på-et-øyeblikk). |
| Hurtig status (`instant_status`) | på | Holder en forbindelse åpen og får endringer på under ett sekund. Kreves for knapper. |
| Avlesningsintervall | `20` sek | Hvor ofte alt leses av når hurtig status er av. |
| Loggnivå | `info` | `debug` logger all trafikk til og fra Home Manager. |
| Markise-innstillinger | | Posisjon, pauser og dobbel stopp for alle markiser. Markiser du legger til får kjøretiden sin på nettsiden. |

---

## Slik virker det

```mermaid
flowchart LR
    subgraph Hus["Huset"]
        D["Dimmere, aktuatorer,<br/>knapper, markiser"]
    end
    subgraph Nett["Lokalt nettverk"]
        HM["xComfort<br/>Home Manager"]
    end
    subgraph HA["Home Assistant-maskinen"]
        APP["xComfort MQTT Bridge<br/>(denne appen)"]
        USB["USB-pinne<br/>CKOZ-00/13"]
        MQ["Mosquitto"]
        CORE["Home Assistant"]
    end
    D <-- "radio 868 MHz" --> HM
    HM <-- "TCP 10023" --> APP
    APP <--> MQ <--> CORE
    APP --- USB
    USB -. "radio, direkte (valgfritt)" .-> D
```

- Appen holder én forbindelse åpen mot Home Manager. Home Manager sier fra med en gang noe endrer seg.
- Kommandoer fra Home Assistant går rett til Home Manager, eller rett til dimmeren via USB-pinnen.
- Alt dukker opp via **MQTT discovery**. Ingen `configuration.yaml`.
- Hver enhet har en fast `unique_id`, så du kan gi den nytt navn, rom og ikon i Home Assistant uten at noe brekker.

---

## Når du endrer noe i xSetup eller MRF

Hver gang du laster opp fra xSetup, kan Home Manager gi objekter **nye interne numre**, særlig knapper og
innganger. Appen tar seg av det:

- Ved oppstart, hver 6. time og etter hvert søk sjekker den at alt har riktig nummer.
- Har noe fått nytt nummer, finner den det igjen på navn. I loggen står det «followed automatically».
- `entity_id` og `unique_id` i Home Assistant endres **ikke**.

Kjør gjerne **Devices → Search again** etterpå, og last ned en ny sikkerhetskopi.

> **Felle:** MRF kan vise et datapunkt som ledig selv om xSetup fortsatt bruker det. Sjekk alltid i xSetup før du
> velger datapunkt i MRF.

---

## Feilsøking

| Problem | Prøv dette |
|---|---|
| Ingen enheter i Home Assistant | Er MQTT-integrasjonen lagt til, og stemmer brukernavn og passord? Se fanen **Logg**. Har du lagt til enheter under **Devices**? |
| Får ikke kontakt med Home Manager | Sjekk IP-adressen. Har Home Manager fått ny adresse? |
| Søket finner ingen lys | Lys må finnes i xSetup (objektet `D_<navn>`). Se **Devices → Find an object** for hva Home Manager faktisk har. |
| Lys reagerer tregt | Se responstid på **Home**. Lange tider betyr ofte dårlig radiodekning eller lang dimmetid i MRF. USB-pinnen hjelper. |
| USB-pinnen blir ikke funnet | Sjekk at den sitter i Home Assistant-maskinen og at MRF ikke bruker den. Appen ser etter den hvert 10. sekund. |
| «No answer» fra noen lys under læring | De er trolig utenfor rekkevidde for pinnen. De styres fortsatt via Home Manager. Flytt pinnen nærmere og prøv igjen. |
| En markise går feil vei, eller posisjonen stemmer ikke | Trykk på blyanten på markisen under **Rooms**. Kryss av for **Swap directions**, eller juster kjøretiden (sekunder fra helt inne til helt ute). |
| En sensor har feil enhet | Trykk på blyanten på sensoren under **Rooms** og velg riktig enhet. |
| En knapp sender ikke noe | Er den **Generell inngang** i xSetup og lastet opp? Dukker den opp under **Buttons** når du trykker? |
| Trenger mer detaljer | Sett **Loggnivå** til `debug`, gjenta problemet, last ned loggen fra nettsiden og legg den ved en issue. |

---

## Begrensninger

Ærlig talt, dette gjenstår:

- **Markise-posisjonen er beregnet** ut fra kjøretiden, siden markisene ikke sier fra hvor de er. Kjør hver
  markise helt inn én gang etter at du har lagt den til, så vet appen hvor den starter.
- **Lysscener** som er lagret i Home Manager er ikke med ennå. Veggknapper og scener i Home Assistant virker fint
  i mellomtiden.
- **Søket kjenner igjen enheter på navnene xSetup gir dem** (`D_`, `R_`, `TH_`, `TS_`, `IN_` og så videre). Mangler
  noe? Finn det under **Devices → Find an object** (innganger kan legges til direkte der), eller opprett en issue
  med navnene du ser der.
- **Testet på ett anlegg** (Sensio X1 / Home Manager med tilleggskontroller). Andre oppsett skal virke, men er
  ikke bekreftet ennå.
- **USB-pinnen** er testet med CKOZ-00/13, og bruker de samme kommandoene som MRF bruker når du tester et lys.
- Appen er ikke laget av eller støttet av Eaton.

---

## Sikkerhet

- Home Manager er laget for å stå på et privat hjemmenett, og appen snakker bare med den der. Hold det slik: ikke
  gjør Home Manager tilgjengelig fra internett, for eksempel med portviderekobling i ruteren.
- Bruk en egen MQTT-bruker til appen, og hold Home Assistant oppdatert.
- Appen gjør ingenting med anlegget ditt før du har valgt enheter. Den kjenner igjen anlegget den ble laget på ved
  å sjekke navnene i Home Manager, og bruker ellers bare det du legger til selv.
- **Hold prosjektfilene dine private.** MRF-eksporter og xSetup-backuper beskriver hele anlegget ditt. Ikke del
  dem eller last dem opp noe offentlig sted. `.gitignore` i dette repoet stopper de vanligste.

---

## For utviklere

Se [den engelske README-en](README.md#for-developers) for filstruktur, protokollen, USB-pinnen og tester.

---

<p align="center">
  Laget i Norge for et hus med for mange lysbrytere. 💡<br>
  <a href="LICENSE">MIT-lisens</a> · <sub>Eaton, xComfort og Sensio er varemerker for sine respektive eiere. Dette prosjektet er uavhengig.</sub>
</p>
