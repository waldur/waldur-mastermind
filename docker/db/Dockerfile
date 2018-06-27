FROM library/postgres:9.6
LABEL   summary="PostgreSQL Image for Waldur Mastermind Unit Test" \
        vendor="OpenNode" \
        license="MIT" \
        version="2.9" \
        release="7" \
        maintainer="Victor Mireyev <victor@opennodecloud.com>" \
        description="Waldur Cloud Brokerage Platform" \
        url="https://waldur.com"

# Copy SQL to create user role
COPY init.sql /docker-entrypoint-initdb.d/init.sql
RUN chmod a+r /docker-entrypoint-initdb.d/init.sql

# Copy database configuration script to enhance performance
COPY update-config.sh /docker-entrypoint-initdb.d/_update-config.sh
RUN chmod a+x /docker-entrypoint-initdb.d/_update-config.sh
