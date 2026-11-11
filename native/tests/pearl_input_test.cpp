// SPDX-License-Identifier: MIT
#include "neuro/pearl_input.hpp"
#include "fixtures/pearl_golden.hpp"

#include "blake3.h"
#include "cp_noise.h"

#include <algorithm>
#include <array>
#include <cstddef>
#include <cstdint>
#include <functional>
#include <iostream>
#include <limits>
#include <stdexcept>
#include <string>
#include <vector>

namespace pearl = neuro::pearl;
namespace fullk = neuro::fullk;

namespace {

void require(bool condition, const std::string& message) {
    if (!condition) throw std::runtime_error(message);
}

pearl::Request request_for(const neuro::pearl_golden::Fixture& fixture) {
    pearl::Request request;
    request.shape = fixture.shape;
    request.header.assign(fixture.header.begin(), fixture.header.end());
    request.seed.assign(fixture.seed.begin(), fixture.seed.end());
    request.cert_version = fixture.cert_version;
    return request;
}

pearl::Digest digest(const void* data, std::size_t count) {
    pearl::Digest result{};
    blake3_hasher hasher;
    blake3_hasher_init(&hasher);
    blake3_hasher_update(&hasher, data, count);
    blake3_hasher_finalize(&hasher, result.data(), result.size());
    return result;
}

pearl::Digest final_c_digest(const std::vector<std::int32_t>& values) {
    std::vector<std::uint8_t> little_endian(values.size() * 4);
    for (std::size_t i = 0; i < values.size(); ++i) {
        const auto bits = static_cast<std::uint32_t>(values[i]);
        for (unsigned byte = 0; byte < 4; ++byte) {
            little_endian[i * 4 + byte] = static_cast<std::uint8_t>(bits >> (8 * byte));
        }
    }
    return digest(little_endian.data(), little_endian.size());
}

std::size_t check_digest(const pearl::Digest& actual, const pearl::Digest& expected,
                         const std::string& label) {
    for (std::size_t i = 0; i < actual.size(); ++i) {
        if (actual[i] != expected[i]) {
            throw std::runtime_error(label + ": digest mismatch at byte " + std::to_string(i));
        }
    }
    return actual.size();
}

template <typename T>
std::size_t compare_words(const std::vector<T>& actual, const T* expected,
                          std::size_t count, const std::string& label) {
    require(actual.size() == count, label + ": wrong result length");
    require(expected != nullptr || count == 0, label + ": missing fixed expected data");
    for (std::size_t i = 0; i < count; ++i) {
        if (actual[i] != expected[i]) {
            throw std::runtime_error(label + ": mismatch at index " + std::to_string(i));
        }
    }
    return count;
}

void check_signal_layout(const pearl::PreparedInput& prepared, fullk::Shape shape) {
    require(prepared.noisy.shape.rows == shape.rows && prepared.noisy.shape.cols == shape.cols,
            "adapter changed the requested shape");
    require(prepared.signal_a.size() == static_cast<std::size_t>(shape.rows) * fullk::inner &&
            prepared.noisy.a.size() == prepared.signal_a.size(), "A dimensions");
    require(prepared.signal_bt.size() == static_cast<std::size_t>(shape.cols) * fullk::inner &&
            prepared.noisy.bt.size() == prepared.signal_bt.size(), "Bt dimensions");
    require(prepared.changed_row >= 0 && prepared.changed_row < shape.rows, "changed row range");

    bool row_has_nonzero = false;
    for (int row = 0; row < shape.rows; ++row) {
        for (int k = 0; k < fullk::inner; ++k) {
            const int value = prepared.signal_a[static_cast<std::size_t>(row) * fullk::inner + k];
            require(value >= -64 && value <= 63, "A signal outside int7 bounds");
            if (row != prepared.changed_row) require(value == 0, "more than one A row changed");
            else row_has_nonzero = row_has_nonzero || value != 0;
        }
    }
    require(row_has_nonzero, "deterministic changed A row was entirely zero");
    for (const auto value : prepared.signal_bt) require(value == 0, "signal Bt is not zero");
    for (const auto value : prepared.noisy.a) {
        require(value >= -127 && value <= 126, "noisy A outside proven signed-int8 bounds");
    }
    for (const auto value : prepared.noisy.bt) {
        require(value >= -63 && value <= 63, "zero-B noisy Bt outside noise bounds");
    }
}

void contaminate_global_config() {
    // Direct donor calls are only made sequentially in this test. They are not
    // covered by the adapter's private mutex, and no concurrency claim is made.
    pearl_set_cutlass_fused(1);
    pearl_set_contiguous_tiles(0);
    pearl_set_contiguous_tile_shape(4, 64);
}

pearl::Digest current_job_key(const pearl::Request& request) {
    pearl::Digest key{};
    pearl_job_key(request.header.data(), static_cast<int>(request.header.size()), key.data());
    return key;
}

std::size_t check_invalid_requests(const pearl::Request& valid) {
    using Mutation = std::function<void(pearl::Request&)>;
    std::vector<Mutation> mutations;
    for (const std::size_t size : {std::size_t(0), std::size_t(75), std::size_t(77)}) {
        mutations.emplace_back([size](auto& request) { request.header.resize(size); });
    }
    for (const std::size_t size : {std::size_t(0), std::size_t(31), std::size_t(33)}) {
        mutations.emplace_back([size](auto& request) { request.seed.resize(size); });
    }
    for (const auto shape : std::array<fullk::Shape, 13>{{
             {0, 128}, {-128, 128}, {16, 128}, {127, 128}, {129, 128},
             {128, 0}, {128, 16}, {128, 127}, {128, 129},
             {1152, 128}, {128, 1152},
             {std::numeric_limits<int>::max(), 128},
             {128, std::numeric_limits<int>::min()}}}) {
        mutations.emplace_back([shape](auto& request) { request.shape = shape; });
    }
    for (const int version : {-1, 0, 1, 4, std::numeric_limits<int>::max()}) {
        mutations.emplace_back([version](auto& request) { request.cert_version = version; });
    }
    for (const auto tile : std::array<fullk::Shape, 5>{{
             {4, 16}, {8, 8}, {16, 16}, {8, 32}, {0, 16}}}) {
        mutations.emplace_back([tile](auto& request) {
            request.tile_rows = tile.rows;
            request.tile_cols = tile.cols;
        });
    }

    contaminate_global_config();
    const auto before = current_job_key(valid);
    for (const auto& mutation : mutations) {
        auto request = valid;
        mutation(request);
        bool rejected = false;
        try {
            (void)pearl::prepare_zero_b(request);
        } catch (const std::invalid_argument&) {
            rejected = true;
        }
        require(rejected, "malformed request accepted");
        require(current_job_key(valid) == before,
                "invalid request mutated donor config before validation");
    }
    return mutations.size();
}

}  // namespace

