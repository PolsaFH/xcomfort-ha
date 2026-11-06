# Changelog – xComfort MQTT Bridge

From 3.0.0 the changelog is written in English. Older entries are in Norwegian.

## 3.1.3 (2026-11-06)

### Fixed
- **On/off lights (R_) were not found on some installations.** The search expected the on and off objects close
  to the light, but on some installations they are further away. The search now looks at everything that belongs
  to the device, up to the next device. Thanks to the first tester for the object list.

### Improved
- Sensors that measure the temperature inside a dimmer or relay are now called «device temperature» and are not
  selected by default, so they don't fill up Home Assistant.
- Inputs that look like motion detectors or door contacts (PIR, door, window, contact) are suggested as on/off
  sensors instead of buttons.

## 3.1.2 (2026-11-04)

### Fixed
- Pressing **Search** many times in a row no longer starts the search over and over. It is also blocked while
  there is no contact with the Home Manager.
- The awning settings are only written to the log when the installation has awnings.

## 3.1.1 (2026-11-04)

### Fixed
- **Clear message when the Home Manager cannot be reached.** The Home tab used to say «Connected to the Home
  Manager, but without a live connection» even when the app had no contact at all. It now says «Cannot reach
  the Home Manager» and explains the likely cause (wrong IP address, wrong port, or the Home Manager is off).
- The search on the Devices tab explains connection errors the same way instead of only showing the error code.
- The welcome guide no longer marks «Connect» as done before the Home Manager has answered.

## 3.1.0 (2026-11-03)

Awnings, thermostats and sensors can now be found and added from the web page, on any installation.

### New
- **The search finds awnings, thermostats and sensors**, in addition to lights, switches and buttons. Add them
  under **Devices** like everything else.
  - **Awnings:** set the running time (seconds from fully in to fully out) when adding. The pencil under
    **Rooms** changes it later, and can swap the directions if extend and retract are the wrong way round.
  - **Thermostats** get the room from the Home Manager name.
  - **Sensors:** the unit (°C, lux, m/s, % and so on) is guessed from the name and can be changed.
- **Inputs can be added as on/off sensors** instead of buttons, for example a door contact or a motion detector.
- **Sensors filter** on the **Rooms** tab, with tiles for sensors and on/off sensors.

### Fixed
- **Long names cut short by the Home Manager** (around 29 to 30 characters) are now matched correctly, so lights
  and switches with long names are no longer missed or paired with the wrong objects.
- **More reliable switch detection.** A switch is only suggested when both its on and off objects are found.
- **The Rooms tab lost its search field and filters** after the first update of the page. They now stay.
- The «Reset awnings» button is only created on the installation the app was built for.

## 3.0.0 (2026-11-03)

Everything you see is now in English, so the app can be used outside Norway. Norwegian is kept where Home
Assistant supports it.

### Breaking changes
- **Button event types are renamed.** Update automations that use them:

  | Before | Now |
  |---|---|
  | `kort_opp` / `kort_ned` | `short_up` / `short_down` |
  | `langt_opp` / `langt_ned` | `long_up` / `long_down` |
  | `langt_opp_slipp` / `langt_ned_slipp` | `long_up_release` / `long_down_release` |
  | `kode_6` … `kode_9` | `code_6` … `code_9` |

  The event attribute `kode` is now called `code`.
- **Settings have new names** (e.g. `lysstyring` is now `light_control`, and its value `pinne` is now
  `usb_stick`). Home Assistant does not carry the old values over, so **check the Configuration tab after
  updating**. The defaults are the same as before.
- Diagnostic entities and the «Reset awnings» button have English names. Entity IDs are unchanged.

### New
- The web page, log messages and setting descriptions are in English.
- Setting descriptions in Norwegian (shown automatically when Home Assistant is set to Norwegian).
- README in English, with a full Norwegian version (`README.no.md`).
- MIT license.
- The USB map tool is now `tools/make_usb_map.py`.

## 2.8.0 (2026-11-02)

Klar for andre anlegg enn det appen ble laget på.

### Nytt
- **Virker på alle anlegg.** Før var listen over lys, brytere, markiser og
  termostater skrevet inn i koden for ett bestemt hus. Hos andre ville de
  numrene pekt på helt andre ting i Home Manager. Nå sjekker appen ved første
  oppstart om navnene i Home Manager stemmer. Gjør de ikke det, starter appen
  tom og lar deg velge enhetene selv. Det opprinnelige anlegget merker ingen
  forskjell.
