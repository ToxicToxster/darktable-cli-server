FROM python:3.12-slim

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        darktable \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd -r dtuser && useradd -r -g dtuser -m -s /bin/false dtuser

WORKDIR /srv/darktable-cli-server

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

RUN mkdir -p /tmp/darktable-work && chown dtuser:dtuser /tmp/darktable-work

USER dtuser

EXPOSE 8000

CMD ["python", "-m", "app"]
