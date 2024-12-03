# Use to avoid pull rate limit for Docker Hub images
ARG DOCKER_REGISTRY=docker.io/

FROM ${DOCKER_REGISTRY}python:3.11-alpine

ENV LANG C.UTF-8

# Install necessary system packages.
RUN echo "@testing http://dl-cdn.alpinelinux.org/alpine/edge/testing" >> /etc/apk/repositories && \
    apk update && \
    apk add --no-cache \
    git~=2.45 \
    # bash is used in multiple scripts in docker/rootfs.
    bash~=5.2 \
    # Commands for managing user accounts and authentication. "useradd" and "groupadd" are used in "app-entrypoint.sh".
    shadow~=4.15 \
    # file provides libmagic package. "import magic" in files like "storage.py" or "utils.py".
    file~=5.45 \
    # The ldap-related package used with django-auth-ldap. Openldap-dev is necessary
    openldap-dev~=2.6 \
    openssl~=3.3 \
    libffi-dev~=3.4 \
    libjpeg-turbo-dev~=3.0 \
    libxml2-dev~=2.12 \
    libxslt-dev~=1.1 \
    # tini isused as container init in app-entrypoint.sh.
    tini~=0.19 \
    # nginx is used as our web server.
    nginx~=1.26 \
    # xmlsec is used in django saml2.
    xmlsec~=1.3 \
    build-base~=0.5 \
    jpeg-dev~=9 \
    zlib-dev~=1.3 \
    # gosu to give privileges to a non-root user. We use it in multiple scripts such as initdb.
    gosu@testing~=1.17

# Set up locales
RUN echo "en_US.UTF-8 UTF-8" >> /etc/locale.gen && \
    echo "de_DE.UTF-8 UTF-8" >> /etc/locale.gen

RUN mkdir -p /usr/src/waldur

COPY . /usr/src/waldur/

COPY docker/rootfs /

# Delete all test directories
RUN cd /usr/src/waldur && find . -name "tests" -exec rm -r {} + && bash docker_build.sh

# Delete .git directories
RUN rm -rf /usr/local/src/ansible-waldur-module/.git \
           /usr/local/src/django-dbtemplates/.git

# Delete build-base package
RUN apk del build-base

ENTRYPOINT ["/app-entrypoint.sh"]
CMD ["/bin/bash"]
