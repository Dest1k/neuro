// SPDX-License-Identifier: MIT
#include "neuro/pearl_input.hpp"

#include "cp_noise.h"

#include <array>
#include <cstddef>
#include <cstdint>
#include <mutex>
#include <stdexcept>
#include <vector>

namespace neuro::pearl {
namespace {

// The donor's mining-config selectors are process globals. This lock serializes
// calls made through this adapter; direct external cp_noise calls are outside
// its synchronization contract.
std::mutex donor_config_mutex;

void validate_request(const Request& request) {
    const int rows = request.shape.rows;
    const int cols = request.shape.cols;
    if (rows < 128 || cols < 128 || rows > fullk::max_dimension ||
        cols > fullk::max_dimension || rows % 128 != 0 || cols % 128 != 0) {
        throw std::invalid_argument("Pearl input dimensions must be multiples of 128 in [128,1024]");
    }
    if (request.header.size() != static_cast<std::size_t>(header_bytes)) {
        throw std::invalid_argument("Pearl input header must contain exactly 76 bytes");
    }
    if (request.seed.size() != static_cast<std::size_t>(attempt_seed_bytes)) {
        throw std::invalid_argument("Pearl attempt seed must contain exactly 32 bytes");
    }
    if (request.cert_version != 2 && request.cert_version != 3) {
        throw std::invalid_argument("Pearl input supports certificate versions 2 and 3 only");
    }
    if (request.tile_rows != fullk::hash_rows || request.tile_cols != fullk::hash_cols) {
        throw std::invalid_argument("Pearl input requires contiguous 8x16 transcript tiles");
    }
}

}  // namespace

PreparedInput prepare_zero_b(const Request& request) {
    // All untrusted dimensions/configuration are rejected before any donor call.
    validate_request(request);
    // cp_noise's get_random_hash copies a native int32 into a byte message.
    // Its pinned encoding is little-endian; do not silently change protocol
    // inputs on a host with another byte order.
    const std::uint32_t endian_probe = 1;
    if (*reinterpret_cast<const unsigned char*>(&endian_probe) != 1) {
        throw std::runtime_error("the pinned Pearl input adapter requires a little-endian host");
    }

    PreparedInput result;
    result.noisy.shape = request.shape;
    const std::size_t a_size = static_cast<std::size_t>(request.shape.rows) * fullk::inner;
    const std::size_t bt_size = static_cast<std::size_t>(request.shape.cols) * fullk::inner;
    result.signal_a.assign(a_size, 0);
    result.signal_bt.assign(bt_size, 0);
    result.noisy.a.resize(a_size);
    result.noisy.bt.resize(bt_size);

    std::lock_guard<std::mutex> lock(donor_config_mutex);
    // The donor default is scattered and would produce a different job key.
    // Explicitly reset every relevant selector on every valid adapter call.
    pearl_set_cutlass_fused(0);
    pearl_set_contiguous_tiles(1);
    pearl_set_contiguous_tile_shape(fullk::hash_rows, fullk::hash_cols);

    pearl_job_key(request.header.data(), header_bytes, result.job_key.data());
    if (pearl_fill_random_a_row(request.seed.data(), attempt_seed_bytes,
                                request.shape.rows, fullk::inner,
                                result.signal_a.data(), &result.changed_row) != 0 ||
        result.changed_row < 0 || result.changed_row >= request.shape.rows) {
        throw std::runtime_error("donor failed to construct the first zero-B A row");
    }

    // Commit the signal matrices, before adding noise. K and supported dimensions
    // make their lengths multiples of the donor's 1024-byte hash chunk.
    pearl_keyed_digest_int8(result.signal_a.data(), result.signal_a.size(),
                            result.job_key.data(), result.commitment_a.data());
    pearl_keyed_digest_int8(result.signal_bt.data(), result.signal_bt.size(),
                            result.job_key.data(), result.commitment_b.data());
    pearl_derive_noise_seeds(result.job_key.data(), result.commitment_a.data(),
                             result.commitment_b.data(),
                             static_cast<std::uint32_t>(request.shape.rows),
                             static_cast<std::uint32_t>(request.shape.cols),
                             request.cert_version == 3,
                             result.b_noise_seed.data(), result.a_noise_seed.data());

    std::vector<std::uint32_t> pairs_a(static_cast<std::size_t>(fullk::inner) * 2);
    std::vector<std::uint32_t> pairs_b(static_cast<std::size_t>(fullk::inner) * 2);
    pearl_build_perm_pairs_a(result.a_noise_seed.data(), fullk::inner, fullk::rank,
                             pairs_a.data());
    pearl_build_perm_pairs_b(result.b_noise_seed.data(), fullk::inner, fullk::rank,
                             pairs_b.data());

    std::array<std::int8_t, fullk::rank> uniform_row{};
    std::array<std::int8_t, fullk::inner> noise_row{};
    for (int row = 0; row < request.shape.rows; ++row) {
        const std::size_t offset = static_cast<std::size_t>(row) * fullk::inner;
        pearl_fuse_noise_row_a_buf(
            row, fullk::inner, fullk::rank, result.a_noise_seed.data(), pairs_a.data(),
            result.signal_a.data() + offset, result.noisy.a.data() + offset,
            uniform_row.data(), noise_row.data());
    }
    for (int col = 0; col < request.shape.cols; ++col) {
        const std::size_t offset = static_cast<std::size_t>(col) * fullk::inner;
        // The real zero-B path permits a null signal row. Output is still Bt:
        // cols rows of 4096 bytes, not a K-by-N row-major matrix.
        pearl_fuse_noise_row_b_buf(
            col, fullk::inner, fullk::rank, result.b_noise_seed.data(), pairs_b.data(),
            nullptr, result.noisy.bt.data() + offset,
            uniform_row.data(), noise_row.data());
    }
    return result;
}

}  // namespace neuro::pearl

