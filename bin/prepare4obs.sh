#!/bin/sh
if [ -z "$1" ]
  then
    echo "No Ceph version supplied"
    echo "Usage: prepare4obs.sh <ceph-version> <croit-product> <croit version>"
    echo "  example: prepare4obs.sh 18.2.7 croit-advanced v1"
    exit -1
fi
if [ -z "$2" ]
  then
    echo "No Croit product name supplied"
    echo "Usage: prepare4obs.sh <ceph-version> <croit-product> <croit version>"
    echo "  example: prepare4obs.sh 18.2.7 croit-advanced v1"
    exit -1
fi
if [ -z "$3" ]
  then
    echo "No Croit version supplied"
    echo "Usage: prepare4obs.sh <ceph-version> <croit-product> <croit version>"
    echo "  example: prepare4obs.sh 18.2.7 croit-advanced v1"
    exit -1
fi
ceph_ver=ceph-$1
nice_ver=$1_$2_$3
archive_name=$2_$3.tar.gz
#echo $full_ver
bin/git-archive-all.sh --format tar --ignore "ceph-.*-corpus" -v --prefix $ceph_ver/ $ceph_ver.tar
#echo "CEPH_GIT_VER = $(git rev-parse HEAD)" > src/.git_version
#echo "CEPH_NICE_GIT_VER = $(git rev-parse --short HEAD)" >> src/.git_version
echo $(git rev-parse HEAD) > src/.git_version
#echo $(git rev-parse --short HEAD) >> src/.git_version
echo $nice_ver >> src/.git_version
cat src/.git_version
tar --transform "s/^/$ceph_ver\//" -rf $ceph_ver.tar src/.git_version
gzip -f $ceph_ver.tar
#let's use croit product/version in archive name only
mv $ceph_ver.tar.gz $archive_name
