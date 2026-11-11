// SPDX-License-Identifier: MIT
// Differential against real donor builders and the scalar GEMM backend.
// The noise/hash/cancellation implementations are linked from pinned upstream
// sources. No replacement cancellation, noise, hash, or proof stubs are used.
#include "neuro/pearl_input.hpp"
#include "fixtures/pearl_golden.hpp"

#include "case33_gemm_xor.hpp"
#include "cp_job_ctrl.h"
#include "cp_noise.h"

#include <cstddef>
#include <cstdint>
#include <iostream>
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

template <typename T>
std::size_t compare_all(const T* actual, const T* expected, std::size_t count,
                        const std::string& label) {
    for (std::size_t i = 0; i < count; ++i) {
        if (actual[i] != expected[i]) {
            throw std::runtime_error(label + ": mismatch at index " + std::to_string(i));
        }
    }
    return count;
}

template <typename T>
std::size_t compare_vectors(const std::vector<T>& actual, const std::vector<T>& expected,
                            const std::string& label) {
    require(actual.size() == expected.size(), label + ": output size mismatch");
    return compare_all(actual.data(), expected.data(), actual.size(), label);
}

}  // namespace

int main() {
    static_assert(fullk::inner == K_DIM && fullk::rank == R_RANK,
                  "Review donor K/rank before changing its pin.");
    static_assert(fullk::hash_rows == Case33GemmXor::kTileRows &&
                  fullk::hash_cols == Case33GemmXor::kTileCols,
                  "Review donor transcript layout before changing its pin.");
    try {
        // No live job is begun and no connection is opened. The linked real
        // cancellation implementation reports inactivity in this offline test.
        require(cp_job_mining_active() == 0 && cp_job_should_cancel() == 0,
                "offline donor cancellation state was unexpectedly active");
        std::size_t fixtures = 0;
        std::size_t seed_bytes = 0;
        std::size_t noisy_bytes = 0;
        std::size_t changed_row_bytes = 0;
        std::size_t reference_transcript = 0;
        std::size_t emulated_transcript = 0;

        for (const auto& fixture : neuro::pearl_golden::fixtures) {
            const auto request = request_for(fixture);
            const auto prepared = pearl::prepare_zero_b(request);
            const std::string label = fixture.name;

            pearl::Digest b_seed{}, a_seed{}, zero_b_seed{}, fast_a_seed{};
            pearl_commitment_seeds(
                prepared.job_key.data(), prepared.signal_a.data(), prepared.signal_bt.data(),
                request.shape.rows, request.shape.cols, fullk::inner, request.cert_version == 3,
                b_seed.data(), a_seed.data());
            seed_bytes += compare_all(b_seed.data(), prepared.b_noise_seed.data(), b_seed.size(),
                                      label + " full commitment B seed");
            seed_bytes += compare_all(a_seed.data(), prepared.a_noise_seed.data(), a_seed.size(),
                                      label + " full commitment A seed");

            pearl_b_noise_seed_from_bt(prepared.job_key.data(), nullptr, request.shape.cols,
                                       fullk::inner, request.cert_version == 3, zero_b_seed.data());
            pearl_a_noise_seed_from_a(prepared.job_key.data(), zero_b_seed.data(),
                                      prepared.signal_a.data(), request.shape.rows, fullk::inner,
                                      request.cert_version == 3, fast_a_seed.data());
            seed_bytes += compare_all(zero_b_seed.data(), prepared.b_noise_seed.data(),
                                      zero_b_seed.size(), label + " zero-B fast seed");
            seed_bytes += compare_all(fast_a_seed.data(), prepared.a_noise_seed.data(),
                                      fast_a_seed.size(), label + " A fast seed");

            std::vector<std::int8_t> generated_a(prepared.signal_a.size());
            require(pearl_generate_random_a(request.seed.data(), pearl::attempt_seed_bytes,
                                             request.shape.rows, fullk::inner,
                                             generated_a.data()) == 0,
                    label + ": full A generator failed");
            const std::size_t row_start =
                static_cast<std::size_t>(prepared.changed_row) * fullk::inner;
            changed_row_bytes += compare_all(
                prepared.signal_a.data() + row_start, generated_a.data() + row_start,
                static_cast<std::size_t>(fullk::inner), label + " row vs full A generator");

            std::vector<std::int8_t> full_a(prepared.signal_a.size());
            std::vector<std::int8_t> full_bt(prepared.signal_bt.size());
            require(pearl_build_noisy_matrices(
                        request.shape.rows, request.shape.cols, fullk::inner, fullk::rank,
                        b_seed.data(), a_seed.data(), prepared.signal_a.data(),
                        prepared.signal_bt.data(), full_a.data(), full_bt.data()) == 0,
                    label + ": full noisy matrix builders failed");
            noisy_bytes += compare_vectors(prepared.noisy.a, full_a, label + " noisy A rows vs full");
            noisy_bytes += compare_vectors(prepared.noisy.bt, full_bt, label + " noisy Bt rows vs full");

            const auto reference = fullk::reference(prepared.noisy);
            const auto emulated = fullk::emulate_warp(prepared.noisy);
            Case33GemmXor donor;
            donor.set_isa(Case33Isa::Scalar);
            donor.set_int8_mode(Case32Int8Mode::ExactS8S8);
            donor.set_prepack_mode(Case33PrepackMode::Separate);
            if (!donor.init(request.shape.rows, request.shape.cols, fullk::inner,
                            full_a.data(), full_bt.data())) {
                throw std::runtime_error(label + ": donor scalar init failed");
            }
            // Keep this guard explicit: GCC LTO uses the surviving scalar
            // condition to discard SIMD branches without replacement symbols.
            if (donor.isa_used() != Case33Isa::Scalar || donor.fast_u8s8_active()) {
                throw std::runtime_error(label + ": donor exact signed-int8 scalar path not selected");
            }
            donor.run();
            require(donor.tile_xor().size() == fullk::transcript_words(request.shape),
                    label + ": donor transcript size");
            reference_transcript += compare_vectors(
                reference.transcript, donor.tile_xor(), label + " reference vs donor transcript");
            emulated_transcript += compare_vectors(
                emulated.transcript, donor.tile_xor(), label + " emulator vs donor transcript");
            ++fixtures;
        }

        require(fixtures == 3, "expected all three Pearl input fixtures");
        std::cout << "{\"schema\":\"neuro.pearl_upstream_test.v1\",\"status\":\"PASS\","
                     "\"kind\":\"cpu_pearl_donor_differential\","
                     "\"gpu_execution_observed\":false,\"proof_verified\":false,"
                     "\"mining_evidence\":false,\"fixture_count\":" << fixtures
                  << ",\"seed_bytes_checked\":" << seed_bytes
                  << ",\"changed_row_bytes_checked\":" << changed_row_bytes
                  << ",\"noisy_matrix_bytes_checked\":" << noisy_bytes
                  << ",\"reference_upstream_transcript_words\":" << reference_transcript
                  << ",\"emulation_upstream_transcript_words\":" << emulated_transcript
                  << ",\"real_cancellation_linked\":true"
                     ",\"pool_connections_opened\":false}\n";
        return 0;
    } catch (const std::exception& error) {
        std::cerr << "Pearl upstream differential failed: " << error.what() << '\n';
        return 1;
    }
}

