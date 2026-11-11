# Official BLAKE3 1.8.5, pinned independently in upstream/sources.lock.json.
# Compile only the portable C implementation: no Cargo, assembly, SIMD or TBB.
include_guard(GLOBAL)

set(NEURO_BLAKE3_C_DIR "${CMAKE_CURRENT_LIST_DIR}/../upstream/blake3/c")
foreach(source IN ITEMS blake3.c blake3_dispatch.c blake3_portable.c blake3.h blake3_impl.h)
  if(NOT EXISTS "${NEURO_BLAKE3_C_DIR}/${source}")
    message(FATAL_ERROR "Initialize pinned BLAKE3: git submodule update --init --recursive")
  endif()
endforeach()

add_library(neuro_blake3 STATIC
  "${NEURO_BLAKE3_C_DIR}/blake3.c"
  "${NEURO_BLAKE3_C_DIR}/blake3_dispatch.c"
  "${NEURO_BLAKE3_C_DIR}/blake3_portable.c")
target_include_directories(neuro_blake3 PUBLIC "${NEURO_BLAKE3_C_DIR}")
# cp_noise.c also includes blake3_impl.h; keep the portable configuration
# consistent for that consumer as well as all three library source files.
target_compile_definitions(neuro_blake3 PUBLIC
  BLAKE3_NO_SSE2 BLAKE3_NO_SSE41 BLAKE3_NO_AVX2 BLAKE3_NO_AVX512
  BLAKE3_USE_NEON=0)
set_target_properties(neuro_blake3 PROPERTIES
  C_STANDARD 11 C_STANDARD_REQUIRED ON C_EXTENSIONS OFF)
