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
RUN echo "deb-src http://deb.debian.org/debian buster main" >> /etc/apt/sources.list && \
    apt-get update       && \
    apt-get install -y      \
    git                     \
    libldap2-dev            \
    libsasl2-dev            \
    libgnutls28-dev         \
    ldap-utils              \
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
    xfonts-base             \
    fonts-liberation        \
    build-essential         \
    chrpath                 \
    debhelper               \
    help2man                \
    libgcrypt20-dev         \
    libnss3-dev             \
    gtk-doc-tools           \
    man2html-base           \
    xsltproc               && \
    mkdir xmlsec1               && \
    cd xmlsec1                  && \
    apt-get source xmlsec1      && \
    cd xmlsec1-1*               && \
    sed "s/--disable-crypto-dl/--disable-crypto-dl --enable-md5=no --enable-ripemd160=no/g" debian/rules >> debian/rules && \
    dpkg-buildpackage -us -uc   && \
    cd ..                       && \
    dpkg -i ./*.deb             && \
    apt-mark hold '*xmlsec1*'   && \
    rm -rf /xmlsec1/            && \
    apt-get remove -y              \
    build-essential                \
    chrpath                        \
    debhelper                      \
    help2man                       \
    gtk-doc-tools                  \
    man2html-base                  \
    xsltproc                    && \
    rm -rf /var/lib/apt/lists

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

ENTRYPOINT ["/app-entrypoint.sh"]
CMD ["/bin/bash"]
