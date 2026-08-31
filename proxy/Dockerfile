FROM debian:13-slim

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
        tinyproxy \
        wget \
    && rm -rf /var/lib/apt/lists/*

COPY tinyproxy.conf /etc/tinyproxy/tinyproxy.conf
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod 0755 /usr/local/bin/entrypoint.sh

EXPOSE 3128

USER nobody

HEALTHCHECK --interval=30s --timeout=5s --start-period=2s \
    CMD http_proxy=http://127.0.0.1:3128 wget -q -O /dev/null http://tinyproxy.stats/ || exit 1

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