- **Veiviser for nye brukere.** Hjem-fanen viser hva du må gjøre når appen er
  ny: søk, velg, ferdig.
- **Ny fane «Enheter».** Søk i Home Manager, og legg til lys, brytere og
  knapper med eget navn og rom i én omgang. Knapper får rom og navn foreslått
  ut fra navnet i Home Manager.
- **Endre navn og rom** på enheter du har lagt til, med blyanten under Rom og
  Knapper. Der kan de også fjernes. Gjelder nå lys og brytere, ikke bare
  knapper.
- **USB-pinnen lærer kartet selv.** «Finn dimmere» under USB-pinne sender en
  kommando via Home Manager til ett lys om gangen og lytter på hvilke dimmere
  som får den. Ingen MRF-eksport, xSetup-backup eller PC-skript trengs lenger.
  Hver dimmer må høres to ganger for å telle, så tilfeldig trafikk i huset
  ikke blir med.
- **Egen fane for USB-pinnen**, med tydelige tall i stedet for en lang
  tekstlinje. Rå trafikk fra Home Manager er flyttet til Logg.
- Ikon og logo i appbutikken, og en kort veiledning under appens
  Dokumentasjon-fane.

### Rettet
- **USB-pinnen ble ikke funnet igjen etter at den var tatt ut og satt inn,
  før appen ble startet på nytt.** USB-biblioteket husket bare enhetene som
  fantes da appen startet, og får ikke beskjed om nye enheter inne i appen.
  Nå starter appen en ny USB-økt hver gang den ser etter pinnen, og lager
  selv enhetsfilen hvis den mangler. Pinnen blir funnet innen 10 sekunder.
- Melding om at pinnen ikke kunne åpnes skrives bare én gang, ikke hvert 10.
  sekund.
- Knappen «Resett markiser» lages bare når det finnes markiser.
- Faste versjoner av paho-mqtt og pyusb i appen, så en ny versjon av et
  bibliotek ikke kan ødelegge noe.

## 2.7.1 (2026-11-01)

Gjennomgang av koden og loggene fra 2.7.0. Ingen nye funksjoner, bare rettelser.

### Rettet
- **Store bolker gikk tregt via pinnen (hybrid).** Loggen viste at 16 utelys
  tok 3,3 sekunder via pinnen, og 12 av dem måtte likevel sendes på nytt via
  Home Manager. Home Manager tok de samme 12 på 0,15 sekunder, og alle ble
  bekreftet. Nå går bolker til mer enn 6 dimmere rett via Home Manager i
  hybrid. Enkeltlys og små grupper går fortsatt via pinnen. Valget «pinne»
  er uendret.
- Lys som pinnen fikk kvittering på, ble likevel telt som «ubekreftet» i
  diagnostikken etter 15 sekunder hvis Home Manager ikke fikk med seg
  endringen. Nå regnes de som bekreftet med en gang, med riktig responstid.
  (Det sto i endringsloggen for 2.7.0, men var ikke gjort helt.)
- Liten risiko for en unødvendig «lesefeil» i loggen akkurat når pinnen ble
  koblet fra under sending. Rettet.
- Fjernet en unødvendig pause på 0,06 s etter siste dimmer i hver bolk.
- Tålte ikke eldre versjoner av USB-biblioteket (pyusb før 1.2). Nå gjør den
  det.
- Kommentar i `usbstick.py` sa at nivået sendes i prosent. Det sendes som
  0–255.
- README beskrev feil ventetid for reserven (1,5 s). Oppdatert.

## 2.7.0 (2026-11-01)

### USB-pinnen er mer pålitelig
- Pinnen sender nå til én dimmer om gangen og venter kort (maks 0,35 s)
  på kvitteringen før den sender til neste. Før ble alt sendt rett etter
  hverandre, og da kolliderte kvitteringene med neste melding på radio.
  Kommer det ikke kvittering, prøver pinnen én gang til før Home Manager
  tar over. Store bolker (f.eks. «slå av alt») venter maks 2,5 sekunder
  totalt.
- HA viser ny tilstand med en gang, før sendingen er ferdig.
- Lys som er kvittert av pinnen regnes som bekreftet med en gang.

