#!/bin/bash
set -eo pipefail

echo "INFO: Welcome to Waldur Mastermind!"

# Only handle docker.sock in non-K8s environments
if [ -z "$KUBERNETES_SERVICE_HOST" ] && [ -e /var/run/docker.sock ]; then
    echo "INFO: Docker socket found."
    SOCKET_PERMS=$(stat -c '%a:%u:%g' /var/run/docker.sock)
    USER_UID=$(id -u)
    USER_GID=$(id -g)
    USER_GROUPS=$(id -G | tr ' ' ',')

    if ! [ -r /var/run/docker.sock ] || ! [ -w /var/run/docker.sock ]; then
        echo "Warning: Container user $USER_UID must have read/write access to /var/run/docker.sock"
        echo "Current socket permissions: $SOCKET_PERMS (mode:uid:gid)"
        echo "Container user details:"
        echo "  - UID: $USER_UID"
        echo "  - Primary GID: $USER_GID"
        echo "  - All group IDs: $USER_GROUPS"
        echo "Please ensure the container user is part of the docker group on the host system for custom scripts to run"
    fi
fi


if [[ -f "/etc/waldur/saml2/sp.pem" ]]; then
    cp -vf "/etc/waldur/saml2/sp.pem" "/var/lib/waldur/sp.pem" || {
        SRC_PERMS=$(stat -c '%a %u:%g' "/etc/waldur/saml2/sp.pem")
        echo "Warning: Failed to copy SAML key"
        echo "Source permissions: $SRC_PERMS (mode:uid:gid)"
        echo "Container user: $(id)"
        echo "Container user groups: $(id -G)"
        echo "Primary group: $(id -g)"
        echo "Required: read permission for user $(id -u) or group $(id -g)"
    }
fi

echo "INFO: Spawning $@"
exec "$@"
