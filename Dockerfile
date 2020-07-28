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
ENV TINI_VERSION v0.16.1
RUN cd /tmp && \
  gpg --keyserver hkp://p80.pool.sks-keyservers.net:80 --recv-keys 595E85A6B1B4779EA4DAAEC70B588DFF0527A9B7 && \
  gpg --fingerprint 595E85A6B1B4779EA4DAAEC70B588DFF0527A9B7 | grep -q "Key fingerprint = 6380 DC42 8747 F6C3 93FE  ACA5 9A84 159D 7001 A4E5" && \
  curl -sSL https://github.com/krallin/tini/releases/download/$TINI_VERSION/tini.asc -o tini.asc && \
  curl -sSL https://github.com/krallin/tini/releases/download/$TINI_VERSION/tini -o /usr/local/bin/tini && \
  gpg --verify tini.asc /usr/local/bin/tini && \
  chmod +x /usr/local/bin/tini && \
  rm tini.asc

# Add gosu
ENV GOSU_VERSION=1.10 \
    GOSU_GPG_KEY=B42F6819007F00F88E364FD4036A9C25BF357DD4
RUN cd /tmp && \
  gpg --keyserver hkp://p80.pool.sks-keyservers.net:80 --recv-keys $GOSU_GPG_KEY && \
  gpg --fingerprint $GOSU_GPG_KEY | grep -q "Key fingerprint = B42F 6819 007F 00F8 8E36  4FD4 036A 9C25 BF35 7DD4" && \
  curl -sSL https://github.com/tianon/gosu/releases/download/$GOSU_VERSION/gosu-amd64.asc -o gosu.asc && \
  curl -sSL https://github.com/tianon/gosu/releases/download/$GOSU_VERSION/gosu-amd64 -o /usr/local/bin/gosu && \
  gpg --verify gosu.asc /usr/local/bin/gosu && \
  chmod +x /usr/local/bin/gosu && \
  rm gosu.asc

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

# Delete all test directories
RUN cd /usr/src/waldur && find . -name "tests" -exec rm -r {} + && bash docker_build.sh

COPY docker/rootfs /

ENTRYPOINT ["/app-entrypoint.sh"]
CMD ["/bin/bash"]