### Feilsøking
- Nytt under Verktøy → USB-pinne: **Radiomeldinger til og fra dimmerne**.
  Viser hva Home Manager sender til hver dimmer og hva dimmerne melder
  tilbake, sammen med siste kommando fra HA. Brukes til å sammenligne
  lysstyrke via pinnen og via Home Manager.
- Feilsøkingsdelene lukker seg ikke lenger når siden oppdateres.

## 2.6.2 (2026-11-01)

### Rettet: USB-pinnen var ustabil
- Broen leste 64 bytes om gangen fra pinnen, men pinnen sender pakker på
  62 bytes. Da ventet USB-biblioteket på enda en pakke etter hver melding.
  Enkeltmeldinger, som kvitteringer fra dimmerne, ble borte, og to
  meldinger tett etter hverandre ga feilen «[Errno 75] Overflow». Ved den
  feilen koblet broen fra pinnen og brukte Home Manager i opptil 30
  sekunder før den prøvde igjen. Nå leses nøyaktig én pakke om gangen.
- En enkelt lesefeil kobler ikke lenger fra pinnen. Bare 20 feil på rad gjør
  det.

## 2.6.1 (2026-10-30)

### Rettet (USB-pinne)
- **Feil lysstyrke via pinnen.** Dimmerne bruker 0–255 på radio, ikke
  0–100. 40 % ble derfor sendt som omtrent 16 %. Nå regnes prosent om
  riktig, så pinnen og Home Manager gir samme lysstyrke.
- **«0 bekreftet» og alt sendt på nytt via Home Manager.** Pinnen i
  HA-maskinen får ikke med seg kvitteringene fra dimmerne. Nå godtas også
  lampas egen statusmelding til Home Manager som bekreftelse, og fristen er
  3 sekunder. Bare lys som verken kvitterer eller melder status sendes på
  nytt via Home Manager.
- En gammel kommando sendes aldri på nytt hvis en nyere kommando er gitt i
  mellomtiden.

### Nytt
- Feilsøking under Verktøy → USB-pinne: hvor mange rammer, radiomeldinger
  og kvitteringer pinnen har mottatt, og de siste rammene. Hører pinnen
  ingenting på radio på 2 minutter, står det i loggen.

## 2.6.0 (2026-10-30)

### Nytt: velg hvordan lys styres
- Ny innstilling **Lysstyring** i Konfigurasjon-fanen:
  - **hybrid** (standard): USB-pinnen først. Mangler pinnen, eller svarer
    ikke en dimmer innen 1,5 sekunder, sendes kommandoen via Home Manager.
  - **pinne**: bare USB-pinnen, uten reserve. Virker ikke pinnen, eller en
    dimmer ikke svarer, skrives det i loggen, men det sendes ikke via Home
    Manager.
  - **home_manager**: bare Home Manager, som før pinnen. Pinnen åpnes ikke.
- Lys uten USB-kart går alltid via Home Manager. Brytere, termostater og
  markiser går alltid via Home Manager.
- Erstatter «Bruk USB-pinne» fra 2.5.0. Var den slått av, gjelder det
  fortsatt til du velger noe annet.
- Nettsiden viser valgt lysstyring under Verktøy → USB-pinne.

## 2.5.0 (2026-10-30)

### Nytt: USB-pinne for direkte radio (valgfritt, eksperimentelt)
- Står xComfort-pinnen **CKOZ-00/13** (den samme som brukes i MRF) i
  HA-maskinen, sender broen lyskommandoer rett ut på radio til dimmerne.
  Lysene reagerer da med en gang, uten omveien via Home Manager.
- Broen trenger et **USB-kart** som sier hvilke dimmere (serienumre) som
  hører til hvert lys. Det lages på PC-en med `verktoy/lag_usb_kart.py` og
  lastes opp under **Verktøy → USB-pinne** på nettsiden. Kartet lagres bare
  i appen, ikke i repoet.
- **Home Manager er alltid reserve:** Hver dimmer kvitterer på radio. Kommer
  det ikke kvittering fra alle dimmerne i et lys innen 1,5 sekunder, sendes
  samme kommando via Home Manager. Er pinnen ikke satt i, trekkes ut eller
  slutter å svare, brukes Home Manager som før, og broen prøver å finne
  pinnen igjen hvert 30. sekund.
