FROM library/postgres:9.6

COPY init.sql /docker-entrypoint-initdb.d/init.sql
RUN chmod a+r /docker-entrypoint-initdb.d/init.sql

COPY update-config.sh /docker-entrypoint-initdb.d/_update-config.sh
RUN chmod a+x /docker-entrypoint-initdb.d/_update-config.sh
