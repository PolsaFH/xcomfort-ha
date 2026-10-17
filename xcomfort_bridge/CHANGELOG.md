# Endringslogg – xComfort MQTT-bro

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
