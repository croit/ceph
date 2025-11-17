#!/bin/bash
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

# This specifies both tar file archive root folder names
# ceph.spec file in OBS project is aware of these names and use references them via
#  Name and Version fields:
# Name:		ceph
# Version:	18.2.7
#
ceph_ver=ceph-$1

# Nice version is what 'ceph versions' command will report
# This includes upstream release, croit product name and version.
# e.g. 18.2.7 ceph-essential v123.5
nice_ver="$1 $2 $3"

# Let's use croit product/version in archive name only
# ceph.spec file in OBS project references this archive name:
# Source:	    croit-advanced_v1.tar.gz
#
archive_name=$2_$3.tar.gz

bin/git-archive-all.sh --format tar --ignore "ceph-.*-corpus" -v --prefix $ceph_ver/ $ceph_ver.tar

echo $(git rev-parse HEAD) > src/.git_version
echo $nice_ver >> src/.git_version
cat src/.git_version
tar --transform "s/^/$ceph_ver\//" -rf $ceph_ver.tar src/.git_version

gzip -f $ceph_ver.tar
mv $ceph_ver.tar.gz $archive_name
