FROM centos:7
MAINTAINER Renato Pozzi <renato.pozzi.dev@gmail.com> 

RUN yum install -y git gcc liblzma-dev libffi-devel openssl-devel postgresql-devel libjpeg-devel zlib-devel python-devel xmlsec1 openldap-devel python-setuptools-devel

RUN git clone https://github.com/opennode/waldur-mastermind.git

WORKDIR waldur-mastermind

RUN easy_install pip

RUN pip install virtualenv

RUN virtualenv venv

RUN yum install -y xz-devel

RUN venv/bin/pip install --upgrade setuptools

RUN venv/bin/pip install --requirement docker-test/api/requirements.txt

RUN venv/bin/pip install --editable .

RUN venv/bin/pip install psycopg2-binary

# RUN cp src/waldur_core/server/settings.py.example src/waldur_core/server/settings.py

COPY settings.py.example src/waldur_core/server/settings.py

# RUN venv/bin/waldur migrate --noinput

RUN venv/bin/waldur collectstatic --noinput

COPY entrypoint.sh /entrypoint.sh

EXPOSE 8000

ENTRYPOINT ["/entrypoint.sh"]
# ENTRYPOINT ["/waldur-mastermind/venv/bin/waldur", "migrate", "--noinput", "/waldur-mastermind/venv/bin/waldur", "runserver"]
