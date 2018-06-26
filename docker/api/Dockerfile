FROM centos:7

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

COPY requirements.txt /tmp/
RUN pip install -r /tmp/requirements.txt

COPY waldur-test /usr/bin/waldur-test
