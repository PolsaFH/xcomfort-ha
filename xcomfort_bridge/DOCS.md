# xComfort MQTT Bridge

Local control of Eaton / Sensio xComfort through the Home Manager. No cloud.

## Getting started

1. Install and start the **Mosquitto broker** app, and add the **MQTT** integration.
2. Create a separate Home Assistant user for the app, e.g. `xcomfort`.
3. On the **Configuration** tab: fill in **Home Manager IP**, **MQTT username** and **MQTT password**.
4. On the **Info** tab: turn on **Start on boot**, **Watchdog** and **Show in sidebar**, and press **Start**.
5. Open **xComfort** in the sidebar and follow the guide: **Search**, choose your devices, give them names and rooms.

## USB stick (optional)

Plug the xComfort stick **CKOZ-00/13** into the Home Assistant machine. Open **USB stick** on the web page and
press **Find dimmers**. The lights are then controlled directly over radio, with the Home Manager as fallback.

## Help

Full documentation (English and Norwegian), troubleshooting and examples:
https://github.com/PolsaFH/xcomfort-ha

Found a bug? Open an issue on GitHub, ideally with the log from the web page (Log → Download).
