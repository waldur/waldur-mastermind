#!/usr/bin/env bash
set -x

[ -d pip-cache ] || mkdir pip-cache

docker --version
docker-compose --version

export COMPOSE_PROJECT_NAME=$BUILD_TAG
docker-compose up --build --detach --no-color
docker-compose run \
    -e LOCAL_USER_ID=`id -u $USER` \
    -e NUM_PROCESSES=5 \
    api waldur-test
result=$?
docker-compose down &> /dev/null || true &> /dev/null
exit $result
