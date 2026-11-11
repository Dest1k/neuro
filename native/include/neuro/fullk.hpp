#pragma once

#include <cstddef>
#include <cstdint>
#include <stdexcept>
#include <vector>

namespace neuro::fullk {

inline constexpr int inner = 4096;
inline constexpr int rank = 128;
inline constexpr int milestones = inner / rank;
inline constexpr int hash_rows = 8;
inline constexpr int hash_cols = 16;
// This correctness-first launch is bounded. Large mining panels need a later
// scheduling/staging design; accepting arbitrary dimensions is not that design.
inline constexpr int max_dimension = 1024;

struct Shape { int rows; int cols; };

inline void validate_shape(Shape shape) {
    if (shape.rows < 16 || shape.cols < 16 || shape.rows % 16 || shape.cols % 16 ||
        shape.rows > max_dimension || shape.cols > max_dimension) {
        throw std::invalid_argument("rows and cols must be multiples of 16 in [16, 1024]");
    }
}

inline std::size_t tile_count(Shape shape) {
    validate_shape(shape);
    return std::size_t(shape.rows / hash_rows) * (shape.cols / hash_cols);
}

inline std::size_t transcript_words(Shape shape) {
    return std::size_t(milestones) * tile_count(shape);
}

enum class Pattern : int {
    zeros = 0, mixed_signed = 1, int7_edges = 2, int8_edges = 3,
    milestone_edges = 4, coordinate_mix = 5
};

struct Input {
    Shape shape;
    std::vector<std::int8_t> a;  // row-major [rows][4096]
    std::vector<std::int8_t> bt; // row-major [cols][4096]: B is transposed
};

struct Result {
    // milestone-major, then tile row, then tile column. Each word is the XOR
    // of the uint32 bit representations of 8x16 cumulative INT32 values.
    std::vector<std::uint32_t> transcript;
    std::vector<std::int32_t> final_c; // row-major [rows][cols]
};

// Fixture data only, not Pearl matrix/noise generation or a cryptographic hash.
Input make_input(Shape shape, Pattern pattern, std::uint32_t seed = 0);

// Independent dense scalar oracle, using INT64 intermediates. It does not use
// GPU fragment coordinates, packing, warp reductions, or candidate output.
Result reference(const Input& input);

// CPU emulation of the full warp/register calculation, implemented separately.
// Its successful execution is not evidence that a CUDA kernel ran.
Result emulate_warp(const Input& input);

} // namespace neuro::fullk
