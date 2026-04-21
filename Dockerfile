# Use to avoid pull rate limit for Docker Hub images
ARG DOCKER_REGISTRY=docker.io/

FROM ${DOCKER_REGISTRY}python:3.13-slim

ENV LANG=C.UTF-8

# Install necessary system packages.
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    git \
    # bash is used in multiple scripts in docker/rootfs.
    bash \
    # file provides libmagic package. "import magic" in files like "storage.py" or "utils.py".
    file \
    # The ldap-related package used with django-auth-ldap.
    libldap2-dev \
    libsasl2-dev \
    libssl-dev \
    libffi-dev \
    libjpeg-dev \
    libxml2-dev \
    libxslt1-dev \
    # xmlsec is used in django saml2.
    xmlsec1 \
    libxmlsec1-dev \
    # Build tools needed for compiling Python packages with C extensions.
    build-essential \
    zlib1g-dev \
    # Needed for old style slurm support which requires SSH command.
    openssh-client \
    # Needed for psutil and other C extensions
    gcc \
    python3-dev \
    procps \
    && rm -rf /var/lib/apt/lists/*

# Set up locales
RUN echo "en_US.UTF-8 UTF-8" >> /etc/locale.gen

# Create local group and user
RUN groupadd -g 1001 waldur && useradd --home /var/lib/waldur --shell /bin/sh --system --uid 1001 --gid 1001 waldur

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
RUN find /usr/src/waldur/ -name ".git" -type d -exec rm -rf {} + || true

# Delete build packages
RUN apt-get purge -y build-essential gcc && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*

# Set permissions again after copying files
RUN chown -R waldur /usr/src/waldur /var/lib/waldur /run/waldur/celery && \
    chmod -R g+rwX /usr/src/waldur /var/lib/waldur /run/waldur/celery

USER waldur

ENTRYPOINT ["/app-entrypoint.sh"]
CMD ["/bin/bash"]
