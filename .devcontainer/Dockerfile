FROM mcr.microsoft.com/devcontainers/python:1-3.11-bullseye

ENV PYTHONUNBUFFERED 1

RUN apt-get update && \
    apt-get install -y \
        git \
        libldap2-dev \
        libsasl2-dev \
        ldap-utils \
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
COPY poetry.lock /tmp/mnt/poetry.lock

RUN python3 -m pip install --upgrade pip && \
    pip3 install poetry==1.8.3 && \
    poetry config virtualenvs.create false && \
    cd /tmp/mnt/ && poetry install
