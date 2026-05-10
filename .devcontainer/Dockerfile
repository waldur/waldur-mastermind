FROM mcr.microsoft.com/devcontainers/python:1-3.12-bullseye

ENV PYTHONUNBUFFERED=1

RUN apt-get update && \
    apt-get install -y \
        git \
        locales \
        gettext \
        openssl \
        libnss3 \
        libnspr4 \
        libffi-dev \
        libjpeg-dev \
        libxml2-dev \
        libxslt-dev \
        libyaml-dev \
        python3-dev

COPY pyproject.toml /tmp/mnt/pyproject.toml
COPY uv.lock /tmp/mnt/uv.lock

RUN python3 -m pip install --upgrade pip && \
    pip3 install uv && \
    cd /tmp/mnt/ && uv sync --locked
