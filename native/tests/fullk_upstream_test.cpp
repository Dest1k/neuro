// SPDX-License-Identifier: MIT
// Differential arithmetic test against the pinned CPPminer scalar backend.
// Requires the documented Linux/GCC LTO recipe. No noise/hash/proof stubs are
// supplied; the fixed scalar/separate path discards unused dependencies.

#include "neuro/fullk.hpp"
#include "case33_gemm_xor.hpp"

#include <array>
#include <cstddef>
#include <cstdint>
#include <iostream>
#include <stdexcept>
#include <string>
#include <vector>

namespace fullk = neuro::fullk;

namespace {

struct TestCase {
    const char* name;
    fullk::Shape shape;
    fullk::Pattern pattern;
    std::uint32_t seed;
};

template <typename T>
std::size_t compare_all(const std::vector<T>& actual, const std::vector<T>& expected,
                        const std::string& label) {
    if (actual.size() != expected.size()) {
        throw std::runtime_error(label + ": output length mismatch");
    }
    for (std::size_t i = 0; i < actual.size(); ++i) {
        if (actual[i] != expected[i]) {
            throw std::runtime_error(label + ": mismatch at index " + std::to_string(i) +
                                     ", got " + std::to_string(actual[i]) +
                                     ", expected " + std::to_string(expected[i]));
        }
    }
    return actual.size();
}

}  // namespace

int main() {
    static_assert(fullk::inner == K_DIM && fullk::rank == R_RANK,
                  "Review upstream K/rank before changing the baseline pin.");
    static_assert(fullk::hash_rows == Case33GemmXor::kTileRows &&
                  fullk::hash_cols == Case33GemmXor::kTileCols,
                  "Review upstream transcript tile shape.");
    try {
        constexpr std::array<TestCase, 4> cases{{
            {"mixed_signed", {128, 128}, fullk::Pattern::mixed_signed, 0},
            {"int8_edges", {128, 128}, fullk::Pattern::int8_edges, 0},
            {"milestone_edges", {128, 256}, fullk::Pattern::milestone_edges, 0},
            {"coordinate_mix", {128, 256}, fullk::Pattern::coordinate_mix, 0x12345678u}
        }};
        std::size_t reference_to_upstream = 0;
        std::size_t emulation_to_upstream = 0;
        std::size_t final_matrix_comparisons = 0;
        for (const auto& test : cases) {
            const auto input = fullk::make_input(test.shape, test.pattern, test.seed);
            const auto expected = fullk::reference(input);
            const auto emulated = fullk::emulate_warp(input);

            Case33GemmXor upstream;
            upstream.set_isa(Case33Isa::Scalar);
            upstream.set_int8_mode(Case32Int8Mode::ExactS8S8);
            upstream.set_prepack_mode(Case33PrepackMode::Separate);
            if (!upstream.init(test.shape.rows, test.shape.cols, fullk::inner,
                               input.a.data(), input.bt.data())) {
                throw std::runtime_error(std::string(test.name) +
                                         ": upstream init failed: " + upstream.simd_error());
            }
            if (upstream.isa_used() != Case33Isa::Scalar || upstream.fast_u8s8_active()) {
                throw std::runtime_error("upstream exact signed-int8 scalar path not selected");
            }
            upstream.run();
            const std::string label = test.name;
            if (upstream.tile_xor().size() != fullk::transcript_words(test.shape)) {
                throw std::runtime_error(label + ": upstream transcript dimensions");
            }
            reference_to_upstream += compare_all(
                expected.transcript, upstream.tile_xor(), label + " oracle vs upstream");
            emulation_to_upstream += compare_all(
                emulated.transcript, upstream.tile_xor(), label + " emulator vs upstream");
            // CPPminer exposes transcript here, not the dense final matrix.
            // This last comparison is only between our independent CPU paths.
            final_matrix_comparisons += compare_all(
                emulated.final_c, expected.final_c, label + " emulator vs oracle final C");
        }

        std::cout << "{\"schema\":\"neuro.fullk_upstream_test.v1\",\"status\":\"PASS\","
                     "\"kind\":\"cpu_scalar_differential\","
                     "\"gpu_execution_observed\":false,\"mining_evidence\":false,"
                     "\"fixture_count\":" << cases.size()
                  << ",\"reference_upstream_transcript_words\":" << reference_to_upstream
                  << ",\"emulation_upstream_transcript_words\":" << emulation_to_upstream
                  << ",\"reference_emulation_final_values\":" << final_matrix_comparisons
                  << ",\"checked_values\":"
                  << reference_to_upstream + emulation_to_upstream + final_matrix_comparisons
                  << "}\n";
        return 0;
    } catch (const std::exception& error) {
        std::cerr << "Full-K upstream differential failed: " << error.what() << '\n';
        return 1;
    }
}
