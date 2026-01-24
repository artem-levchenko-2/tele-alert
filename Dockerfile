FROM python:3.10-slim

WORKDIR /opt

# bash потрібен щоб запускати run.sh
RUN apt-get update \
 && apt-get install -y --no-install-recommends bash ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# залежності
RUN pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir telethon aiohttp

COPY forwarder.py /opt/forwarder.py
COPY run.sh /run.sh

RUN chmod +x /run.sh

CMD ["/bin/bash", "/run.sh"]
