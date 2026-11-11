# Use to avoid pull rate limit for Docker Hub images
ARG DOCKER_REGISTRY=docker.io/

# The image is built in two stages. The builder has the compiler, the C headers,
# git and uv, and produces the virtualenv in /opt/venv, the collected static
# files and the web shell assets. The runtime stage starts again from the bare
# base image and copies in only those results, so none of the build toolchain is
# in any layer of the image that ships.
#
# Both stages use the same base image, and the virtualenv and the source sit at
# the same paths in both: the venv's console scripts carry an absolute
# #!/opt/venv/bin/python shebang, the venv's python is a symlink to the base
# image's interpreter, and the project is installed editable, pointing at
# /usr/src/waldur/src.

FROM ${DOCKER_REGISTRY}python:3.13-slim AS builder

ENV LANG=C.UTF-8 \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_PYTHON_DOWNLOADS=never \
    UV_LINK_MODE=copy \
    PATH=/opt/venv/bin:$PATH

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    # python-ldap is the only locked dependency without a Linux wheel, so it is
    # compiled here against the OpenLDAP and SASL headers. libc6-dev is the C
    # library's headers, which gcc only recommends.
    gcc \
    libc6-dev \
    libldap2-dev \
    libsasl2-dev \
    # django-model-utils is installed from a pinned git commit.
    git \
    && rm -rf /var/lib/apt/lists/*

# uv runs only in this stage. Its own layer, before the source is copied in, so
# a source change does not reinstall it. Pinned (hadolint DL3013), so a new uv
# release cannot change how the image is built without a commit here.
RUN python3 -m pip install --no-cache-dir uv==0.13.7

WORKDIR /usr/src/waldur

COPY . /usr/src/waldur/

COPY docker/rootfs /

RUN bash docker_build.sh

# The runtime stage copies the source with --chown, and COPY keeps the mode
# bits, so the group-writable modes OpenShift's arbitrary UIDs need are set
# here rather than in a chmod layer that would duplicate the whole tree.
RUN chmod -R g+rwX /usr/src/waldur && \
    find /usr/src/waldur -type d -exec chmod g+s {} +


FROM ${DOCKER_REGISTRY}python:3.13-slim

ENV LANG=C.UTF-8

# The virtualenv comes first on PATH, so `waldur`, `gunicorn`, `celery` and
# `python3` (matrix-init's shebang) resolve to it.
ENV PATH=/opt/venv/bin:$PATH

# ghostty-web for `waldur web_shell`, downloaded by docker_build.sh.
ENV WALDUR_WEB_SHELL_ASSETS_DIR=/usr/share/waldur/web-shell

# Install the shared libraries and tools used at runtime, and nothing else. The
# compiler, headers and git stay in the builder stage.
#
# No font packages are installed. Invoice PDFs and table and chart exports
# (waldur_core/core/pdf/) bundle Source Sans 3, which covers Latin, Greek and
# Cyrillic, and fall back to host fonts for other scripts. With none here,
# names in Arabic, Hebrew, Thai, CJK, ... print as blank boxes. To render them, add
# fonts-noto-core (~30 MB; Arabic, Hebrew, Thai, Indic, ...) and fonts-noto-cjk
# (~60 MB more; Chinese, Japanese, Korean) below.
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    # bash is used in multiple scripts in docker/rootfs.
    bash \
    # pgrep for the API container's liveness probe, ps for debugging.
    procps \
    # The OpenLDAP and SASL libraries python-ldap (django-auth-ldap) was
    # compiled against in the builder.
    libldap2 \
    libsasl2-2 \
    # libmagic for python-magic: "import magic" in files like "storage.py" or "utils.py".
    libmagic1t64 \
    # pysaml2 (djangosaml2) signs and verifies SAML messages by calling the xmlsec1 binary.
    xmlsec1 \
    # jemalloc reduces allocator fragmentation in long-running prefork workers.
    libjemalloc2 \
    && rm -rf /var/lib/apt/lists/*

# Nothing installs Python packages at runtime, so the base image's pip goes.
RUN rm -rf /usr/local/bin/pip /usr/local/bin/pip3* \
    /usr/local/lib/python3*/site-packages/pip \
    /usr/local/lib/python3*/site-packages/pip-*.dist-info

# Use jemalloc instead of the default glibc allocator to curb memory
# fragmentation in long-running gunicorn/celery prefork workers. The bare soname
# resolves via the linker search path on both amd64 and arm64. Set LD_PRELOAD to
# an empty value to fall back to the system allocator. Set after libjemalloc2 is
# installed, so the apt-get above does not warn that it cannot be preloaded.
ENV LD_PRELOAD=libjemalloc.so.2

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

COPY --from=builder /opt/venv /opt/venv

COPY --from=builder /usr/share/waldur /usr/share/waldur

# Owner waldur and group root, as the chown -R that used to follow left it: an
# arbitrary UID always runs with GID 0, and the builder made the tree group-writable.
COPY --from=builder --chown=waldur:root /usr/src/waldur /usr/src/waldur

COPY docker/rootfs /

USER waldur

ENTRYPOINT ["/app-entrypoint.sh"]
CMD ["/bin/bash"]
