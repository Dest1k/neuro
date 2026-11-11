cmake_minimum_required(VERSION 3.24)
project(neuro LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
option(NEURO_BUILD_CPU_CHECKS "Build offline CPU component checks" ON)
option(NEURO_ENABLE_CUDA "Build bounded GPU instruction probe (not a miner)" OFF)

include(CTest)
set(CPPMINER "${CMAKE_CURRENT_SOURCE_DIR}/upstream/cppminer")

if(NEURO_BUILD_CPU_CHECKS)
  if(NOT EXISTS "${CPPMINER}/include/cp_config.h")
    message(FATAL_ERROR "Initialize pinned dependencies: git submodule update --init --recursive")
  endif()
  add_executable(neuro_cli_test "${CPPMINER}/tests/cp_cli_test.cpp")
  target_include_directories(neuro_cli_test PRIVATE "${CPPMINER}/include")
  add_test(NAME neuro_cli COMMAND neuro_cli_test)

  add_executable(neuro_protocol_test
    "${CPPMINER}/tests/cp_pool_protocol_test.cpp"
    "${CPPMINER}/src/common/cp_json_frame.cpp"
    "${CPPMINER}/src/common/cp_pool_notify.cpp"
    "${CPPMINER}/src/common/cp_util.cpp"
    "${CPPMINER}/src/common/cp_state.cpp")
  target_include_directories(neuro_protocol_test PRIVATE "${CPPMINER}/include")
  if(WIN32)
    target_link_libraries(neuro_protocol_test PRIVATE bcrypt ws2_32)
  endif()
  add_test(NAME neuro_protocol COMMAND neuro_protocol_test)

  add_executable(neuro_mma_layout_test native/tests/mma_layout_test.cpp)
  target_include_directories(neuro_mma_layout_test PRIVATE native/include)
  add_test(NAME neuro_mma_layout COMMAND neuro_mma_layout_test)

  # This limited probe uses GCC LTO to omit unused noise/SIMD dependencies.
  # It does not replace a full build of CPPminer or the Rust proof library.
  if(CMAKE_CXX_COMPILER_ID STREQUAL "GNU" AND UNIX AND NOT APPLE)
    add_executable(neuro_math_probe native/tests/cppminer_math_probe.cpp
      "${CPPMINER}/src/cpu/gemm/case33_gemm_xor.cpp"
      "${CPPMINER}/src/cpu/gemm/case33_cpu_features.cpp")
    target_include_directories(neuro_math_probe PRIVATE
      "${CPPMINER}/include" "${CPPMINER}/src/cpu/gemm")
    target_compile_options(neuro_math_probe PRIVATE -O2 -flto -ffunction-sections -fdata-sections)
    target_link_options(neuro_math_probe PRIVATE -flto -Wl,--gc-sections)
    add_test(NAME neuro_math COMMAND neuro_math_probe)
  else()
    message(STATUS "GCC/Linux LTO math probe omitted; other CPU checks remain available")
  endif()
endif()

if(NEURO_ENABLE_CUDA)
  # CUDA 12.8+ knows sm_120. Do not silently substitute a datacenter Blackwell target.
  if(NOT DEFINED CMAKE_CUDA_ARCHITECTURES)
    set(CMAKE_CUDA_ARCHITECTURES 120)
  endif()
  enable_language(CUDA)
  if(CMAKE_CUDA_COMPILER_VERSION VERSION_LESS 12.8)
    message(FATAL_ERROR "RTX 50 target requires CUDA Toolkit 12.8 or newer")
  endif()
  add_executable(neuro_mma_probe native/cuda/mma_probe.cu)
  target_include_directories(neuro_mma_probe PRIVATE native/include)
  set_target_properties(neuro_mma_probe PROPERTIES CUDA_STANDARD 17 CUDA_STANDARD_REQUIRED ON)
  # GPU execution is explicit; ordinary ctest stays usable without a GPU.
endif()
