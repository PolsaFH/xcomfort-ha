FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1

WORKDIR /app

RUN pip install --no-cache-dir paho-mqtt

COPY *.py seed_cache.json /app/
COPY web /app/web
COPY run.sh /run.sh
RUN chmod a+x /run.sh

CMD [ "/run.sh" ]
