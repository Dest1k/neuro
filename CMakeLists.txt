cmake_minimum_required(VERSION 3.24)
project(neuro LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
option(NEURO_BUILD_CPU_CHECKS "Build offline CPU component checks" ON)
option(NEURO_ENABLE_CUDA "Build bounded CUDA arithmetic kernels and probes (not a miner)" OFF)

include(CTest)
set(CPPMINER "${CMAKE_CURRENT_SOURCE_DIR}/upstream/cppminer")

add_library(neuro_fullk_reference STATIC native/reference/fullk_reference.cpp)
target_include_directories(neuro_fullk_reference PUBLIC native/include native)
add_library(neuro_fullk_warp STATIC native/reference/fullk_warp.cpp)
target_include_directories(neuro_fullk_warp PUBLIC native/include native)

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

  add_executable(neuro_fullk_test native/tests/fullk_test.cpp)
  target_link_libraries(neuro_fullk_test PRIVATE neuro_fullk_reference neuro_fullk_warp)
  add_test(NAME neuro_fullk COMMAND neuro_fullk_test)

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

    # Build the donor differential as one LTO unit; unused donor noise/SIMD
    # paths must not be replaced by successful-looking stubs.
    add_executable(neuro_fullk_upstream_test native/tests/fullk_upstream_test.cpp
      native/reference/fullk_reference.cpp native/reference/fullk_warp.cpp
      "${CPPMINER}/src/cpu/gemm/case33_gemm_xor.cpp"
      "${CPPMINER}/src/cpu/gemm/case33_cpu_features.cpp")
    target_include_directories(neuro_fullk_upstream_test PRIVATE native/include native
      "${CPPMINER}/include" "${CPPMINER}/src/cpu/gemm")
    target_compile_options(neuro_fullk_upstream_test PRIVATE -O2 -flto -ffunction-sections -fdata-sections)
    target_link_options(neuro_fullk_upstream_test PRIVATE -flto -Wl,--gc-sections)
    add_test(NAME neuro_fullk_upstream COMMAND neuro_fullk_upstream_test)
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

  add_library(neuro_fullk_cuda STATIC native/cuda/fullk_kernel.cu)
  target_include_directories(neuro_fullk_cuda PUBLIC native/include)
  set_target_properties(neuro_fullk_cuda PROPERTIES CUDA_STANDARD 17 CUDA_STANDARD_REQUIRED ON)

  add_executable(neuro_fullk_probe native/cuda/fullk_probe.cu)
  target_link_libraries(neuro_fullk_probe PRIVATE neuro_fullk_cuda neuro_fullk_reference)
  set_target_properties(neuro_fullk_probe PROPERTIES CUDA_STANDARD 17 CUDA_STANDARD_REQUIRED ON)
  # This mode rejects malformed calls before any CUDA runtime call. Actual GPU
  # execution remains explicit; ordinary ctest does not initialize a device.
  add_test(NAME neuro_fullk_host_validation COMMAND neuro_fullk_probe --host-validation)
endif()
