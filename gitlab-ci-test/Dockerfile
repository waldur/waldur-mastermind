# Use to avoid pull rate limit for Docker Hub images
ARG DOCKER_REGISTRY=docker.io/
FROM ${DOCKER_REGISTRY}library/python:3.8

# Install necessary packages
RUN apt-get update    &&\
    apt-get install -y  \
    libldap2-dev        \
    libsasl2-dev        \
    ldap-utils          \
    xmlsec1             \
    lcov                \
    python3-dev

RUN python3 -m pip install --upgrade pip
RUN pip3 install poetry==1.1.6
RUN poetry config virtualenvs.create false

# Copy poetry files
COPY pyproject.toml /tmp/mnt/pyproject.toml
COPY poetry.lock /tmp/mnt/poetry.lock

# Install dependencies
# NB: `poetry update` command uses --dry-run to update commit sha in poetry.lock
RUN cd /tmp/mnt && \
    poetry update --dry-run ansible-waldur-module && \
    poetry install -vvv

CMD ["/bin/sh"]
