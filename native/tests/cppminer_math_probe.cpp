// SPDX-License-Identifier: MIT
// Independent arithmetic probe against CPPminer's pinned scalar GEMM/XOR path.
//
// This is an offline component test. Inputs are already formed signed-int8
// matrices: no matrix generation, noise, BLAKE3, proof, GPU, or pool is tested.
// Upstream B is stored as B^T: N rows of K bytes. The oracle deliberately reads
// those input matrices directly instead of reusing upstream packing/kernel code.

#include "case33_gemm_xor.hpp"

#include <algorithm>
#include <cstdint>
#include <exception>
#include <functional>
#include <iomanip>
#include <iostream>
#include <limits>
#include <stdexcept>
#include <string>
#include <vector>

namespace {

constexpr int kK = K_DIM;
constexpr int kRank = R_RANK;
constexpr int kRows = Case33GemmXor::kMacroM;
constexpr int kCols = Case33GemmXor::kMacroN;
constexpr int kTileRows = Case33GemmXor::kTileRows;
constexpr int kTileCols = Case33GemmXor::kTileCols;

static_assert(kTileRows == 8 && kTileCols == 16,
              "The pinned reference uses 8x16 semantic tiles; review on upgrade.");
static_assert(kK == 4096 && kRank == 128,
              "Review fixed probe coverage when upstream K/rank changes.");
static_assert(static_cast<int64_t>(kK) * 128 * 128 <=
                  std::numeric_limits<int32_t>::max(),
              "Probe accumulation must fit int32_t for all int8 inputs.");

struct Input {
    int m;
    int n;
    std::vector<int8_t> a;
    std::vector<int8_t> bt;

    Input(int rows, int cols)
        : m(rows), n(cols),
          a(static_cast<size_t>(rows) * kK, 0),
          bt(static_cast<size_t>(cols) * kK, 0) {}
};

// Int64 accumulation provides a simple independent numerical reference.
// For each C[row,col], retain the cumulative dot product across milestones.
// Only the final representation conversion and tile reduction use uint32_t.
std::vector<uint32_t> oracle(const Input& input) {
    const int tile_cols = input.n / kTileCols;
    const size_t tile_count =
        static_cast<size_t>(input.m / kTileRows) * tile_cols;
    std::vector<uint32_t> out(
        static_cast<size_t>(kK / kRank) * tile_count, 0);

    for (int row = 0; row < input.m; ++row) {
        for (int col = 0; col < input.n; ++col) {
            const size_t a_start = static_cast<size_t>(row) * kK;
            const size_t b_start = static_cast<size_t>(col) * kK;
            const size_t tile =
                static_cast<size_t>(row / kTileRows) * tile_cols +
                static_cast<size_t>(col / kTileCols);
            int64_t sum = 0;
            for (int milestone = 0; milestone < kK / kRank; ++milestone) {
                const int begin = milestone * kRank;
                for (int k = begin; k < begin + kRank; ++k) {
                    sum += static_cast<int32_t>(input.a[a_start + k]) *
                           static_cast<int32_t>(input.bt[b_start + k]);
                }
                if (sum < std::numeric_limits<int32_t>::min() ||
                    sum > std::numeric_limits<int32_t>::max()) {
                    throw std::runtime_error("oracle int32 range exceeded");
                }
                out[static_cast<size_t>(milestone) * tile_count + tile] ^=
                    static_cast<uint32_t>(static_cast<int32_t>(sum));
            }
        }
    }
    return out;
}

uint32_t next_value(uint32_t& state) {
    state ^= state << 13;
    state ^= state >> 17;
    state ^= state << 5;
    return state;
}

void fill_palette(Input& input, const std::vector<int>& values, uint32_t seed) {
    for (int8_t& value : input.a) {
        value = static_cast<int8_t>(values[next_value(seed) % values.size()]);
    }
    for (int8_t& value : input.bt) {
        value = static_cast<int8_t>(values[next_value(seed) % values.size()]);
    }
}

bool check(const std::string& name, const Input& input, bool require_nonzero) {
    Case33GemmXor upstream;
    upstream.set_isa(Case33Isa::Scalar);
    upstream.set_int8_mode(Case32Int8Mode::ExactS8S8);
    upstream.set_prepack_mode(Case33PrepackMode::Separate);

    if (!upstream.init(input.m, input.n, kK, input.a.data(), input.bt.data())) {
        std::cerr << "FAIL " << name << ": upstream init rejected dimensions: "
                  << upstream.simd_error() << '\n';
        return false;
    }
    if (upstream.isa_used() != Case33Isa::Scalar ||
        upstream.fast_u8s8_active()) {
        std::cerr << "FAIL " << name << ": scalar signed-int8 path not selected\n";
        return false;
    }

    upstream.run();
    const auto expected = oracle(input);
    const auto& actual = upstream.tile_xor();
    if (actual.size() != expected.size()) {
        std::cerr << "FAIL " << name << ": result length " << actual.size()
                  << ", expected " << expected.size() << '\n';
        return false;
    }
    const size_t tile_count =
        static_cast<size_t>(input.m / kTileRows) * (input.n / kTileCols);
    const size_t nonzero = static_cast<size_t>(std::count_if(
        expected.begin(), expected.end(), [](uint32_t v) { return v != 0; }));
    if (require_nonzero && nonzero == 0) {
        std::cerr << "FAIL " << name << ": fixture has only cancelling XORs\n";
        return false;
    }
    for (size_t i = 0; i < expected.size(); ++i) {
        if (actual[i] != expected[i]) {
            std::cerr << "FAIL " << name
                      << ": milestone=" << i / tile_count
                      << " tile=" << i % tile_count
                      << " expected=0x" << std::hex << expected[i]
                      << " actual=0x" << actual[i] << std::dec << '\n';
            return false;
        }
    }

    std::cout << "PASS " << name << " M=" << input.m << " N=" << input.n
              << " K=" << kK << " milestones=" << kK / kRank
              << " compared=" << expected.size()
              << " nonzero=" << nonzero << '\n';
    return true;
}

}  // namespace

