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

# set UTF-8 locale as default
ENV LANG='en_US.UTF-8' LANGUAGE='en_US:en' LC_ALL='en_US.UTF-8'
RUN localedef -i en_US -f UTF-8 en_US.UTF-8

RUN yum --assumeyes install epel-release
RUN yum --assumeyes update && yum clean all
RUN yum --assumeyes install \
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
  rsync \
  xmlsec1 \
  zlib-devel

# Copy unit test runner script
COPY waldur-test /usr/bin/waldur-test

# Copy script to execute command as another user
COPY entrypoint.sh /usr/local/bin/entrypoint.sh

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["/bin/bash"]
