# Use to avoid pull rate limit for Docker Hub images
ARG DOCKER_REGISTRY=docker.io/

FROM ${DOCKER_REGISTRY}python:3.11-alpine

ENV LANG C.UTF-8

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
    # nginx is used as our web server.
    nginx\>=1.26 \
    # xmlsec is used in django saml2.
    xmlsec\>=1.3 \
    build-base\>=0.5 \
    jpeg-dev\>=9 \
    zlib-dev\>=1.3 \
    # Needed for old style slurm support which requires SSH command.
    openssh\>=9.7

# Set up locales
RUN echo "en_US.UTF-8 UTF-8" >> /etc/locale.gen

# Set up nginx directories with proper permissions
RUN mkdir -p /var/lib/nginx/tmp/client_body \
             /var/lib/nginx/tmp/proxy \
             /var/lib/nginx/tmp/fastcgi \
             /var/lib/nginx/tmp/uwsgi \
             /var/lib/nginx/tmp/scgi \
             /var/lib/nginx/logs && \
    chown -R 1001:0 /var/lib/nginx && \
    chmod -R g+rwX /var/lib/nginx && \
    chmod -R g+s /var/lib/nginx

RUN sed -i '/^user/s/^/#/' /etc/nginx/nginx.conf

# Create directories and set permissions for OpenShift compatibility
RUN mkdir -p /usr/src/waldur /var/lib/waldur && \
    chown -R 1001:0 /usr/src/waldur /var/lib/waldur && \
    chmod -R g+rwX /usr/src/waldur /var/lib/waldur && \
    chmod -R 775 /usr/src/waldur /var/lib/waldur && \
    chmod -R g+s /usr/src/waldur /var/lib/waldur

COPY . /usr/src/waldur/

COPY docker/rootfs /

# Delete all test directories
RUN cd /usr/src/waldur && find . -name "tests" -exec rm -r {} + && bash docker_build.sh

# Delete .git directories
RUN find /usr/local/src/ -name ".git" -type d -exec rm -rf {} +

# Delete build-base package
RUN apk del build-base

# Set permissions again after copying files
RUN chown -R 1001:0 /usr/src/waldur /var/lib/waldur && \
    chmod -R g+rwX /usr/src/waldur /var/lib/waldur

USER 1001:0

ENTRYPOINT ["/app-entrypoint.sh"]
CMD ["/bin/bash"]
