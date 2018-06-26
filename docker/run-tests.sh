#!/usr/bin/env bash
set -x

export COMPOSE_PROJECT_NAME=$BUILD_TAG
docker-compose up --build --detach --no-color
docker-compose run api waldur-test
result=$?
docker-compose down --rmi all &> /dev/null || true &> /dev/null
exit $result
