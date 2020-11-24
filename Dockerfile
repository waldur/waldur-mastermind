FROM python:3.8

# Add tini
ENV TINI_VERSION=v0.19.0
RUN curl -L https://github.com/krallin/tini/releases/download/${TINI_VERSION}/tini -o /tini && \
    chmod +x /tini

# Add gosu
ENV GOSU_VERSION=1.12
RUN curl -L https://github.com/tianon/gosu/releases/download/$GOSU_VERSION/gosu-amd64 -o /usr/local/bin/gosu && \
    chmod +x /usr/local/bin/gosu

# Install necessary packages
RUN apt-get update       && \
    apt-get install -y      \
    git                     \
    libldap2-dev            \
    libsasl2-dev            \
    ldap-utils              \
    xmlsec1                 \
    lcov                    \
    python3-dev             \
    gettext                 \
    logrotate               \
    openssl                 \
    libffi-dev              \
    libjpeg-dev             \
    libxml2-dev             \
    libxslt-dev             \
    libyaml-dev             \
    uwsgi-src               \
    xfonts-75dpi            \
    fonts-liberation

RUN wget https://github.com/wkhtmltopdf/packaging/releases/download/0.12.6-1/wkhtmltox_0.12.6-1.buster_amd64.deb && \
    dpkg -i wkhtmltox_0.12.6-1.buster_amd64.deb                                                                  && \
    rm wkhtmltox_0.12.6-1.buster_amd64.deb

# Create python3.8 uwsgi plugin
RUN PYTHON=python3.8 uwsgi --build-plugin "/usr/src/uwsgi/plugins/python python38" && \
    mv python38_plugin.so /usr/lib/uwsgi/plugins/ && \
    apt remove -y uwsgi-src

RUN mkdir -p /usr/src/waldur

COPY . /usr/src/waldur/

COPY docker/rootfs /

# Delete all test directories
RUN cd /usr/src/waldur && find . -name "tests" -exec rm -r {} + && bash docker_build.sh

ENTRYPOINT ["/app-entrypoint.sh"]
CMD ["/bin/bash"]
