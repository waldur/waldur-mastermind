#!/usr/bin/env bash

cat <<EOF >> /var/lib/postgresql/data/postgresql.conf
fsync = off
synchronous_commit = off
full_page_writes = off
max_connections = 1000

shared_buffers = 1GB
effective_cache_size = 4GB
work_mem = 32MB
maintenance_work_mem = 32MB
temp_buffers = 16MB
wal_buffers = 48MB
EOF
