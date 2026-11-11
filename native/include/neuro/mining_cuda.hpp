// SPDX-License-Identifier: MIT
#pragma once

#include "neuro/fullk.hpp"

namespace neuro::host {

// Synchronous, bounded CUDA compute callback for the host Coordinator.
// It owns its stream/device buffers until all transfers/kernel work complete.
// It does not submit shares or initialize CUDA before validating host inputs.
// Built only when both NEURO_ENABLE_PROOF and NEURO_ENABLE_CUDA are enabled.
fullk::Result compute_cuda(const fullk::Input& input, int device);

} // namespace neuro::host
