#!/usr/bin/env bash
set -e
# Alle innstillinger leses direkte fra /data/options.json av broen selv.
echo "Starter xComfort MQTT-bro ..."
exec python3 -u /app/xcomfort_mqtt_bridge.py
