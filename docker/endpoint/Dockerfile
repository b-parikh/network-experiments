FROM debian:bookworm-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends iperf3 uftp iputils-ping iproute2 procps coreutils tcpdump ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && mkdir -p /data
WORKDIR /data
CMD ["sleep", "infinity"]
