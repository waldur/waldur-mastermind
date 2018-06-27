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
  rsync \
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