- Brytere, termostater og markiser går fortsatt via Home Manager.
- Ny innstilling **Bruk USB-pinne** (på som standard). Nettsiden viser om
  pinnen er tilkoblet, hvor mange lys som har serienumre, og hvor ofte
  reserven via Home Manager har blitt brukt.
- Pinnen kan ikke brukes i MRF samtidig. Flytt den til PC-en når du skal
  programmere, og tilbake etterpå.

## 2.4.0 (2026-10-30)

### Raskere lys
- Lys- og bryterkommandoer sendes nå rett på den åpne lytte-forbindelsen
  til Home Manager. Før åpnet broen en ny forbindelse for hver kommando og
  ventet på handshake fra Home Manager først. Det tok tid hver gang.
- Broen venter kortere på å samle kommandoer som kommer samtidig (0,05 s i
  stedet for 0,15 s), så enkeltkommandoer går ut raskere. Scener som
  styrer flere lys samtidig sendes fortsatt samlet.
- Hvis lytte-forbindelsen er nede, eller et lys er nytt og styre-objektene
  må letes fram, brukes den gamle veien automatisk.
- Loggen viser nå hvor lang tid det gikk fra HA ba om noe til kommandoen
  var sendt, og hvilken vei den gikk, for eksempel «via lytte-forbindelsen».

## 2.3.1 (2026-10-18)

### Endret
- Innstillingene er ikke lenger forhåndsutfylt med IP-adresse og
  MQTT-brukernavn fra utviklerens eget anlegg. Nye brukere må fylle inn
  **Home Manager IP**, **MQTT-brukernavn** og **MQTT-passord** selv.
  Har du allerede lagret innstillingene dine, beholdes de som før.
- Er Home Manager IP tom, stopper broen med en tydelig melding i loggen
  om hva som må fylles inn, i stedet for å prøve å koble til ingenting.
- Bedre forklaring av innstillingene i Konfigurasjon-fanen.

## 2.3.0 (2026-10-18)

### Ny nettside (sidepanelet «xComfort» i HA)
- **Hjem:** status i klartekst («Alt fungerer» eller hva som er galt),
  nøkkeltall (lys på, responstid, kommandoer, radiotrafikk), markiser med
  posisjon og Inn/Stopp/Ut, graf over responstid, og **«Hva skjer i
  huset»**: en levende liste over knappetrykk, lys og brytere som endres,
  markiser og alarmer.
- **Rom:** alle lys, brytere, markiser og termostater gruppert per rom.
  Trykk på et lys for av/på, dra for å dimme, «Slå av alt» per rom. Filtrer
  på rom, type, søk eller «bare det som er på».
- **Knapper:** alle knapper med siste trykk (lyser opp når noen trykker),
  nye knapper legges til med ett trykk, knapper lagt til fra nettsiden kan
  fjernes, og en forklaring av trykk-typene til bruk i automasjoner.
- **Logg:** fargekodet, filter på nivå (alt / info / advarsler og feil),
  søk, pause og nedlasting.
- **Verktøy:** søk etter enheter med fremdrift, forslag, søk i alle
  objekter, sikkerhetskopi og rå trafikk samlet ett sted.
- Mørk modus, fungerer på mobil, og husker hvilken fane du var på.

## 2.2.4 (2026-10-18)

### Endret
- Knapper lagt til fra nettsiden havner nå på riktig rom-enhet i HA (for
  eksempel «Stue») med kort navn («BRstue1»), i stedet for enheten
  «Knapper» med navnet «Knapper Hus BRstue1». Navn og rom følger det
  objektet heter i Home Manager nå, også etter endringer i xSetup.
  unique_id er uendret.

## 2.2.3 (2026-10-18)

### Nytt
- Verktøy viser øverst «Knapper du nettopp har trykket på»: innganger som
  har sendt noe, men ikke er lagt til ennå. De kan legges til med ett trykk,
  uten å vente på et fullt søk.

## 2.2.2 (2026-10-18)

### Rettet
- Knapper og innganger blir funnet igjen også når SONEN endres i xSetup.
  Home Manager har sonen i navnet (IN_Hus_BRstue1 ble IN_Stue_BRstue1), så
  broen sammenligner nå bare første og siste del av navnet.

