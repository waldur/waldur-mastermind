# Use to avoid pull rate limit for Docker Hub images
ARG DOCKER_REGISTRY=docker.io/

FROM ${DOCKER_REGISTRY}python:3.11-alpine

ENV LANG=C.UTF-8

# Install necessary system packages.
RUN apk update && \
    apk add --no-cache \
    git\>=2.45 \
    # bash is used in multiple scripts in docker/rootfs.
    bash\>=5.2 \
    # Commands for managing user accounts and authentication. "useradd" and "groupadd" are used in "app-entrypoint.sh".
    shadow\>=4.15 \
    # file provides libmagic package. "import magic" in files like "storage.py" or "utils.py".
    file\>=5.45 \
    # The ldap-related package used with django-auth-ldap. Openldap-dev is necessary
    openldap-dev\>=2.6 \
    openssl\>=3.3 \
    libffi-dev\>=3.4 \
    libjpeg-turbo-dev\>=3.0 \
    libxml2-dev\>=2.12 \
    libxslt-dev\>=1.1 \
    # xmlsec is used in django saml2.
    xmlsec\>=1.3 \
    build-base\>=0.5 \
    jpeg-dev\>=9 \
    zlib-dev\>=1.3 \
    # Needed for old style slurm support which requires SSH command.
    openssh\>=9.7 \
    # Needed for psutil
    gcc\>=14.2 \
    python3-dev\>=3.12 \
    musl-dev\>=1.2 \
    linux-headers\>=6.6 \
    # GNU coreutils to replace BusyBox date command to generate date in correct format in scripts
    coreutils\>=9.4

# Set up locales
RUN echo "en_US.UTF-8 UTF-8" >> /etc/locale.gen

# Create local group and user
RUN /usr/sbin/groupadd -g 1001 waldur && useradd --home /var/lib/waldur --shell /bin/sh --system --uid 1001 --gid 1001 waldur

# Create directories and set permissions for OpenShift compatibility
RUN mkdir -p /usr/src/waldur /var/lib/waldur /run/waldur/celery /run/waldur/celerybeat && \
    chown -R waldur /usr/src/waldur /var/lib/waldur /run/waldur/celery /run/waldur/celerybeat && \
    chmod -R g+rwX /usr/src/waldur /var/lib/waldur /run/waldur/celery /run/waldur/celerybeat && \
    chmod -R 775 /usr/src/waldur /var/lib/waldur /run/waldur/celery /run/waldur/celerybeat && \
    chmod -R g+s /usr/src/waldur /var/lib/waldur /run/waldur/celery /run/waldur/celerybeat

COPY . /usr/src/waldur/

COPY docker/rootfs /

# Delete all test directories
RUN cd /usr/src/waldur && find . -name "tests" -exec rm -r {} + && bash docker_build.sh

# Delete .git directories
RUN find -f /usr/src/waldur/ -name ".git" -type d -exec rm -rf {} + || true

# Delete build-base package
RUN apk del build-base

# Set permissions again after copying files
RUN chown -R waldur /usr/src/waldur /var/lib/waldur /run/waldur/celery && \
    chmod -R g+rwX /usr/src/waldur /var/lib/waldur /run/waldur/celery

USER waldur

ENTRYPOINT ["/app-entrypoint.sh"]
CMD ["/bin/bash"]
