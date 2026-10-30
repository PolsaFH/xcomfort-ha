FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends libusb-1.0-0 \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir paho-mqtt pyusb

COPY *.py seed_cache.json /app/
COPY web /app/web
COPY run.sh /run.sh
RUN chmod a+x /run.sh

CMD [ "/run.sh" ]