int main() {
    try {
        bool ok = true;

        Input zeros(kRows, kCols);
        ok = check("zero", zeros, false) && ok;

        Input int7(kRows, kCols);
        fill_palette(int7, {-64, -63, -1, 0, 1, 62, 63}, 0x7341a52bu);
        ok = check("signed-int7-boundaries", int7, true) && ok;

        // Mathematical int8 stress, not a claim that these values are a valid
        // Pearl-generated/noised matrix distribution.
        Input int8(kRows, kCols);
        fill_palette(int8, {-128, -127, -65, -64, -1, 0, 1, 63, 64, 126, 127},
                     0x25d9c83fu);
        ok = check("signed-int8-extremes", int8, true) && ok;

        // Only one output cell is nonzero. Products straddle each rank boundary
        // and alternate signs, exposing reset-per-panel or off-by-one errors.
        Input boundaries(kRows, kCols);
        for (int milestone = 0; milestone < kK / kRank; ++milestone) {
            const int first = milestone * kRank;
            const int last = first + kRank - 1;
            const int8_t sign = milestone % 2 == 0 ? 1 : -1;
            boundaries.a[static_cast<size_t>(kTileRows - 1) * kK + first] = 63;
            boundaries.a[static_cast<size_t>(kTileRows - 1) * kK + last] = -64;
            boundaries.bt[static_cast<size_t>(kTileCols) * kK + first] = sign;
            boundaries.bt[static_cast<size_t>(kTileCols) * kK + last] = 2;
        }
        ok = check("rank-boundaries-cumulative", boundaries, true) && ok;

        // Unequal macro counts and deterministic asymmetric data exercise
        // spatial tile ordering, transposed B indexing, and the second macro.
        Input spatial(kRows, 2 * kCols);
        fill_palette(spatial, {-64, -7, -1, 0, 2, 13, 63}, 0xb92c4e01u);
        ok = check("rectangular-two-macros", spatial, true) && ok;

        if (!ok) {
            return 1;
        }
        std::cout << "All scalar GEMM/milestone-XOR component checks passed. "
                     "No proof, GPU, pool, or hashrate validation performed.\n";
        return 0;
    } catch (const std::exception& error) {
        std::cerr << "FAIL exception: " << error.what() << '\n';
        return 1;
    }
}