## 2.2.1 (2026-10-18)

### Rettet
- Fjernet falske «Ukjent knapp/inngang»-linjer i loggen ved oppstart. Det var
  Home Manager sine svar på broens egen nummersjekk, ikke trykk.

## 2.2.0 (2026-10-18)

### Rettet
- **Ventilasjonsvarslene (Drift, Tett filter, Overoppheting) sluttet å
  oppdatere seg etter opplasting fra xSetup.** Home Manager nummererte om 506
  objekter, blant dem alle innganger. Broen følger nå med på innganger og
  knapper ved NAVN: den sjekker ved oppstart, hver 6. time og etter hvert
  fulle søk i Verktøy, og finner selv det nye nummeret. Ingenting endres i
  HA (samme entity_id og unique_id). Endringer skrives i loggen.

## 2.1.3 (2026-10-18)

### Endret
- Knappe-hendelser har nå lesbare navn i stedet for `kode_N`:
  `kort_opp`, `kort_ned`, `langt_opp`, `langt_opp_slipp`, `langt_ned`,
  `langt_ned_slipp`. Kartlagt med BR stue satt opp som «Generell inngang» i
  xSetup. Selve koden følger fortsatt med som `kode`.

## 2.1.2 (2026-10-17)

### Rettet
- Søket etter enheter stoppet på nummer 270 000, men Home Manager har
  objekter helt opp mot det, og nye objekter fra MRF får høyere numre. Søket
  fortsetter nå til det har vært 30 000 tomme numre på rad.

## 2.1.1 (2026-10-17)

### Nytt
- Verktøy-fanen har et søk i ALLE objektene skanneren fant (navn eller
  nummer), og hele lista kan lastes ned. Hvilket som helst objekt kan legges
  til som knapp/hendelse, ikke bare de som heter `IN_...`. Nyttig for å finne
  nye veggbrytere (f.eks. BR stue) som Home Manager har gitt et annet navn.

## 2.1.0 (2026-10-17)

Ingen unique_id-er eller entity_id-er er endret. Alt nytt kommer i tillegg.

### Nytt
- **Hurtig status.** Broen holder nå én forbindelse åpen mot Home Manager
  og får endringer i det øyeblikket de skjer (typisk under 1 sek), også når
  noen bruker en bryter på veggen. Før kunne det ta opptil 20 sek. Den
  vanlige avlesningen kjører fortsatt, men bare hvert 5. minutt så lenge
  lytte-forbindelsen er frisk (og hvert 20. sek igjen hvis den faller ut).
  Kan slås av med `hurtig_status`.
- **Egen nettside i sidepanelet i HA («xComfort»).** Oversikt med
  tilkobling, responstider og radiotrafikk, alle enheter med testknapper,
  levende logg, rå trafikk fra Home Manager og verktøy.
- **Knapper som hendelser.** Markiseknapp 1–3 ute (og knapper du legger til
  selv) blir `event`-enheter i HA som kan brukes direkte i automasjoner.
- **Søk etter nye enheter.** Verktøy-fanen kan lete gjennom hele Home
  Manager og foreslå lys, brytere og knapper broen ikke kjenner. Huk av og
  trykk «Legg til», så dukker de opp i HA uten ny versjon av appen.
- **Raskere oppstart.** Appen har med seg en liste over kjente
  styre-objekter, så den slipper å lete etter dem ved første start.
- **Lysbrytere som lys (valgfritt).** `lysbrytere_som_lys` lager en ekstra
  lys-enhet for hver av/på-lysbryter. Bryter-enhetene beholdes.
- **Måling av responstid.** Når HA dimmer eller skrur på et lys, måler broen
  hvor lang tid det tar før lampa selv melder tilbake. Vises i nettsiden og
  som sensorer («Responstid siste/snitt», «Lys bekreftet/ubekreftet»).
- **Radiotrafikk.** Ny sensor viser antall radiokvitteringer siste 5 min.
- **Automatiske tester på GitHub** ved hver opplasting.

### Endret
- Fjernet sporing av kvittering per markisekommando (og sensorene
  «uten svar», «kvittert», «uten kvittering»). Home Manager sine
  kvitteringer gjelder hele radioen, ikke én kommando. De gamle sensorene
  fjernes automatisk fra HA.
