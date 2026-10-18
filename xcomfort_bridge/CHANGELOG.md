# Endringslogg – xComfort MQTT-bro

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
