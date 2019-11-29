#!/bin/bash
set -eo pipefail

# local variables
DEFAULT_PWD="waldur"
DEFAULT_PWD_COMMENT="******"

# required variables
: ${GLOBAL_SECRET_KEY:=}

# optional variables
# global
: ${GLOBAL_DEFAULT_FROM_EMAIL:=demo@waldur.com}
: ${GLOBAL_OWNER_CAN_MANAGE_CUSTOMER:=true}
# system logs
: ${LOGGING_ADMIN_EMAIL:=}
: ${LOGGING_LOG_LEVEL:=INFO}
# user logs
: ${EVENTS_LOGSERVER_HOST:=waldur-logs}
: ${EVENTS_LOGSERVER_PORT:=5959}
: ${EVENTS_LOG_LEVEL:=INFO}
# database
: ${POSTGRESQL_HOST:=waldur-db}
: ${POSTGRESQL_PORT:=5432}
: ${POSTGRESQL_NAME:=waldur}
: ${POSTGRESQL_USER:=waldur}
: ${POSTGRESQL_PASSWORD:=$DEFAULT_PWD}
# queue
: ${REDIS_HOST:=waldur-queue}
: ${REDIS_PORT:=6379}
: ${REDIS_PASSWORD:=$DEFAULT_PWD}
# uwsgi
: ${UWSGI_SOCKET:=":8000"}
# user / group ids
: ${WALDUR_UID:=984}
: ${WALDUR_GID:=984}


echo "INFO: Welcome to Waldur Mastermind!"

/usr/bin/getent group waldur 2>&1 > /dev/null || /usr/sbin/groupadd -g $WALDUR_GID waldur

if ! id waldur 2> /dev/null > /dev/null; then
  # Create user and group if it does not exist yet
  echo "INFO: Creating user waldur ${WALDUR_UID}:${WALDUR_GID} "
  useradd --home /var/lib/waldur --shell /bin/sh --system --uid $WALDUR_UID --gid $WALDUR_GID waldur
fi

if [[ ! -d "/etc/waldur" ]] ; then
  echo "INFO: Creating new /etc/waldur folder structure"
  # Copy configuration files
  mkdir -p /etc/waldur/
  cp /etc/waldur-templates/celery.conf /etc/waldur/celery.conf
  cp /etc/waldur-templates/waldur/core.ini /etc/waldur/core.ini
  cp /etc/waldur-templates/uwsgi.ini /etc/waldur/uwsgi.ini

  # Copy default SAML2 configuration
  mkdir -p /etc/waldur/saml2/
  cp /etc/waldur-templates/saml2.conf.py.example /etc/waldur/saml2/

  echo "INFO: Processing required ENV variables..."
  if [ -z "$GLOBAL_SECRET_KEY" ]; then

    echo "ERROR: Environment variable GLOBAL_SECRET_KEY is not defined! Aborting."
    echo "NOTE: "
  cat << EOF

  You can add docker run ENV variable with this random generated key like this:
    -e GLOBAL_SECRET_KEY='$( head -c32 /dev/urandom | base64 )'

  Alternatively you can generate secret_key by running the following command:
    echo \$( head -c32 /dev/urandom | base64 )

  WARNING: Changing secret_key with existing database is not supported!
EOF

    exit 1

  fi
  echo "INFO: Setting [global] secret_key"
  crudini --set /etc/waldur/core.ini global secret_key $GLOBAL_SECRET_KEY

  echo "INFO: Processing optional ENV variables..."
  echo "INFO: Setting [global] default_from_email = $GLOBAL_DEFAULT_FROM_EMAIL"
  crudini --set /etc/waldur/core.ini global default_from_email $GLOBAL_DEFAULT_FROM_EMAIL
  echo "INFO: Setting [global] owner_can_manage_customer = $GLOBAL_OWNER_CAN_MANAGE_CUSTOMER"
  crudini --set /etc/waldur/core.ini global owner_can_manage_customer $GLOBAL_OWNER_CAN_MANAGE_CUSTOMER

  if [ -n "$LOGGING_ADMIN_EMAIL" ]; then

    echo "INFO: Setting [logging] admin_email = $LOGGING_ADMIN_EMAIL"
    crudini --set /etc/waldur/core.ini logging admin_email $LOGGING_ADMIN_EMAIL

  fi
  echo "INFO: Setting [logging] log_level = $LOGGING_LOG_LEVEL"
  crudini --set /etc/waldur/core.ini logging log_level $LOGGING_LOG_LEVEL

  echo "INFO: Setting [events] logserver_host = $EVENTS_LOGSERVER_HOST"
  crudini --set /etc/waldur/core.ini events logserver_host $EVENTS_LOGSERVER_HOST
  echo "INFO: Setting [events] logserver_port = $EVENTS_LOGSERVER_PORT"
  crudini --set /etc/waldur/core.ini events logserver_port $EVENTS_LOGSERVER_PORT
  echo "INFO: Setting [events] log_level = $EVENTS_LOG_LEVEL"
  crudini --set /etc/waldur/core.ini events log_level $EVENTS_LOG_LEVEL

  echo "INFO: Setting [postgresql] host = $POSTGRESQL_HOST"
  crudini --set /etc/waldur/core.ini postgresql host $POSTGRESQL_HOST
  echo "INFO: Setting [postgresql] port = $POSTGRESQL_PORT"
  crudini --set /etc/waldur/core.ini postgresql port $POSTGRESQL_PORT
  echo "INFO: Setting [postgresql] name = $POSTGRESQL_NAME"
  crudini --set /etc/waldur/core.ini postgresql name $POSTGRESQL_NAME
  echo "INFO: Setting [postgresql] user = $POSTGRESQL_USER"
  crudini --set /etc/waldur/core.ini postgresql user $POSTGRESQL_USER
  if [ "$POSTGRESQL_PASSWORD" == "$DEFAULT_PWD" ]; then

    DEFAULT_PWD_COMMENT="(default: $DEFAULT_PWD)"

  fi
  echo "INFO: Setting [postgresql] password $DEFAULT_PWD_COMMENT"
  crudini --set /etc/waldur/core.ini postgresql password $POSTGRESQL_PASSWORD

  echo "INFO: Setting [redis] host = $REDIS_HOST"
  crudini --set /etc/waldur/core.ini redis host $REDIS_HOST
  echo "INFO: Setting [redis] port = $REDIS_PORT"
  crudini --set /etc/waldur/core.ini redis port $REDIS_PORT
  echo "INFO: Setting [redis] password = $REDIS_PASSWORD"
  crudini --set /etc/waldur/core.ini redis password $REDIS_PASSWORD

  echo "INFO: Setting [uwsgi] socket = $UWSGI_SOCKET"
  crudini --set /etc/waldur/uwsgi.ini uwsgi socket $UWSGI_SOCKET

  echo "INFO: Disabling log files"
  crudini --set /etc/waldur/core.ini logging log_file
  crudini --set /etc/waldur/core.ini events log_file
  crudini --del /etc/waldur/uwsgi.ini uwsgi logto

fi

if [[ ! -d "/var/log/waldur" ]] ; then
  echo "INFO: Create logging directory"
  mkdir -p /var/log/waldur/
fi
chmod 750 /var/log/waldur/
chown -R waldur:waldur /var/log/waldur/

if [[ ! -d "/var/lib/waldur/media" ]] ; then
  echo "INFO: Create media assets directory"
  mkdir -p /var/lib/waldur/media/
fi
chmod 750 /var/lib/waldur/
chown -R waldur:waldur /var/lib/waldur/

echo "INFO: Spawning $@"
exec /usr/local/bin/tini -- "$@"
