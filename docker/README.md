Table of Contents
=================

   * [Waldur Mastermind Docker Image](#waldur-mastermind-docker-image)
      * [Image building instructions](#image-building-instructions)
      * [Docker-compose usage instructions](#docker-compose-usage-instructions)
      * [Manual image usage instructions](#manual-image-usage-instructions)
         * [Preparing environment and configuration](#preparing-environment-and-configuration)
         * [Initializing Mastermind backend](#initializing-mastermind-backend)
         * [Running Mastermind worker service (celery)](#running-mastermind-worker-service-celery)
         * [Running Mastermind beat service (celery-beat)](#running-mastermind-beat-service-celery-beat)
         * [Running Mastermind uwsgi service](#running-mastermind-uwsgi-service)
         * [Running Mastermind API frontend (nginx)](#running-mastermind-api-frontend-nginx)
         * [Checking Mastermind status](#checking-mastermind-status)
      * [Mastermind prerequisites](#mastermind-prerequisites)
         * [Docker host configuration](#docker-host-configuration)
            * [Docker overlay storage](#docker-overlay-storage)
            * [Creating app network](#creating-app-network)
         * [Running PostgreSQL](#running-postgresql)
         * [Running Redis](#running-redis)
         * [Running Elasticsearch](#running-elasticsearch)
         * [Running Logstash](#running-logstash)
            * [Create logstash pipeline configuration](#create-logstash-pipeline-configuration)
            * [Create logstash configuration](#create-logstash-configuration)
            * [Running Logstash container](#running-logstash-container)
         * [Running Postfix SMTP](#running-postfix-smtp)

# Waldur Mastermind Docker Image

## Image building instructions

```bash
# clone repo
mkdir -p ~/repos
cd ~/repos
git clone git@github.com:opennode/waldur-mastermind-docker.git

# build image
cd waldur-mastermind-docker
make build

# push image to docker hub
# NB! Make sure that you are authenticated via docker login 
# and that you have write access to hub.docker.com/opennode/waldur-mastermind repo!
make push
```

## Docker-compose usage instructions

Prerequisites:
* at least 8GB RAM on Docker Host to run all containers
* local hostname resolution in laptop /etc/hosts: waldur-mastermind-api -> your_docker_host

Prepare environment:
```bash
# clone repo
mkdir -p ~/repos
cd ~/repos
git clone git@github.com:opennode/waldur-mastermind-docker.git
cd ~/repos/waldur-mastermind-docker

# create rc file
echo $( head -c32 /dev/urandom | base64 ) > ~/waldur_secret.key
chmod 600 ~/waldur_secret.key
echo "export GLOBAL_SECRET_KEY=\"$( cat ~/waldur_secret.key )\"" > ~/waldurrc
echo "export POSTGRESQL_PASSWORD=\"waldur\"" >> ~/waldurrc

# load ENV variables
source ~/waldurrc

# create app network
docker network create waldur --driver bridge

# create and populate logstash pipeline configuration
VOLUME_NAME=waldur_logs_pipeline
docker volume create --name=$VOLUME_NAME
TARGET_DIR=$( docker volume inspect --format '{{ .Mountpoint }}' $VOLUME_NAME )
cp files/waldur-logs/logstash.conf $TARGET_DIR
chown root:root $TARGET_DIR/logstash.conf
chmod 644 $TARGET_DIR/logstash.conf 

# create and populate logstash settings
VOLUME_NAME=waldur_logs_settings
docker volume create --name=$VOLUME_NAME
TARGET_DIR=$( docker volume inspect --format '{{ .Mountpoint }}' $VOLUME_NAME )
cp files/waldur-logs/logstash.yml $TARGET_DIR
chown root:root $TARGET_DIR/logstash.yml
chmod 666 $TARGET_DIR/logstash.yml
cp files/waldur-logs/log4j2.properties $TARGET_DIR
chown root:root $TARGET_DIR/log4j2.properties
chmod 644 $TARGET_DIR/log4j2.properties

# create and populate mastermind-api nginx proxy configuration
VOLUME_NAME=waldur_mastermind_api
docker volume create --name=$VOLUME_NAME
TARGET_DIR="$( docker volume inspect --format '{{ .Mountpoint }}' $VOLUME_NAME )"
CONF_DIR="${TARGET_DIR}/nginx/conf"
mkdir -p $CONF_DIR
cp files/waldur-mastermind-api/nginx.conf $CONF_DIR
chown root:root $CONF_DIR/nginx.conf
chmod 644 $CONF_DIR/nginx.conf
chown -R 1001:root ${TARGET_DIR}/nginx 
```

Booting up:
```bash
# set sleep interval
INTERVAL=10
# launch DB
docker-compose up -d waldur-db
# launch Redis
echo never > /sys/kernel/mm/transparent_hugepage/enabled
docker-compose up -d waldur-queue
sleep $INTERVAL
# reconfigure Redis
docker-compose run -T --rm waldur-queue sed -i 's/^protected-mode yes/protected-mode no/g' /bitnami/redis/conf/redis.conf
docker-compose restart waldur-queue
# launch Elasticsearch
sysctl -w vm.max_map_count=262144
sysctl -w fs.file-max=65536
docker-compose up -d waldur-events
# init DB and admin user
docker-compose run -T --rm waldur-mastermind-worker initdb
docker-compose run -T --rm waldur-mastermind-worker createadmin
# launch Celery
docker-compose up -d waldur-mastermind-worker
sleep $INTERVAL
# launch Celery-beat
docker-compose up -d waldur-mastermind-beat
sleep $INTERVAL
# launch MasterMind uwsgi
docker-compose up -d waldur-mastermind-uwsgi
sleep $INTERVAL
# launch MasterMind REST API proxy
docker-compose up -d waldur-mastermind-api
sleep $INTERVAL
# launch HomePort
docker-compose up -d waldur-homeport
# verify
docker-compose ps
docker-compose run --rm waldur-mastermind-worker status
```

Tearing down and cleaning up (deleting ALL volumes):
```bash
docker-compose down -v
for VOLUME in $( docker volume ls | awk '/waldur_/ { print $2 }' ); do docker volume rm $VOLUME; done
docker network rm waldur
```

## Manual image usage instructions

Prerequisites:
* App network
* PostgreSQL database
* Redis kv store
* Elasticsearch 
* Logstash
* SMTP
* local hostname resolution in your laptop /etc/hosts: waldur-mastermind-api -> your_docker_host

### Preparing environment and configuration

```bash
# pull image from https://hub.docker.com/r/opennode/waldur-mastermind/
docker pull opennode/waldur-mastermind

# TODO: create docker volume for /var/lib/waldur/media persistance
docker volume create waldur_media
# verify
docker volume inspect waldur_media

# generating secret_key for Mastermind configuration
echo $( head -c32 /dev/urandom | base64 ) > waldur_secret.key
chmod 600 waldur_secret.key
# OR alternatively just run waldur-mastermind container to generate and output key
docker run --rm opennode/waldur-mastermind

# set REQUIRED ENV variables for Mastermind configuration
echo "GLOBAL_SECRET_KEY=\"$( cat waldur_secret.key )\"" > mastermindrc

# set OPTIONAL ENV variables for Mastermind configuration
# if you want to override any configuration defaults

# general configuration
echo "GLOBAL_DEFAULT_FROM_EMAIL=\"demo@waldur.com\"" >> mastermindrc
echo "GLOBAL_OWNER_CAN_MANAGE_CUSTOMER=\"true\"" >> mastermindrc

# system logging configuration
echo "LOGGING_ADMIN_EMAIL=\"admin@example.com\"" >> mastermindrc
echo "LOGGING_LOG_LEVEL=\"info\"" >> mastermindrc

# user logs configuration (logstash)
echo "EVENTS_LOGSERVER_HOST=\"waldur-logs\"" >> mastermindrc
echo "EVENTS_LOGSERVER_PORT=\"5959\"" >> mastermindrc
echo "EVENTS_LOG_LEVEL=\"info\"" >> mastermindrc

# database connection (postgresql)
echo "POSTGRESQL_HOST=\"waldur-db\"" >> mastermindrc
echo "POSTGRESQL_PORT=\"5432\"" >> mastermindrc
echo "POSTGRESQL_NAME=\"waldur\"" >> mastermindrc
echo "POSTGRESQL_USER=\"waldur\"" >> mastermindrc
echo "POSTGRESQL_PASSWORD=\"waldur\"" >> mastermindrc

# events configuration (elasticsearch)
echo "ELASTICSEARCH_HOST=\"waldur-events\"" >> mastermindrc
echo "ELASTICSEARCH_PORT=\"9200\"" >> mastermindrc
echo "ELASTICSEARCH_PROTOCOL=\"http\"" >> mastermindrc
echo "ELASTICSEARCH_USERNAME=\"elastic\"" >> mastermindrc
echo "ELASTICSEARCH_PASSWORD=\"elastic\"" >> mastermindrc
echo "ELASTICSEARCH_VERIFY_CERTS=\"true\"" >> mastermindrc

# queue configuration (redis)
echo "REDIS_HOST=\"waldur-queue\"" >> mastermindrc
echo "REDIS_PORT=\"6379\"" >> mastermindrc

# uwsgi configuration
echo "UWSGI_SOCKET=\":8000\"" >> mastermindrc

```

### Initializing Mastermind backend

```bash
# initialize Mastermind database
source mastermindrc && \
docker run --rm \
  --network waldur \
  -e GLOBAL_SECRET_KEY=$GLOBAL_SECRET_KEY \
  -e POSTGRESQL_PASSWORD=$POSTGRESQL_PASSWORD \
  opennode/waldur-mastermind initdb

# create Mastermind admin user
source mastermindrc && \
docker run --rm \
  --network waldur \
  -e GLOBAL_SECRET_KEY=$GLOBAL_SECRET_KEY \
  -e POSTGRESQL_PASSWORD=$POSTGRESQL_PASSWORD \
  opennode/waldur-mastermind createadmin [password]
```

### Running Mastermind worker service (celery)

```bash  
source mastermindrc && \
docker run -d --name waldur-mastermind-worker \
  --network waldur \
  -e GLOBAL_SECRET_KEY=$GLOBAL_SECRET_KEY \
  -e POSTGRESQL_PASSWORD=$POSTGRESQL_PASSWORD \
  opennode/waldur-mastermind worker

# verify
docker logs -f waldur-mastermind-worker
```

### Running Mastermind beat service (celery-beat)

```bash
source mastermindrc && \
docker run -d --name waldur-mastermind-beat \
  --network waldur \
  -e GLOBAL_SECRET_KEY=$GLOBAL_SECRET_KEY \
  -e POSTGRESQL_PASSWORD=$POSTGRESQL_PASSWORD \
  opennode/waldur-mastermind beat

# verify
docker logs -f waldur-mastermind-beat
```

### Running Mastermind uwsgi service

```bash
source mastermindrc && \
docker run -d --name waldur-mastermind-uwsgi \
  --network waldur \
  -e GLOBAL_SECRET_KEY=$GLOBAL_SECRET_KEY \
  -e POSTGRESQL_PASSWORD=$POSTGRESQL_PASSWORD \
  opennode/waldur-mastermind mastermind

# verify
docker logs -f waldur-mastermind-uwsgi
```

### Running Mastermind API frontend (nginx)

* https://github.com/bitnami/bitnami-docker-nginx/

```bash
docker pull bitnami/nginx:latest
cd ~/repos/waldur-mastermind

VOLUME_NAME=waldur_mastermind_api
docker volume create --name=$VOLUME_NAME
TARGET_DIR="$( docker volume inspect --format '{{ .Mountpoint }}' $VOLUME_NAME )"
CONF_DIR="${TARGET_DIR}/nginx/conf"
mkdir -p $CONF_DIR
cp files/waldur-mastermind-api/nginx.conf $CONF_DIR
chown root:root $CONF_DIR/nginx.conf
chmod 644 $CONF_DIR/nginx.conf
chown -R 1001:root ${TARGET_DIR}/nginx 

docker run -d --name waldur-mastermind-api \
    --network waldur \
    --mount source=waldur_mastermind_api,target=/bitnami \
    -p 8080:8080 \
    bitnami/nginx:latest

# verify
docker logs -f waldur-mastermind-api 
```

### Checking Mastermind status

```bash
source mastermindrc && \
docker run --rm \
  --network waldur \
  -e GLOBAL_SECRET_KEY=$GLOBAL_SECRET_KEY \
  -e POSTGRESQL_PASSWORD=$POSTGRESQL_PASSWORD \
  opennode/waldur-mastermind status
```

## Mastermind prerequisites

### Docker host configuration

* Image was tested with the following docker versions: 17.07.0-ce, 1.13.1 
* Overlay storage driver was used with XFS data volume
* NB! XFS needs to be formatted with ftype=1 option - or overlay storage driver will cause problems!
* App network needs to be created - which is used for Mastermind components inter-communication
* At least 8GB of RAM is required to run all containers

#### Docker overlay storage

```bash
# check XFS fstype
xfs_info / | awk '/ftype/ { print $6 }'

# list available block devices
lsblk

# create separate data volume as /dev/vdb and format with XFS ftype=1
mkfs.xfs -n ftype=1 /dev/vdb

# migrate docker data
systemctl stop docker
cp -au /var/lib/docker /var/lib/docker.bk
rm -rf /var/lib/docker/*
echo "$( blkid | awk '/vdb:/ { print $2 }' ) /var/lib/docker         xfs     defaults     0 0" >> /etc/fstab
mount /var/lib/docker
rsync -a /var/lib/docker.bk/ /var/lib/docker/
rm -rf /var/lib/docker.bk
systemctl start docker
```

#### Creating app network

```bash
docker network create waldur --driver bridge
# verify
docker network ls
```

### Running PostgreSQL

* https://github.com/bitnami/bitnami-docker-postgresql

```bash
docker pull bitnami/postgresql:latest

docker volume create waldur_db
docker volume inspect waldur_db

docker run -d --name waldur-db \
    --network waldur \
    --mount source=waldur_db,target=/bitnami \
    -e POSTGRESQL_USERNAME=waldur \
    -e POSTGRESQL_PASSWORD=waldur \
    -e POSTGRESQL_DATABASE=waldur \
    bitnami/postgresql:latest

docker logs -f waldur-db

# test client
docker run -it --rm \
    --network waldur \
    bitnami/postgresql:latest psql -h waldur-db -U waldur
```

### Running Redis

* https://github.com/bitnami/bitnami-docker-redis
* NB! https://github.com/bitnami/bitnami-docker-redis/issues/81

```bash
# docker host configuration
echo never > /sys/kernel/mm/transparent_hugepage/enabled

docker pull bitnami/redis:latest

docker volume create waldur_queue
docker volume inspect waldur_queue

# run redis
docker run -d --name waldur-queue \
    --network waldur \
    --mount source=waldur_queue,target=/bitnami \
    -e ALLOW_EMPTY_PASSWORD=yes \
    bitnami/redis:latest


# disable protected-mode
sed -i 's/^protected-mode yes/protected-mode no/g' /var/lib/docker/volumes/waldur-queue/_data/redis/conf/redis.conf

# reload redis to pick up configuration change
docker restart waldur-queue

# verify
docker logs -f waldur-queue

# test client
docker run -it --rm \
    --network waldur \
    bitnami/redis:latest redis-cli -h waldur-queue
```

### Running Elasticsearch

* https://github.com/bitnami/bitnami-docker-elasticsearch 

```bash
# docker host configuration
sysctl -w vm.max_map_count=262144
sysctl -w fs.file-max=65536

docker pull bitnami/elasticsearch:latest

docker volume create waldur_events
docker volume inspect waldur_events

docker run -d --name waldur-events \
    --network waldur \
    --mount source=waldur_events,target=/bitnami \
    bitnami/elasticsearch:latest

# verify
docker logs -f waldur-events
```

### Running Logstash

* https://www.elastic.co/guide/en/logstash/5.6/docker.html

```bash
docker pull docker.elastic.co/logstash/logstash:5.6.0
```

#### Create logstash pipeline configuration

```bash
cd ~/repos/waldur-mastermind
VOLUME_NAME=waldur_logs_pipeline
docker volume create --name=$VOLUME_NAME
TARGET_DIR=$( docker volume inspect --format '{{ .Mountpoint }}' $VOLUME_NAME )
cp files/waldur-logs/logstash.conf $TARGET_DIR
chown root:root $TARGET_DIR/logstash.conf
chmod 644 $TARGET_DIR/logstash.conf 
```

#### Create logstash configuration

```bash
cd ~/repos/waldur-mastermind
VOLUME_NAME=waldur_logs_settings
docker volume create --name=$VOLUME_NAME
TARGET_DIR=$( docker volume inspect --format '{{ .Mountpoint }}' $VOLUME_NAME )
cp files/waldur-logs/logstash.yml $TARGET_DIR
chown root:root $TARGET_DIR/logstash.yml
chmod 666 $TARGET_DIR/logstash.yml
cp files/waldur-logs/log4j2.properties $TARGET_DIR
chown root:root $TARGET_DIR/log4j2.properties
chmod 644 $TARGET_DIR/log4j2.properties
```

#### Running Logstash container

```bash
docker run -d --name waldur-logs \
    --network waldur \
    --mount source=waldur_logs_pipeline,target=/usr/share/logstash/pipeline \
    --mount source=waldur_logs_settings,target=/usr/share/logstash/config \
    -e XPACK_MONITORING_ENABLED=false \
    docker.elastic.co/logstash/logstash:5.6.0

# verify
docker logs -f waldur-logs
```


### Running Postfix SMTP

* https://github.com/eea/eea.docker.postfix

```bash
docker pull eeacms/postfix:latest

docker run -d --name=waldur-smtp \
    --network waldur \
    -e  MTP_HOST=demo.waldur.com \
    eeacms/postfix

# verify
docker logs -f waldur-smtp
```
