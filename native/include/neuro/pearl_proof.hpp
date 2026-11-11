// SPDX-License-Identifier: MIT
#pragma once

#include "neuro/pearl_input.hpp"

#include <array>
#include <cstddef>
#include <cstdint>
#include <string>
#include <string_view>
#include <vector>

namespace neuro::proof {

using Target = pearl::Digest; // Unscaled pool target, big-endian U256.
inline constexpr std::size_t max_proof_b64_bytes = 1024 * 1024;
inline constexpr std::size_t witness_block_bytes = 256 * 1024;

// Absolute matrix coordinates; contiguous 8x16 anchors are multiples of 8/16.
struct Tile { int row; int col; };

struct Jackpot {
    std::array<std::uint32_t, 16> message{};
    pearl::Digest hash{}; // BLAKE3 bytes; difficulty interprets this as LE U256.
    bool meets_target = false;
};

struct Verification {
    bool valid = false;
    std::string error;
};

// For this bounded K4096/rank128/8x16 profile, the work factor is exactly 2^19.
// Overflow is an error, never saturation to an automatically passing target.
Target scaled_target(const Target& pool_target_be);

// Candidate evaluation only. Acceptance still requires the real Rust verifier.
// The caller must supply the transcript for this PreparedInput; verification
// independently recomputes the selected tile from committed signal strips.
Jackpot evaluate_tile(const pearl::PreparedInput& input,
                      const fullk::Result& arithmetic, Tile tile,
                      const Target& pool_target_be);

// Build a plain Merkle proof, not a STARK. This does not imply a winning tile.
// These producer calls accept the bounded first zero-B inputs from iteration 3.
std::string build_plain(const pearl::Request& request,
                        const pearl::PreparedInput& input, Tile tile);

struct MatrixWitness {
    std::vector<pearl::Digest> subroots;
    std::vector<std::uint8_t> blocks;
    std::vector<std::uint32_t> block_indices;
    pearl::Digest root{};
};

struct Witness { MatrixWitness a; MatrixWitness bt; };

// Computes genuine non-root BLAKE3 chaining values, retaining only the selected
// 256-chunk A block. The zero-B witness uses the donor's documented zero cache.
Witness make_witness(const pearl::Request& request,
                      const pearl::PreparedInput& input, Tile tile);
std::string build_witness(const pearl::Request& request, Tile tile,
                          const Witness& witness);

// Bounded dense-proof harness. A structural preflight limits counts and checks
// this profile before the pinned Rust parser; cryptographic acceptance comes
// from cp_proof_verify, including Merkle binding, noise, GEMM and share target.
// This is not a general parser for arbitrary network certificate formats.
Verification verify_plain(const std::vector<std::uint8_t>& header,
                           std::string_view proof_b64,
                           const Target& pool_target_be, int cert_version);

} // namespace neuro::proof
