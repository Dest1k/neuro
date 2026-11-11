#pragma once

#include <array>
#include <cstdint>
#include <stdexcept>

#if defined(__CUDACC__)
#define NEURO_MMA_HD __host__ __device__
#else
#define NEURO_MMA_HD
#endif

namespace neuro::mma16832 {

inline constexpr int rows = 16;
inline constexpr int cols = 8;
inline constexpr int inner = 64;
inline constexpr int warp_threads = 32;
inline constexpr int case_count = 4;

struct Coordinate { int row; int col; };

// NVIDIA PTX ISA 9.5, section 9.7.16.5.10, signed eight-bit operands:
// https://docs.nvidia.com/cuda/parallel-thread-execution/#warp-level-matrix-fragment-mma-16832
// `word` is a 32-bit register; `byte` selects its least-significant-first byte.
NEURO_MMA_HD constexpr Coordinate a_coordinate(int lane, int word, int byte, int k_base = 0) {
    return {lane / 4 + (word % 2) * 8,
            k_base + (lane % 4) * 4 + (word / 2) * 16 + byte};
}

NEURO_MMA_HD constexpr Coordinate b_coordinate(int lane, int word, int byte, int k_base = 0) {
    return {k_base + (lane % 4) * 4 + word * 16 + byte, lane / 4};
}

NEURO_MMA_HD constexpr Coordinate d_coordinate(int lane, int reg) {
    return {lane / 4 + (reg / 2) * 8, (lane % 4) * 2 + reg % 2};
}

// Both host matrices have ordinary row-major storage. The fragment packing,
// rather than a raw pointer reinterpretation, implements MMA's row.col layout.
NEURO_MMA_HD inline std::uint32_t pack_a(const std::int8_t* a, int lane, int word, int k_base) {
    std::uint32_t packed = 0;
    for (int byte = 0; byte < 4; ++byte) {
        const auto p = a_coordinate(lane, word, byte, k_base);
        packed |= std::uint32_t(static_cast<std::uint8_t>(a[p.row * inner + p.col])) << (8 * byte);
    }
    return packed;
}

NEURO_MMA_HD inline std::uint32_t pack_b(const std::int8_t* b, int lane, int word, int k_base) {
    std::uint32_t packed = 0;
    for (int byte = 0; byte < 4; ++byte) {
        const auto p = b_coordinate(lane, word, byte, k_base);
        packed |= std::uint32_t(static_cast<std::uint8_t>(b[p.row * cols + p.col])) << (8 * byte);
    }
    return packed;
}

NEURO_MMA_HD constexpr int signed_byte(std::uint32_t packed, int byte) {
    const int value = static_cast<int>((packed >> (8 * byte)) & 0xffu);
    return value < 128 ? value : value - 256;
}

struct Input {
    std::array<std::int8_t, rows * inner> a{};
    std::array<std::int8_t, inner * cols> b{};
};

inline const char* case_name(int index) {
    switch (index) {
        case 0: return "zeros";
        case 1: return "mixed_signed";
        case 2: return "int7_boundaries";
        case 3: return "int8_boundaries";
        default: throw std::invalid_argument("unknown probe case");
    }
}

// Deliberately asymmetric row/column and K-half patterns expose swaps and a
// forgotten second accumulation. Full INT8 endpoints exercise sign extension.
inline Input make_case(int index) {
    (void)case_name(index);
    Input input;
    const int lo = index == 2 ? -64 : -128;
    const int hi = index == 2 ? 63 : 127;
    const int edges[8] = {lo, hi, lo, 0, hi, -1, 1, hi};
    for (int r = 0; r < rows; ++r) {
        for (int k = 0; k < inner; ++k) {
            const int value = index == 0 ? 0 : index == 1
                ? (r * 17 + k * 11 + (k / 32) * 7) % 63 - 31
                : edges[(r * 5 + k * 3 + k / 32) % 8];
            input.a[r * inner + k] = static_cast<std::int8_t>(value);
        }
    }
    for (int k = 0; k < inner; ++k) {
        for (int c = 0; c < cols; ++c) {
            const int value = index == 0 ? 0 : index == 1
                ? (k * 13 + c * 19 + (k / 32) * 3) % 61 - 30
                : edges[(k * 7 + c * 3 + (k / 32) * 2) % 8];
            input.b[k * cols + c] = static_cast<std::int8_t>(value);
        }
    }
    return input;
}

// Independent row-major oracle: no fragment coordinates or packing helpers.
// 64 * 128 * 128 = 1,048,576, so these inputs cannot overflow an INT32 sum.
inline std::array<std::int32_t, rows * cols> reference(const Input& input, int checkpoint) {
    if (checkpoint != 32 && checkpoint != 64) {
        throw std::invalid_argument("checkpoint must be 32 or 64");
    }
    std::array<std::int32_t, rows * cols> result{};
    for (int r = 0; r < rows; ++r) {
        for (int c = 0; c < cols; ++c) {
            std::int64_t sum = 0;
            for (int k = 0; k < checkpoint; ++k) {
                sum += std::int64_t(input.a[r * inner + k]) * input.b[k * cols + c];
            }
            result[r * cols + c] = static_cast<std::int32_t>(sum);
        }
    }
    return result;
}

}  // namespace neuro::mma16832

#undef NEURO_MMA_HD
