# Disable weak crypto algorithms in xmldsig used by PySAML2
# See also: https://github.com/IdentityPython/pysaml2/issues/421#issuecomment-306133822
FROM buildpack-deps:buster as xmlsec1
WORKDIR /xmlsec1
RUN echo "deb-src http://deb.debian.org/debian buster main" >> /etc/apt/sources.list && \
    apt-get update              && \
    apt-get install -y --no-install-recommends build-essential && \
    apt-get build-dep -y xmlsec1 && \
    apt-get source xmlsec1      && \
    cd xmlsec1-1*               && \
    sed "s/--disable-crypto-dl/--disable-crypto-dl --enable-md5=no --enable-ripemd160=no/g" debian/rules >> debian/rules && \
    dpkg-buildpackage -us -uc && \
    cd .. && rm ./*-dbgsym*.deb ./*-dev*.deb ./*-doc*.deb

FROM python:3.8

# Install necessary packages
RUN apt-get update       && \
    apt-get install -y      \
    git                     \
    gosu                    \
    libldap2-dev            \
    libsasl2-dev            \
    ldap-utils              \
    lcov                    \
    locales                 \
    gettext                 \
    logrotate               \
    openssl                 \
    libnss3                 \
    libnspr4                \
    libffi-dev              \
    libjpeg-dev             \
    libxml2-dev             \
    libxslt-dev             \
    libyaml-dev             \
    tini                    \
    uwsgi-src               \
    xfonts-75dpi            \
    xfonts-base             \
    fonts-liberation     && \
    rm -rf /var/lib/apt/lists

RUN sed -i -e 's/# en_US.UTF-8 UTF-8/en_US.UTF-8 UTF-8/' /etc/locale.gen && \
    sed -i -e 's/# de_DE.UTF-8 UTF-8/de_DE.UTF-8 UTF-8/' /etc/locale.gen && \
    dpkg-reconfigure --frontend=noninteractive locales

# Install xmlsec1
WORKDIR /tmp/xmlsec1
COPY --from=xmlsec1 /xmlsec1/*.deb ./
RUN dpkg -i ./*.deb && rm ./*.deb

RUN curl -LO https://github.com/wkhtmltopdf/packaging/releases/download/0.12.6-1/wkhtmltox_0.12.6-1.buster_amd64.deb && \
    dpkg -i wkhtmltox_0.12.6-1.buster_amd64.deb                                                                      && \
    rm wkhtmltox_0.12.6-1.buster_amd64.deb

# Create python3.8 uwsgi plugin
RUN PYTHON=python3.8 uwsgi --build-plugin "/usr/src/uwsgi/plugins/python python38" && \
    mv python38_plugin.so /usr/lib/uwsgi/plugins/ && \
    apt-get remove -y uwsgi-src

RUN mkdir -p /usr/src/waldur

COPY . /usr/src/waldur/

COPY docker/rootfs /

# Delete all test directories
RUN cd /usr/src/waldur && find . -name "tests" -exec rm -r {} + && bash docker_build.sh

# Delete .git directories
RUN rm -rf /usr/local/src/ansible-waldur-module/.git \
           /usr/local/src/django-dbtemplates/.git

# Delete all development libraries
RUN apt-get purge -y lib*-dev

ENTRYPOINT ["/app-entrypoint.sh"]
CMD ["/bin/bash"]
