#!/bin/bash
set -e

if [ -z "$1" ]
then
  version='latest'
else
  # Strip prefix from tag name so that v3.7.5 becomes 3.7.5
  version=${1#v}
fi

echo "$WALDUR_DOCKER_HUB_PASSWORD" | docker login -u "$WALDUR_DOCKER_HUB_USER" --password-stdin
sed -i "s/version = \"0.0.0\"/version = \"$version\"/" pyproject.toml

docker build -t opennode/waldur-mastermind:$version .
docker push "opennode/waldur-mastermind:$version"
docker images
