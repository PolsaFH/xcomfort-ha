# Endringslogg – xComfort MQTT-bro

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
