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

if [[ -f "/etc/waldur/id_rsa" ]] ; then
  # assure that ssh private is owned by waldur and avoid modifying permissions of the original key
  cp -vf /etc/waldur/id_rsa /var/lib/waldur/id_rsa
  chown waldur:waldur /var/lib/waldur/id_rsa
fi

if [[ -f "/etc/waldur/sp.pem" ]] ; then
  # assure that signing private is owned by waldur and avoid modifying permissions of the original key
  cp -vf /etc/waldur/sp.pem /var/lib/waldur/sp.pem
  chown waldur:waldur /var/lib/waldur/sp.pem
fi

echo "INFO: Spawning $@"
exec /tini -- "$@"
