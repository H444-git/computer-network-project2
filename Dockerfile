FROM python:3.12-slim

WORKDIR /app

COPY common.py hub.py relay.py webconsole.py gateway.py demo_online.py ./
COPY web ./web

ENV PYTHONUNBUFFERED=1 \
    WEB_BIND=0.0.0.0 \
    WEB_PORT=8080 \
    WEB_TOKEN=parent123 \
    RELAY_BIND=127.0.0.1 \
    RELAY_HOST=127.0.0.1 \
    RELAY_PORT=9000 \
    DEVICE_ID=demo-gw \
    TARGET_DEVICE=demo-gw

EXPOSE 8080

CMD ["python", "-u", "demo_online.py"]
