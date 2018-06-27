FROM centos:7
MAINTAINER Victor Mireyev <victor@opennodecloud.com>

LABEL   summary="Waldur Mastermind REST API Image for Unit Test" \
        vendor="OpenNode" \
        license="MIT" \
        version="2.9" \
        release="7" \
        maintainer="Victor Mireyev <victor@opennodecloud.com>" \
        description="Waldur Cloud Brokerage Platform" \
        url="https://waldur.com"

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
RUN yum --assumeyes install http://opennodecloud.com/centos/7/waldur-release.rpm
RUN yum --assumeyes install https://download.postgresql.org/pub/repos/yum/9.6/redhat/rhel-7-x86_64/pgdg-centos96-9.6-3.noarch.rpm
RUN yum --assumeyes install epel-release centos-release-openstack-pike
RUN yum-config-manager --disable centos-qemu-ev
RUN yum --assumeyes update && yum clean all
RUN yum --assumeyes install \
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
  zlib-devel

# Install Python dependencies for Waldur MasterMind from PyPI
COPY requirements.txt /tmp/
RUN pip install -r /tmp/requirements.txt

# Copy unit test runner script
COPY waldur-test /usr/bin/waldur-test

# Copy script to execute command as another user
COPY entrypoint.sh /usr/local/bin/entrypoint.sh

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["/bin/bash"]
