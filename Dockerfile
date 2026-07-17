FROM ubuntu:22.04

ARG GOST_VERSION=2.11.5

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# ── System dependencies ─────────────────────────────────────────
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        curl \
        gnupg \
        lsb-release \
        ca-certificates \
        wget \
        iproute2 \
        iptables \
        procps \
        dbus \
        python3 \
        python3-pip \
        && \
    rm -rf /var/lib/apt/lists/*

# ── Install Cloudflare WARP ─────────────────────────────────────
RUN curl -fsSL https://pkg.cloudflareclient.com/pubkey.gpg | \
    gpg --yes --dearmor --output /usr/share/keyrings/cloudflare-warp-archive-keyring.gpg && \
    echo "deb [arch=amd64 signed-by=/usr/share/keyrings/cloudflare-warp-archive-keyring.gpg] https://pkg.cloudflareclient.com/ $(lsb_release -cs) main" | \
    tee /etc/apt/sources.list.d/cloudflare-client.list && \
    apt-get update && \
    apt-get install -y --no-install-recommends cloudflare-warp && \
    rm -rf /var/lib/apt/lists/*

# ── Install GOST ────────────────────────────────────────────────
RUN wget -O /tmp/gost.gz "https://github.com/ginuerzh/gost/releases/download/v${GOST_VERSION}/gost-linux-amd64-${GOST_VERSION}.gz" && \
    gzip -d /tmp/gost.gz && \
    mv /tmp/gost /usr/local/bin/gost && \
    chmod +x /usr/local/bin/gost

# ── Python dependencies ─────────────────────────────────────────
COPY requirements.txt /tmp/requirements.txt
RUN pip3 install -r /tmp/requirements.txt && \
    rm -f /tmp/requirements.txt

# ── Application files ───────────────────────────────────────────
COPY backend/ /app/backend/
COPY entrypoint.sh /entrypoint.sh

RUN chmod +x /entrypoint.sh && \
    mkdir -p /data/licenses

EXPOSE 1080 8080 8000

ENTRYPOINT ["/entrypoint.sh"]
