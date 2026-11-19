// SPDX-License-Identifier: MIT
#pragma once

#include "neuro/fullk.hpp"

namespace neuro::host {

// Synchronous, bounded CUDA compute callback for the host Coordinator.
// The call validates host inputs before touching CUDA, then synchronizes its
// stream before returning. Device buffers for the same shape are kept for the
// next call; a different shape or device releases that resident set first.
// The result carries the milestone transcript. final_c is empty: the kernel is
// launched with a null final matrix. It does not submit shares.
// Built only when both NEURO_ENABLE_PROOF and NEURO_ENABLE_CUDA are enabled.
fullk::Result compute_cuda(const fullk::Input& input, int device);

} // namespace neuro::host
