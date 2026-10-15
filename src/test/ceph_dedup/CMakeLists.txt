# ceph_test_dedup_merge
# Not run by 'make check': a benchmark taking minutes by default.
add_executable(ceph_test_dedup_merge
  test_dedup_merge.cc)
target_link_libraries(ceph_test_dedup_merge
  ${UNITTEST_LIBS})
install(TARGETS ceph_test_dedup_merge
  DESTINATION ${CMAKE_INSTALL_BINDIR})
