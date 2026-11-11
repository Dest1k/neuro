# Bounded lifecycle and real loopback wire integration. Called from proof mode.
# The base donor archive already contains the genuine transport/utility code.
# Its generated bounded copy excludes the unused asynchronous reader.

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

  add_executable(neuro_attempt_search_test native/tests/attempt_search_test.cpp)
  target_include_directories(neuro_attempt_search_test PRIVATE native)
  target_link_libraries(neuro_attempt_search_test PRIVATE neuro_mining_job)
  add_test(NAME neuro_attempt_search COMMAND neuro_attempt_search_test)
  set_tests_properties(neuro_attempt_search PROPERTIES TIMEOUT 120)

  add_executable(neuro_pool_wire_test native/tests/pool_wire_test.cpp)
  target_include_directories(neuro_pool_wire_test PRIVATE native)
  target_link_libraries(neuro_pool_wire_test PRIVATE neuro_pool_wire)
  add_test(NAME neuro_pool_wire COMMAND neuro_pool_wire_test)
  set_tests_properties(neuro_pool_wire PROPERTIES TIMEOUT 30)
endif()
