# Build context is the repository root (see docker/compose.yml).
FROM debian:bookworm-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends iperf3 uftp iputils-ping iproute2 procps coreutils tcpdump \
      ca-certificates python3 python3-venv \
 && rm -rf /var/lib/apt/lists/* \
 && mkdir -p /data
# CFDP entity (cfdp-py) in its own virtualenv; run it as `cfdp send ...` / `cfdp recv ...`
RUN python3 -m venv /opt/cfdp && /opt/cfdp/bin/pip install --no-cache-dir cfdp-py==0.7.0
COPY apps/cfdp/cfdp_entity.py /opt/cfdp/cfdp_entity.py
RUN printf '#!/bin/sh\nexec /opt/cfdp/bin/python /opt/cfdp/cfdp_entity.py "$@"\n' > /usr/local/bin/cfdp \
 && chmod +x /usr/local/bin/cfdp
WORKDIR /data
CMD ["sleep", "infinity"]
