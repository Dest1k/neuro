// SPDX-License-Identifier: MIT
#include "neuro/pearl_proof.hpp"
#include "pearl_proof_internal.hpp"

#include "blake3_impl.h"
#include "cp_noise.h"
#include "cp_proof.h"

#include <algorithm>
#include <array>
#include <cstddef>
#include <cstdint>
#include <stdexcept>
#include <string>
#include <vector>

namespace neuro::proof {
namespace {

static_assert(sizeof(pearl::Digest) == 32, "C witness digests must be packed 32-byte arrays");
static_assert(witness_block_bytes == CP_WITNESS_BLOCK_BYTES, "witness block ABI differs");

std::array<std::uint32_t, 8> key_words(const pearl::Digest& key) {
    std::array<std::uint32_t, 8> words{};
    for (std::size_t i = 0; i < words.size(); ++i) {
        for (unsigned b = 0; b < 4; ++b) words[i] |= std::uint32_t(key[i * 4 + b]) << (8 * b);
    }
    return words;
}

pearl::Digest cv_bytes(const std::array<std::uint32_t, 8>& words) {
    pearl::Digest bytes{};
    for (std::size_t i = 0; i < words.size(); ++i) {
        for (unsigned b = 0; b < 4; ++b) bytes[i * 4 + b] = static_cast<std::uint8_t>(words[i] >> (8 * b));
    }
    return bytes;
}

// Assemble a fixed-size BLAKE3 subtree with the official pinned compression
// implementation. A subroot is a chaining value: ROOT must not be set, and
// chunk counters remain offsets in the complete matrix, not in this block.
pearl::Digest block_subroot(const std::uint8_t* bytes, std::size_t block_index,
                            const pearl::Digest& key) {
    const auto initial = key_words(key);
    std::array<pearl::Digest, CP_WITNESS_BLOCK_CHUNKS> layer{};
    for (std::size_t chunk = 0; chunk < layer.size(); ++chunk) {
        auto cv = initial;
        const auto counter = static_cast<std::uint64_t>(block_index * layer.size() + chunk);
        for (std::size_t block = 0; block < BLAKE3_CHUNK_LEN / BLAKE3_BLOCK_LEN; ++block) {
            const auto flags = static_cast<std::uint8_t>(KEYED_HASH |
                (block == 0 ? CHUNK_START : 0) |
                (block + 1 == BLAKE3_CHUNK_LEN / BLAKE3_BLOCK_LEN ? CHUNK_END : 0));
            blake3_compress_in_place(cv.data(),
                bytes + chunk * BLAKE3_CHUNK_LEN + block * BLAKE3_BLOCK_LEN,
                BLAKE3_BLOCK_LEN, counter, flags);
        }
        layer[chunk] = cv_bytes(cv);
    }
    for (std::size_t width = layer.size(); width > 1; width /= 2) {
        for (std::size_t parent = 0; parent < width / 2; ++parent) {
            std::array<std::uint8_t, BLAKE3_BLOCK_LEN> block{};
            std::copy(layer[parent * 2].begin(), layer[parent * 2].end(), block.begin());
            std::copy(layer[parent * 2 + 1].begin(), layer[parent * 2 + 1].end(), block.begin() + 32);
            auto cv = initial;
            blake3_compress_in_place(cv.data(), block.data(), BLAKE3_BLOCK_LEN, 0, KEYED_HASH | PARENT);
            layer[parent] = cv_bytes(cv);
        }
    }
    return layer[0];
}

std::vector<std::uint32_t> selected_blocks(int rows, int anchor, bool is_bt) {
    std::array<std::uint32_t, CP_WITNESS_MAX_BLOCKS> indices{};
    const int count = cp_proof_witness_blocks(CP_TILE_LAYOUT_CONTIGUOUS, is_bt ? 1 : 0,
                                             anchor, rows, fullk::inner,
                                             indices.data(), indices.size());
    if (count <= 0 || static_cast<std::size_t>(count) > indices.size()) {
        throw std::runtime_error("real witness block selector rejected a validated tile");
    }
    return {indices.begin(), indices.begin() + count};
}

CpMatrixWitness ffi_witness(const MatrixWitness& witness) {
    CpMatrixWitness result{};
    result.subroots = witness.subroots.empty() ? nullptr : witness.subroots.front().data();
    result.num_subroots = witness.subroots.size();
    result.blocks = witness.blocks.empty() ? nullptr : witness.blocks.data();
    result.block_idx = witness.block_indices.empty() ? nullptr : witness.block_indices.data();
    result.num_blocks = witness.block_indices.size();
    result.root = witness.root.data();
    return result;
}

} // namespace

Witness make_witness(const pearl::Request& request, const pearl::PreparedInput& input, Tile tile) {
    detail::validate_prepared(request, input);
    detail::validate_tile(request.shape, tile);
    Witness result;
    result.a.root = input.commitment_a;
    result.bt.root = input.commitment_b;
    const std::size_t count = input.signal_a.size() / witness_block_bytes;
    result.a.subroots.reserve(count);
    const auto* bytes = reinterpret_cast<const std::uint8_t*>(input.signal_a.data());
    for (std::size_t block = 0; block < count; ++block) {
        result.a.subroots.push_back(block_subroot(bytes + block * witness_block_bytes, block, input.job_key));
    }
    result.a.block_indices = selected_blocks(request.shape.rows, tile.row, false);
    result.a.blocks.reserve(result.a.block_indices.size() * witness_block_bytes);
    for (const auto block : result.a.block_indices) {
        const auto* begin = bytes + std::size_t(block) * witness_block_bytes;
        result.a.blocks.insert(result.a.blocks.end(), begin, begin + witness_block_bytes);
    }
    // Empty Bt blocks and subroots request the real Rust zero-matrix cache;
    // expected root is still supplied and checked by the witness builder.
    return result;
}

std::string build_witness(const pearl::Request& request, Tile tile, const Witness& witness) {
    detail::validate_request(request);
    detail::validate_tile(request.shape, tile);
    const std::size_t expected_subroots = std::size_t(request.shape.rows) * fullk::inner / witness_block_bytes;
    const auto expected_blocks = selected_blocks(request.shape.rows, tile.row, false);
    if (witness.a.subroots.size() != expected_subroots || witness.a.block_indices != expected_blocks ||
        witness.a.blocks.size() != expected_blocks.size() * witness_block_bytes) {
        throw std::invalid_argument("A witness sizes or selected block indices differ from the bounded tile");
    }
    if (!witness.bt.subroots.empty() || !witness.bt.blocks.empty() || !witness.bt.block_indices.empty()) {
        throw std::invalid_argument("bounded zero-B witness requires empty Bt subroots and blocks");
    }
    const auto a = ffi_witness(witness.a);
    const auto bt = ffi_witness(witness.bt);
    std::vector<char> output(max_proof_b64_bytes + 1, '\0');
    std::array<char, detail::error_capacity> error{};
    const int status = cp_proof_build_witness(request.header.data(), request.header.size(),
                                             PEARL_CONTIGUOUS_CONFIG, 52, &a, &bt,
                                             request.shape.rows, request.shape.cols,
                                             fullk::inner, fullk::rank, tile.row, tile.col,
                                             CP_TILE_LAYOUT_CONTIGUOUS,
                                             output.data(), output.size(), error.data(), error.size());
    if (status != 0) throw std::runtime_error(std::string("real witness-proof builder: ") + error.data());
    return detail::output_string(output);
}

} // namespace neuro::proof
