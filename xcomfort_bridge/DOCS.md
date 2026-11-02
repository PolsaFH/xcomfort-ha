# xComfort MQTT-bro

Lokal styring av Eaton / Sensio xComfort via Home Manager. Ingen sky.

## Kom i gang

1. Installer og start **Mosquitto broker**, og legg til **MQTT**-integrasjonen.
2. Lag en egen Home Assistant-bruker til appen, f.eks. `xcomfort`.
3. Under **Konfigurasjon**: fyll inn **Home Manager IP**, **MQTT-brukernavn** og **MQTT-passord**.
4. Under **Info**: slå på **Start ved oppstart**, **Watchdog** og **Vis i sidepanelet**, og trykk **Start**.
5. Åpne **xComfort** i sidepanelet og følg veiviseren: **Søk**, velg enheter, gi dem navn og rom.

## USB-pinne (valgfritt)

Sett xComfort-pinnen **CKOZ-00/13** i Home Assistant-maskinen. Åpne **USB-pinne** på nettsiden og trykk
**Finn dimmere**. Lysene styres da direkte over radio, med Home Manager som reserve.

## Hjelp

Full dokumentasjon, feilsøking og eksempler:
https://github.com/PolsaFH/xcomfort-ha

Fant du en feil? Opprett en issue på GitHub, gjerne med loggen fra nettsiden (Logg → Last ned).
