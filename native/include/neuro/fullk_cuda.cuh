#pragma once

#include "neuro/fullk.hpp"
#include <cuda_runtime_api.h>

namespace neuro::fullk {

// A[rows][4096] and Bt[cols][4096] are contiguous signed-byte device arrays.
// `transcript` has transcript_words(shape) uint32 entries. Optional final_c has
// rows*cols INT32 entries. This function performs no allocation or synchronization;
// its return value reports argument validation or immediate launch errors only.
// The caller must synchronize/check its stream before trusting either output.
// Invalid dimensions or null a/bt/transcript fail before any CUDA runtime call.
cudaError_t launch_fullk(const std::int8_t* a, const std::int8_t* bt,
                        std::uint32_t* transcript, std::int32_t* final_c,
                        Shape shape, cudaStream_t stream = nullptr);

} // namespace neuro::fullk
