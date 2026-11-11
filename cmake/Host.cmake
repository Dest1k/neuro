# Bounded lifecycle and real loopback wire integration. Called from proof mode.
# Keep networking in the existing donor archive so its cyclic utility symbols
# resolve together. The asynchronous donor session/reader is never started.
target_sources(neuro_pearl_donor PRIVATE
  "${CPPMINER}/src/common/cp_pool_notify.cpp"
  "${CPPMINER}/src/common/cp_tcp.cpp"
  "${CPPMINER}/src/common/cp_json_frame.cpp"
  "${CPPMINER}/src/common/cp_util.cpp"
  "${CPPMINER}/src/common/cp_state.cpp")

add_library(neuro_mining_job STATIC native/host/mining_job.cpp)
target_include_directories(neuro_mining_job PUBLIC native/include)
target_link_libraries(neuro_mining_job PUBLIC neuro_pearl_proof Threads::Threads)

add_library(neuro_pool_wire STATIC native/host/pool_wire.cpp)
target_include_directories(neuro_pool_wire PUBLIC native/include)
target_link_libraries(neuro_pool_wire PUBLIC neuro_mining_job)

add_executable(neuro_pool_replay_client native/host/pool_replay_client.cpp)
target_link_libraries(neuro_pool_replay_client PRIVATE neuro_pool_wire)

if(NEURO_BUILD_CPU_CHECKS)
  add_executable(neuro_mining_job_test native/tests/mining_job_test.cpp)
  target_include_directories(neuro_mining_job_test PRIVATE native)
  target_link_libraries(neuro_mining_job_test PRIVATE neuro_mining_job)
  add_test(NAME neuro_mining_job COMMAND neuro_mining_job_test)
  set_tests_properties(neuro_mining_job PROPERTIES TIMEOUT 120)

  add_executable(neuro_pool_wire_test native/tests/pool_wire_test.cpp)
  target_include_directories(neuro_pool_wire_test PRIVATE native)
  target_link_libraries(neuro_pool_wire_test PRIVATE neuro_pool_wire)
  add_test(NAME neuro_pool_wire COMMAND neuro_pool_wire_test)
  set_tests_properties(neuro_pool_wire PROPERTIES TIMEOUT 30)
endif()
