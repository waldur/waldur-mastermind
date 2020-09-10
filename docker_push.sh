#!/bin/bash
set -e

if [ -z "$1" ]
then
  image_version='latest'
  package_version="0.0.0-$CI_COMMIT_SHORT_SHA"
else
  # Strip prefix from tag name so that v3.7.5 becomes 3.7.5
  image_version=${1#v}
  package_version=$image_version
fi

echo "$WALDUR_DOCKER_HUB_PASSWORD" | docker login -u "$WALDUR_DOCKER_HUB_USER" --password-stdin
sed -i "s/version = \"0.0.0\"/version = \"$package_version\"/" pyproject.toml

docker build -t opennode/waldur-mastermind:$image_version .
docker push "opennode/waldur-mastermind:$image_version"
docker images
