#!/bin/bash
echo "$DOCKER_PASSWORD" | docker login -u "$DOCKER_USERNAME" --password-stdin
cd docker && docker build -t opennode/waldur-mastermind .
docker push "opennode/waldur-mastermind:latest"
docker images
