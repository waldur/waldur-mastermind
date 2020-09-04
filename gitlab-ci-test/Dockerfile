FROM python:3.6

# set UTF-8 locale as default
RUN apt-get update
RUN apt-get install -y locales
RUN sed -i 's/^# *\(en_US.UTF-8\)/\1/' /etc/locale.gen && locale-gen
ENV LANG=en_US.UTF-8 LANGUAGE=en_US:en LC_ALL=en_US.UTF-8
ENV PYTHONIOENCODING=UTF-8

RUN apt-get install -y  \
    libldap2-dev        \
    libsasl2-dev        \
    ldap-utils          \
    xmlsec1             \
    lcov                \
    python3-dev

RUN pip3 install poetry
RUN poetry config virtualenvs.create false

# Copy poetry files
COPY pyproject.toml /tmp/mnt/pyproject.toml
COPY poetry.lock /tmp/mnt/poetry.lock

# Install dependencies
RUN cd /tmp/mnt && poetry install

CMD ["/bin/sh"]
