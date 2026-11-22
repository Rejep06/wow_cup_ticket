# Playwright version must match requirements.txt (1.57.0).
FROM mcr.microsoft.com/playwright/python:v1.57.0-noble

ENV DEBIAN_FRONTEND=noninteractive \
    DISPLAY=:99 \
    HOME=/home/runner \
    XAUTHORITY=/home/runner/.Xauthority \
    PYTHONUNBUFFERED=1 \
    VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:$PATH"

RUN apt-get update && apt-get install -y --no-install-recommends \
    python3-venv xvfb xauth x11-utils x11vnc fluxbox xterm novnc websockify \
    openssl fonts-dejavu-core fonts-liberation ca-certificates procps \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --uid 10001 --create-home --shell /bin/bash runner && \
    mkdir -p /app /home/runner/.vnc && \
    chown -R runner:runner /app /home/runner

WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN python3 -m venv /opt/venv && \
    /opt/venv/bin/python -m pip install --no-cache-dir -r /app/requirements.txt
COPY --chown=runner:runner . /app/
RUN chmod +x /app/server/entrypoint.sh /app/server/console.sh
USER runner
EXPOSE 6080
ENTRYPOINT ["/app/server/entrypoint.sh"]
