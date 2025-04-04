# Use to avoid pull rate limit for Docker Hub images
ARG DOCKER_REGISTRY=docker.io/
FROM ${DOCKER_REGISTRY}library/python:3.11-bullseye

# Install necessary packages
RUN apt-get update    &&\
    apt-get install -y  \
    libldap2-dev        \
    libsasl2-dev        \
    ldap-utils          \
    xmlsec1             \
    git                 \
    lcov                \
    graphviz            \
    python3-dev

RUN python3 -m pip install --upgrade pip
RUN pip3 install poetry==2.1.2

# Copy poetry files
COPY pyproject.toml /tmp/mnt/pyproject.toml
COPY poetry.lock /tmp/mnt/poetry.lock

# Install dependencies
RUN poetry config virtualenvs.create false
RUN cd /tmp/mnt && poetry install -vvv --no-root

CMD ["/bin/sh"]
