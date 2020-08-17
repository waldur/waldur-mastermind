#!/bin/bash
set -eo pipefail

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

  echo "INFO: Setting [uwsgi] socket = $UWSGI_SOCKET"
  crudini --set /etc/waldur/uwsgi.ini uwsgi socket $UWSGI_SOCKET

  echo "INFO: Disabling log file for UWSGI"
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
exec /tini -- "$@"
