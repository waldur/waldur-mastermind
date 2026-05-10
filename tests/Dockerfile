# Use to avoid pull rate limit for Docker Hub images
ARG DOCKER_REGISTRY=docker.io/
FROM ${DOCKER_REGISTRY}library/python:3.13-bookworm

# Install necessary packages
RUN apt-get update    &&\
    apt-get install -y  \
    libldap2-dev        \
    libsasl2-dev        \
    ldap-utils          \
    xmlsec1             \
    git                 \
    lcov                \
    python3-dev

RUN python3 -m pip install --upgrade pip
RUN pip3 install uv

# Copy project files
COPY pyproject.toml /tmp/mnt/pyproject.toml
COPY uv.lock /tmp/mnt/uv.lock

# Install dependencies using lock file for reproducible builds
RUN cd /tmp/mnt && uv sync --locked

CMD ["/bin/sh"]
