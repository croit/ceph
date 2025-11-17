#!/bin/sh
if [ -z "$1" ]
  then
    echo "No version supplied"
    exit -1
fi
bin/git-archive-all.sh --format tar --ignore "ceph-.*-corpus" -v --prefix $1/ $1.tar
echo "CEPH_GIT_VER = $(git rev-parse HEAD)" > src/.git_version
echo "CEPH_NICE_GIT_VER = $(git rev-parse --short HEAD)" >> src/.git_version
echo $(git rev-parse HEAD) > src/.git_version
echo $(git rev-parse --short HEAD) >> src/.git_version
cat src/.git_version
tar --transform "s/^/$1\//" -rf $1.tar src/.git_version
gzip -f $1.tar
