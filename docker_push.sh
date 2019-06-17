#!/bin/bash
if [ -z "$TRAVIS_TAG" ]
then
  version='latest'
else
  # Strip prefix from tag name so that v3.7.5 becomes 3.7.5
  version=${TRAVIS_TAG#v}
fi

echo "$DOCKER_PASSWORD" | docker login -u "$DOCKER_USERNAME" --password-stdin
docker build -t opennode/waldur-mastermind .
docker push "opennode/waldur-mastermind:$version"
docker images
