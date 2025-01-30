#!/bin/bash
set -eo pipefail

# uwsgi
: ${UWSGI_SOCKET:=":8000"}

# user / group ids
: ${WALDUR_UID:=984}
: ${WALDUR_GID:=984}

# Function to validate numeric input
validate_numeric() {
    local value=$1
    local name=$2
    if ! [[ "$value" =~ ^[0-9]+$ ]]; then
        echo "Error: $name must be a number"
        exit 1
    fi
}

echo "INFO: Welcome to Waldur Mastermind!"

/usr/bin/getent group waldur 2>&1 > /dev/null || /usr/sbin/groupadd -g $WALDUR_GID waldur

if ! id waldur 2> /dev/null > /dev/null; then
  # Create user and group if it does not exist yet
  echo "INFO: Creating user waldur ${WALDUR_UID}:${WALDUR_GID} "
  useradd --home /var/lib/waldur --shell /bin/sh --system --uid $WALDUR_UID --gid $WALDUR_GID waldur
fi

# Get docker GID from socket and validate
if [ -e /var/run/docker.sock ]; then
    echo "INFO: Docker socket found, setting up docker group"
    HOST_DOCKER_GID=$(stat -c '%g' /var/run/docker.sock)
    validate_numeric "$HOST_DOCKER_GID" "Docker socket GID"

    if getent group $HOST_DOCKER_GID > /dev/null; then
        echo "INFO: Local group with GID=$HOST_DOCKER_GID already exists"
    else
        echo "INFO: Creating local docker group with GID=$HOST_DOCKER_GID"
        groupadd -g "$HOST_DOCKER_GID" docker || {
            echo "Failed to create docker group"
            exit 1
        }
    fi

    # Add waldur user to docker group
    usermod -aG $HOST_DOCKER_GID waldur
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

if [[ -f "/etc/waldur/saml2/sp.pem" ]] ; then
  # assure that signing private is owned by waldur and avoid modifying permissions of the original key
  cp -vf /etc/waldur/saml2/sp.pem /var/lib/waldur/sp.pem
  chown waldur:waldur /var/lib/waldur/sp.pem
fi

echo "INFO: Spawning $@"
exec tini -- "$@"
