# xComfort MQTT-bro for Home Assistant

Lokal bro mellom en Eaton/Sensio xComfort Home Manager (TCP port 10023) og
Home Assistant via MQTT discovery. Lys, brytere, sensorer, termostater og
markiser. Ingen sky.

## Installere i Home Assistant
1. Innstillinger → Apper → Appbutikk → ⋮ (øverst til høyre) → Repositories.
2. Lim inn adressen til dette repoet og trykk Legg til.
3. Finn «xComfort MQTT-bro» i butikken, installer, fyll inn innstillingene og start.

## Oppdatere
Når en ny versjon er lagt ut her, dukker det opp en «Oppdater»-knapp på appen
i Home Assistant. Se `xcomfort_bridge/CHANGELOG.md` for hva som er nytt.

## Tester (på PC)
    cd xcomfort_bridge
    pip install paho-mqtt
    python -m unittest discover -s tests -v
