FROM debian:bookworm-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends iperf3 uftp iputils-ping iproute2 procps coreutils tcpdump ca-certificates python3 python3-venv \
 && rm -rf /var/lib/apt/lists/* \
 && python3 -m venv /opt/cfdp \
 && /opt/cfdp/bin/pip install --no-cache-dir cfdp-py==0.7.0 \
 && mkdir -p /data
# CFDP sender/receiver (build context is the repository root, see docker/compose.yml)
COPY apps/cfdp/cfdp_entity.py /usr/local/bin/cfdp-entity
RUN chmod +x /usr/local/bin/cfdp-entity && sed -i '1s|.*|#!/opt/cfdp/bin/python|' /usr/local/bin/cfdp-entity
WORKDIR /data
CMD ["sleep", "infinity"]
