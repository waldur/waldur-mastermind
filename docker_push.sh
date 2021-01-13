#!/bin/bash
set -e

if [ -z "$1" ]
then
  image_version='latest'
  package_version="0.0.0"
else
  # Strip prefix from tag name so that v3.7.5 becomes 3.7.5
  image_version=${1#v}
  package_version=$image_version
fi

echo "$WALDUR_DOCKER_HUB_PASSWORD" | docker login -u "$WALDUR_DOCKER_HUB_USER" --password-stdin
sed -i "s/version = \"0.0.0\"/version = \"$package_version\"/" pyproject.toml

if [ $CI_COMMIT_SHA ]
then
  echo "[+] Adding CI_COMMIT_SHA to docker/rootfs/COMMIT_SHA file"
  echo $CI_COMMIT_SHA > docker/rootfs/COMMIT_SHA
  cat docker/rootfs/COMMIT_SHA
fi

if [ $CI_COMMIT_TAG ]
then
  echo "[+] Adding CI_COMMIT_TAG to docker/rootfs/COMMIT_TAG file"
  echo $CI_COMMIT_TAG > docker/rootfs/COMMIT_TAG
  cat docker/rootfs/COMMIT_TAG
fi

docker build -t opennode/waldur-mastermind:$image_version .
docker push "opennode/waldur-mastermind:$image_version"
docker images
