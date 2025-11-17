#!/bin/sh
bin/git-archive-all.sh --format tar --ignore "ceph-.*-corpus" -v --prefix ceph-croit_edge/
echo "CEPH_GIT_VER = $(git rev-parse HEAD)" > src/.git_version
echo "CEPH_NICE_GIT_VER = $(git rev-parse --short HEAD)" >> src/.git_version
cat src/.git_version
tar -rf ceph-croit_edge.tar ../ceph-croit_edge/src/.git_version
gzip ceph-croit_edge.tar
