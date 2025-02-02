#!/bin/bash
set -eo pipefail

# uwsgi
: ${UWSGI_SOCKET:=":8000"}

echo "INFO: Welcome to Waldur Mastermind!"

# Only handle docker.sock in non-K8s environments
if [ -z "$KUBERNETES_SERVICE_HOST" ] && [ -e /var/run/docker.sock ]; then
    echo "INFO: Docker socket found."
    SOCKET_PERMS=$(stat -c '%a:%u:%g' /var/run/docker.sock)
    if ! [ -r /var/run/docker.sock ] || ! [ -w /var/run/docker.sock ]; then
        echo "Error: Container user $(id -u) must have read/write access to /var/run/docker.sock"
        echo "Current socket permissions: $SOCKET_PERMS (mode:uid:gid)"
        echo "Please ensure the container user is part of the docker group on the host system"
        exit 1
    fi
fi

if [[ -f "/etc/waldur/id_rsa" ]]; then
    WALDUR_DIR="/var/lib/waldur"
    TARGET_FILE="$WALDUR_DIR/id_rsa"

    if ! [ -w "$WALDUR_DIR" ]; then
        DIR_PERMS=$(stat -c '%a %u:%g' "$WALDUR_DIR")
        echo "Error: Cannot write to $WALDUR_DIR"
        echo "Current directory permissions: $DIR_PERMS (mode:uid:gid)"
        echo "Container user: $(id)"
        echo "Required: write permission for user $(id -u)"
        exit 1
    fi

    cp -vf "/etc/waldur/id_rsa" "$TARGET_FILE" || {
        SRC_PERMS=$(stat -c '%a %u:%g' "/etc/waldur/id_rsa")
        echo "Error: Failed to copy SSH key"
        echo "Source permissions: $SRC_PERMS (mode:uid:gid)"
        echo "Container user: $(id)"
        echo "Required: read permission for user $(id -u)"
        exit 1
    }
fi

if [[ -f "/etc/waldur/saml2/sp.pem" ]]; then
    cp -vf "/etc/waldur/saml2/sp.pem" "/var/lib/waldur/sp.pem" || {
        SRC_PERMS=$(stat -c '%a %u:%g' "/etc/waldur/saml2/sp.pem")
        echo "Error: Failed to copy SAML key"
        echo "Source permissions: $SRC_PERMS (mode:uid:gid)"
        echo "Container user: $(id)"
        echo "Required: read permission for user $(id -u)"
        exit 1
    }
fi

echo "INFO: Spawning $@"
exec "$@"
