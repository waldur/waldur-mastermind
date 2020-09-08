#!/bin/bash
set -e

if [ -z "$1" ]
then
  version='latest'
else
  # Strip prefix from tag name so that v3.7.5 becomes 3.7.5
  version=${1#v}
fi

DOCKER_PASSWORD=${DOCKER_PASSWORD:-$WALDUR_DOCKER_HUB_PASSWORD}
DOCKER_USERNAME=${DOCKER_USERNAME:-$WALDUR_DOCKER_HUB_USER}

echo "$DOCKER_PASSWORD" | docker login -u "$DOCKER_USERNAME" --password-stdin
docker build -t opennode/waldur-mastermind:$version .
docker push "opennode/waldur-mastermind:$version"
docker images
