FROM 	centos:centos7

LABEL   summary="Waldur Mastermind REST API Image" \
        vendor="OpenNode" \
        license="MIT" \
        maintainer="Victor Mireyev <victor@opennodecloud.com>" \
        description="Waldur Cloud Brokerage Platform" \
        url="https://waldur.com"

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
RUN yum --assumeyes install \
    centos-release-openstack-pike \
    epel-release \
    https://download.postgresql.org/pub/repos/yum/9.6/redhat/rhel-7-x86_64/pgdg-centos96-9.6-3.noarch.rpm \
RUN yum --assumeyes update && yum clean all
RUN yum --assumeyes install --setopt=tsflags=nodocs \
  gcc \
  libffi-devel \
  libjpeg-devel \
  libxml2-devel \
  libxslt-devel \
  libyaml-devel \
  openldap-devel \
  openssl-devel \
  postgresql-devel \
  python-devel \
  python-pip \
  xmlsec1 \
  xmlsec1-openssl \
  zlib-devel \
  crudini \
  jq \
  python2-httpie \
  logrotate \
  mailcap \
  openssl \
  uwsgi-plugin-python2 \
  gettext

RUN mkdir -p /usr/src/waldur

COPY . /usr/src/waldur/
RUN cd /usr/src/waldur && bash docker_build.sh

COPY docker/rootfs /

ENTRYPOINT ["/app-entrypoint.sh"]
CMD ["/bin/bash"]
