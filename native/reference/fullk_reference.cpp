// SPDX-License-Identifier: MIT
#include "neuro/fullk.hpp"

#include <array>
#include <limits>
#include <stdexcept>

namespace neuro::fullk {
namespace {

static_assert(static_cast<std::int64_t>(inner) * 128 * 128 <=
                  std::numeric_limits<std::int32_t>::max(),
              "All full-K signed-int8 products must fit signed INT32.");

void validate_pattern(Pattern pattern) {
    switch (pattern) {
        case Pattern::zeros:
        case Pattern::mixed_signed:
        case Pattern::int7_edges:
        case Pattern::int8_edges:
        case Pattern::milestone_edges:
        case Pattern::coordinate_mix:
            return;
    }
    throw std::invalid_argument("unknown full-K fixture pattern");
}

// Fixture construction is not Pearl matrix/noise generation. All formulas are
// deterministic and separate from the dense arithmetic oracle below.
std::int8_t fixture_value(bool for_a, int row, int k, Pattern pattern,
                          std::uint32_t seed) {
    const int milestone = k / rank;
    switch (pattern) {
        case Pattern::zeros:
            return 0;
        case Pattern::mixed_signed:
            return static_cast<std::int8_t>(
                for_a ? (row * 17 + k * 11 + milestone * 7) % 127 - 64
                      : (row * 29 + k * 13 + milestone * 3) % 127 - 63);
        case Pattern::int7_edges:
        case Pattern::int8_edges: {
            constexpr std::array<int, 8> int7{{-64, 63, -64, 0, 63, -1, 1, 63}};
            constexpr std::array<int, 8> int8{{-128, 127, -128, 0, 127, -1, 1, 127}};
            const auto& edges = pattern == Pattern::int7_edges ? int7 : int8;
            const int index = for_a ? (row * 5 + k * 3 + milestone) % 8
                                    : (row * 7 + k * 5 + 3 * milestone + 1) % 8;
            return static_cast<std::int8_t>(edges[static_cast<std::size_t>(index)]);
        }
        case Pattern::milestone_edges: {
            const int offset = k % rank;
            if (offset != 0 && offset != rank - 1) return 0;
            return static_cast<std::int8_t>(
                for_a ? (row * 7 + milestone * 3 + (offset == rank - 1 ? 5 : 0)) % 17 - 8
                      : (row * 11 + milestone * 5 + (offset == rank - 1 ? 3 : 0)) % 19 - 9);
        }
        case Pattern::coordinate_mix: {
            // Every operation on x wraps in uint32_t, including multiplication.
            std::uint32_t x = seed ^ (for_a ? 0x243f6a88u : 0x9e3779b9u);
            x ^= static_cast<std::uint32_t>(row) * 0x85ebca6bu;
            x ^= static_cast<std::uint32_t>(k) * 0xc2b2ae35u;
            x ^= x >> 16;
            x *= 0x7feb352du;
            x ^= x >> 15;
            x *= 0x846ca68bu;
            x ^= x >> 16;
            return static_cast<std::int8_t>(static_cast<int>(x & 127u) - 64);
        }
    }
    throw std::invalid_argument("unknown full-K fixture pattern");
}

}  // namespace

Input make_input(Shape shape, Pattern pattern, std::uint32_t seed) {
    validate_shape(shape);
    validate_pattern(pattern);
    Input input{shape,
                std::vector<std::int8_t>(static_cast<std::size_t>(shape.rows) * inner),
                std::vector<std::int8_t>(static_cast<std::size_t>(shape.cols) * inner)};
    for (int row = 0; row < shape.rows; ++row) {
        for (int k = 0; k < inner; ++k) {
            input.a[static_cast<std::size_t>(row) * inner + k] =
                fixture_value(true, row, k, pattern, seed);
        }
    }
    for (int col = 0; col < shape.cols; ++col) {
        for (int k = 0; k < inner; ++k) {
            input.bt[static_cast<std::size_t>(col) * inner + k] =
                fixture_value(false, col, k, pattern, seed);
        }
    }
    return input;
}

Result reference(const Input& input) {
    validate_shape(input.shape);
    const std::size_t expected_a = static_cast<std::size_t>(input.shape.rows) * inner;
    const std::size_t expected_bt = static_cast<std::size_t>(input.shape.cols) * inner;
    if (input.a.size() != expected_a || input.bt.size() != expected_bt) {
        throw std::invalid_argument("full-K input lengths do not match shape");
    }

    const int rows = input.shape.rows;
    const int cols = input.shape.cols;
    const int tile_cols = cols / hash_cols;
    const std::size_t tiles = tile_count(input.shape);
    Result output;
    output.transcript.assign(static_cast<std::size_t>(milestones) * tiles, 0);
    output.final_c.resize(static_cast<std::size_t>(rows) * cols);

    // Intentionally simple cell-wise dot products. No MMA coordinates, register
    // packing, candidate helpers, or upstream arithmetic are used by this oracle.
    for (int row = 0; row < rows; ++row) {
        const std::size_t a_start = static_cast<std::size_t>(row) * inner;
        for (int col = 0; col < cols; ++col) {
            const std::size_t bt_start = static_cast<std::size_t>(col) * inner;
            const std::size_t tile =
                static_cast<std::size_t>(row / hash_rows) * tile_cols + col / hash_cols;
            std::int64_t cumulative = 0;
            for (int milestone = 0; milestone < milestones; ++milestone) {
                const int end = (milestone + 1) * rank;
                for (int k = milestone * rank; k < end; ++k) {
                    cumulative += static_cast<std::int64_t>(input.a[a_start + k]) *
                                  static_cast<std::int64_t>(input.bt[bt_start + k]);
                }
                if (cumulative < std::numeric_limits<std::int32_t>::min() ||
                    cumulative > std::numeric_limits<std::int32_t>::max()) {
                    throw std::overflow_error("full-K reference accumulator exceeds INT32");
                }
                // Signed-to-unsigned conversion gives the required modulo-2^32
                // representation without relying on signed bitwise operations.
                output.transcript[static_cast<std::size_t>(milestone) * tiles + tile] ^=
                    static_cast<std::uint32_t>(static_cast<std::int32_t>(cumulative));
            }
            output.final_c[static_cast<std::size_t>(row) * cols + col] =
                static_cast<std::int32_t>(cumulative);
        }
    }
    return output;
}

}  // namespace neuro::fullk