int main() {
    try {
        std::size_t fixtures = 0;
        std::size_t digest_bytes = 0;
        std::size_t transcript_words = 0;
        std::size_t final_cells = 0;
        std::size_t matrix_bytes_hashed = 0;

        for (const auto& fixture : neuro::pearl_golden::fixtures) {
            const auto request = request_for(fixture);
            const auto prepared = pearl::prepare_zero_b(request);
            const std::string name = fixture.name;
            check_signal_layout(prepared, fixture.shape);
            require(prepared.changed_row == fixture.changed_row, name + ": wrong changed row");

            digest_bytes += check_digest(prepared.job_key, fixture.job_key, name + " job key");
            digest_bytes += check_digest(prepared.commitment_a, fixture.commitment_a, name + " A commitment");
            digest_bytes += check_digest(prepared.commitment_b, fixture.commitment_b, name + " Bt commitment");
            digest_bytes += check_digest(prepared.b_noise_seed, fixture.b_noise_seed, name + " B noise seed");
            digest_bytes += check_digest(prepared.a_noise_seed, fixture.a_noise_seed, name + " A noise seed");

            auto bound_a = prepared.commitment_a;
            auto bound_b = prepared.commitment_b;
            if (request.cert_version == 3) {
                pearl_bind_root_a(prepared.commitment_a.data(),
                                  static_cast<std::uint32_t>(request.shape.rows), bound_a.data());
                pearl_bind_root_b(prepared.commitment_b.data(),
                                  static_cast<std::uint32_t>(request.shape.cols), bound_b.data());
            }
            digest_bytes += check_digest(bound_a, fixture.bound_a, name + " bound A");
            digest_bytes += check_digest(bound_b, fixture.bound_b, name + " bound Bt");

            digest_bytes += check_digest(digest(prepared.signal_a.data(), prepared.signal_a.size()),
                                          fixture.signal_a_hash, name + " signal A bytes");
            digest_bytes += check_digest(digest(prepared.signal_bt.data(), prepared.signal_bt.size()),
                                          fixture.signal_bt_hash, name + " signal Bt bytes");
            digest_bytes += check_digest(digest(prepared.noisy.a.data(), prepared.noisy.a.size()),
                                          fixture.noisy_a_hash, name + " noisy A bytes");
            digest_bytes += check_digest(digest(prepared.noisy.bt.data(), prepared.noisy.bt.size()),
                                          fixture.noisy_bt_hash, name + " noisy Bt bytes");
            matrix_bytes_hashed += prepared.signal_a.size() + prepared.signal_bt.size() +
                                   prepared.noisy.a.size() + prepared.noisy.bt.size();

            const auto reference = fullk::reference(prepared.noisy);
            const auto emulated = fullk::emulate_warp(prepared.noisy);
            require(fixture.transcript_count == fullk::transcript_words(fixture.shape),
                    name + ": fixed transcript dimensions");
            transcript_words += compare_words(reference.transcript, fixture.transcript,
                                               fixture.transcript_count, name + " reference transcript");
            transcript_words += compare_words(emulated.transcript, fixture.transcript,
                                               fixture.transcript_count, name + " emulator transcript");
            require(reference.final_c.size() ==
                        static_cast<std::size_t>(fixture.shape.rows) * fixture.shape.cols,
                    name + ": final C dimensions");
            final_cells += compare_words(emulated.final_c, reference.final_c.data(),
                                          reference.final_c.size(), name + " final C paths");
            digest_bytes += check_digest(final_c_digest(reference.final_c),
                                          fixture.final_c_hash, name + " reference final C fingerprint");
            digest_bytes += check_digest(final_c_digest(emulated.final_c),
                                          fixture.final_c_hash, name + " emulator final C fingerprint");
            ++fixtures;
        }
        require(fixtures == 3, "expected all three independent Pearl input fixtures");

        const auto& first = neuro::pearl_golden::fixtures.front();
        const auto valid = request_for(first);
        const auto invalid_count = check_invalid_requests(valid);
        require(current_job_key(valid) != first.job_key, "config contamination fixture was ineffective");
        const auto restored = pearl::prepare_zero_b(valid);
        require(restored.job_key == first.job_key, "adapter failed to reset contaminated donor config");
        require(digest(restored.noisy.a.data(), restored.noisy.a.size()) == first.noisy_a_hash &&
                digest(restored.noisy.bt.data(), restored.noisy.bt.size()) == first.noisy_bt_hash,
                "config reset/replay changed the noisy matrices");

        std::cout << "{\"schema\":\"neuro.pearl_input_test.v1\",\"status\":\"PASS\","
                     "\"kind\":\"cpu_pearl_input_and_arithmetic\","
                     "\"gpu_execution_observed\":false,\"proof_verified\":false,"
                     "\"mining_evidence\":false,\"fixture_count\":" << fixtures
                  << ",\"digest_bytes_checked\":" << digest_bytes
                  << ",\"matrix_bytes_fingerprinted\":" << matrix_bytes_hashed
                  << ",\"transcript_words_checked\":" << transcript_words
                  << ",\"final_c_values_checked\":" << final_cells
                  << ",\"invalid_requests_rejected\":" << invalid_count
                  << ",\"global_config_reset_checked\":true}\n";
        return 0;
    } catch (const std::exception& error) {
        std::cerr << "Pearl input check failed: " << error.what() << '\n';
        return 1;
    }
}

