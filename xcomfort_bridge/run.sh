#!/usr/bin/env bash
set -e
# All settings are read from /data/options.json by the bridge itself.
echo "Starting xComfort MQTT Bridge ..."
exec python3 -u /app/xcomfort_mqtt_bridge.py
