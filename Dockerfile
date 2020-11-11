FROM 	centos:centos7

LABEL   summary="Waldur Mastermind REST API Image" \
        vendor="OpenNode" \
        license="MIT" \
        maintainer="Victor Mireyev <victor@opennodecloud.com>" \
        description="Waldur Cloud Brokerage Platform" \
        url="https://waldur.com"

# CentOS 7 docker image does not define preferred locale.
# That's why ANSI_X3.4-1968 encoding is used by default.
ENV LANG='en_US.UTF-8' LANGUAGE='en_US:en' LC_ALL='en_US.UTF-8'
RUN localedef -i en_US -f UTF-8 en_US.UTF-8

# Add tini
ENV TINI_VERSION=v0.19.0
RUN curl https://github.com/krallin/tini/releases/download/${TINI_VERSION}/tini -o /tini
RUN chmod +x /tini

# Add gosu
ENV GOSU_VERSION=1.12
RUN curl https://github.com/tianon/gosu/releases/download/$GOSU_VERSION/gosu-amd64 -o /usr/local/bin/gosu
RUN chmod +x /usr/local/bin/gosu

# Install build dependencies for Waldur MasterMind from RPM repositories
RUN yum clean all && \
    yum --assumeyes install epel-release && \
    yum --assumeyes update && \
    yum --assumeyes install --setopt=tsflags=nodocs \
    gcc \
    git \
    libffi-devel \
    libjpeg-devel \
    libxml2-devel \
    libxslt-devel \
    libyaml-devel \
    openldap-devel \
    openssl-devel \
    python3-devel \
    xmlsec1 \
    xmlsec1-openssl \
    zlib-devel \
    logrotate \
    mailcap \
    openssl \
    uwsgi-plugin-python36 \
    gettext \
    which \
    https://github.com/wkhtmltopdf/packaging/releases/download/0.12.6-1/wkhtmltox-0.12.6-1.centos7.x86_64.rpm \
    freetype \
    liberation-serif-fonts \
    liberation-sans-fonts \
    liberation-mono-fonts \
    liberation-narrow-fonts && \
    yum clean all && \
    rm -fr /var/cache/*

RUN mkdir -p /usr/src/waldur

COPY . /usr/src/waldur/

COPY docker/rootfs /

# Delete all test directories
RUN cd /usr/src/waldur && find . -name "tests" -exec rm -r {} + && bash docker_build.sh

ENTRYPOINT ["/app-entrypoint.sh"]
CMD ["/bin/bash"]