- Tilstander som ikke er endret sendes ikke på nytt til MQTT hvert 20. sek.
- Helsesjekken er flyttet inn i nettsiden (samme adresse som før).

## 2.0.2 (2026-10-17)

### Rettet
- Fjernet falske advarsler «fikk INGEN kvittering». Home Manager sender ikke
  kvittering (ACK_DIRECT) for hver markisekommando, så en manglende
  kvittering betyr ikke at kommandoen forsvant. Logges nå bare på
  debug-nivå mens vi kartlegger når kvitteringene faktisk kommer.

## 2.0.1 (2026-10-17)

### Rettet
- **Boblebad ble stående ute etter «Resett markiser».** Den ekstra
  «stopp»-en fra en tidligere kommando kunne komme rett etter en ny «inn» og
  stoppe markisen igjen. Det skjedde da reset ble trykket mens «Kjør ut
  markiser» holdt på å avslutte. Nå avbryter enhver ny kommando alt som var
  planlagt for samme markise.

### Nytt
- **Kvittering fra markisene.** Home Manager forteller oss når selve
  markise-aktuatoren har bekreftet at den fikk telegrammet («ACK_DIRECT»).
  Broen følger nå med på dette. Kommer det ingen kvittering innen 6 sek,
  skrives en ADVARSEL i loggen og telles i diagnostikken
  («uten kvittering»). Da ser vi endelig hvilke kommandoer som ikke kom fram.

## 2.0.0 (2026-10-17)

Ingen unique_id-er eller entity_id-er er endret. Lys, brytere og termostater
styres på samme måte som før.

### Markiser
- Egen kø og egen tråd. Markisekommandoer blokkerer ikke lenger MQTT og
  venter ikke på pollingen (før kunne de vente flere sekunder, i verste fall
  25+ sek).
- Fast minste pause mellom radiotelegrammer til markisene (standard 0,5 s),
  så tre kommandoer ikke går tett i tett.
- «Stopp» sendes to ganger (0,8 s mellom). Alltid trygt.
- Valgfritt (AV som standard): send «ut»/«inn» én ekstra gang. Skal bare
  slås på etter test.
- Én forbindelse til Home Manager per markise-sekvens i stedet for én per
  kommando, og et nytt forsøk hvis forbindelsen var brutt.
- Tidsbasert posisjon 0–100 % og «sett posisjon» fra HA (kan slås av i
  innstillingene). Kjøretidene stilles i appens innstillinger. Posisjonen
  lagres og overlever omstart.
- Råsvaret fra Home Manager logges for hver markisekommando.

### Lys, brytere, termostater
- Bare siste kommando per enhet sendes når flere kommer tett (slider).
- Et lys eller en bryter som ikke kan finnes, letes ikke etter på nytt i
  hver pollrunde (før kunne det holde alt annet igjen i ~25 sek).

### Drift og pålitelighet
- Sjekker at Home Manager faktisk svarer ved tilkobling (før gikk den videre
  etter 5 sek uansett), og prøver opptil 3 ganger med økende ventetid.
  Kommandoer som ikke kommer fram logges som FEIL og telles.
- Pollingen holder ikke låsen i flere sekunder i strekk.
- Kobler til MQTT på nytt automatisk, også hvis megleren ikke er oppe når
  broen starter.
- Sender discovery og tilstand på nytt når Home Assistant restarter
  (`homeassistant/status`).
- QoS 1 på kommandoer og discovery.
- Feil i én melding stopper ikke broen.
- Helsesjekk på port 8099 for Supervisor sin watchdog.
- Lukker forbindelser pent (FIN i stedet for RST).

### Diagnostikk (ny enhet «xComfort bro»)
- Home Manager tilkoblet, siste kontakt, kommandoer sendt/feilet, siste
  feil, varighet pollrunde, markise-kø, versjon.
- Per markise: kommandoer sendt, feilet, uten svar, siste kommando.

### Logging
- Tidsstempel med millisekunder på hver linje.
- Loggnivå i innstillingene (debug viser alt som sendes og mottas).

### Annet
- Alle innstillinger i appens options.
- Tester mot en simulert Home Manager: `python -m unittest discover -s tests`.
- Nytt verktøy `verktoy/markise_diagnose.py` (bare lesing) for feilsøking.

## 1.0.0
- Første versjon.
