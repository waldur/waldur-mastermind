# -*- coding: utf-8; indent-tabs-mode: nil; tab-width: 2; -*-
#
# Build:
#
#   docker build --rm --tag=waldur-mastermind:$(grep version setup.py | cut -d'=' -f2 | tr -d "',") .
#
FROM centos:7

ADD . /mnt/src

RUN \
  yum --assumeyes install epel-release ;\
  yum --assumeyes update ;\
  yum --assumeyes install gcc python-devel python2-pip ;\
  yum clean all ;\
  pip install --editable /mnt/src ;\
