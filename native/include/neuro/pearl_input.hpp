#pragma once

#include "neuro/fullk.hpp"

#include <array>
#include <cstdint>
#include <vector>

namespace neuro::pearl {

using Digest = std::array<std::uint8_t, 32>;
inline constexpr int header_bytes = 76;
inline constexpr int attempt_seed_bytes = 32;

// A bounded first zero-B attempt, not a live pool job or a complete miner.
// Header and seed are byte vectors so the boundary can reject malformed lengths.
struct Request {
    fullk::Shape shape{128, 128};
    std::vector<std::uint8_t> header;
    std::vector<std::uint8_t> seed;
    int cert_version = 3;
    int tile_rows = 8;
    int tile_cols = 16;
};

struct PreparedInput {
    fullk::Input noisy;
    std::vector<std::int8_t> signal_a;
    std::vector<std::int8_t> signal_bt;
    Digest job_key{};
    Digest commitment_a{};
    Digest commitment_b{};
    Digest a_noise_seed{};
    Digest b_noise_seed{};
    int changed_row = -1;
};

// Requires M,N multiples of 128 in [128,1024], header[76], seed[32], certificate
// version 2 (legacy seed binding) or 3, and contiguous 8x16 transcript tiles.
// Invalid input throws std::invalid_argument before donor functions are called.
// Starts A/Bt at zero, changes one A row, commits the signals, derives noise,
// and returns row-major noisy A plus transposed noisy Bt for fullk arithmetic.
// Calls through this adapter are serialized around the donor's global config;
// external direct calls to cp_noise are outside that synchronization contract.
// The pinned donor encoding requires a little-endian host; other byte orders
// fail explicitly. This bounded synchronous adapter does not preempt a live job.
PreparedInput prepare_zero_b(const Request& request);

} // namespace neuro::pearl
