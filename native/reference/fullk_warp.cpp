#include "neuro/fullk.hpp"
#include "neuro/mma_layout.hpp"

#include <array>

namespace neuro::fullk {
namespace {

std::uint32_t pack_four(const std::int8_t* values) {
    std::uint32_t word = 0;
    for (int i = 0; i < 4; ++i) {
        word |= std::uint32_t(static_cast<std::uint8_t>(values[i])) << (8 * i);
    }
    return word;
}

using Accumulators = std::array<std::array<std::int32_t, 4>, 32>;
using ARegisters = std::array<std::array<std::uint32_t, 4>, 32>;
using BRegisters = std::array<std::array<std::uint32_t, 2>, 32>;

// Model MMA register semantics through independent inverse lookup equations,
// not direct dot products over the original matrices. Packing and scattering
// use the same documented coordinates as the CUDA kernel.
void emulate_mma(Accumulators& d, const ARegisters& a, const BRegisters& b) {
    for (int row = 0; row < 16; ++row) {
        for (int col = 0; col < 8; ++col) {
            std::int32_t sum = 0;
            for (int k = 0; k < 32; ++k) {
                const int a_lane = (row % 8) * 4 + (k % 16) / 4;
                const int a_word = row / 8 + 2 * (k / 16);
                const int b_lane = col * 4 + (k % 16) / 4;
                sum += mma16832::signed_byte(a[a_lane][a_word], k % 4)
                     * mma16832::signed_byte(b[b_lane][k / 16], k % 4);
            }
            d[(row % 8) * 4 + col / 2][2 * (row / 8) + col % 2] += sum;
        }
    }
}

std::uint32_t emulate_xor(std::array<std::uint32_t, 32> lanes) {
    for (int delta = 16; delta > 0; delta /= 2) {
        const auto previous = lanes; // shfl reads all lanes before the assignment.
        for (int lane = 0; lane < 32; ++lane) {
            lanes[lane] = previous[lane] ^ previous[lane ^ delta];
        }
    }
    return lanes[0];
}

} // namespace

Result emulate_warp(const Input& input) {
    validate_shape(input.shape);
    if (input.a.size() != std::size_t(input.shape.rows) * inner
        || input.bt.size() != std::size_t(input.shape.cols) * inner) {
        throw std::invalid_argument("full-K input matrix sizes do not match the shape");
    }
    const Shape shape = input.shape;
    const int tile_cols = shape.cols / hash_cols;
    const std::size_t tiles = tile_count(shape);
    Result result;
    result.transcript.resize(transcript_words(shape));
    result.final_c.resize(std::size_t(shape.rows) * shape.cols);

    for (int row_base = 0; row_base < shape.rows; row_base += 16) {
        for (int col_base = 0; col_base < shape.cols; col_base += 16) {
            Accumulators left{}, right{};
            const int upper_tile = (row_base / hash_rows) * tile_cols + col_base / hash_cols;
            for (int milestone = 0; milestone < milestones; ++milestone) {
                for (int segment = 0; segment < rank / 32; ++segment) {
                    const int k_base = milestone * rank + segment * 32;
                    ARegisters a;
                    BRegisters b_left, b_right;
                    for (int lane = 0; lane < 32; ++lane) {
                        for (int word = 0; word < 4; ++word) {
                            const auto p = mma16832::a_coordinate(lane, word, 0, k_base);
                            a[lane][word] = pack_four(input.a.data() + (row_base + p.row) * inner + p.col);
                            if (word < 2) {
                                const auto q = mma16832::b_coordinate(lane, word, 0, k_base);
                                b_left[lane][word] = pack_four(input.bt.data() + (col_base + q.col) * inner + q.row);
                                b_right[lane][word] = pack_four(input.bt.data() + (col_base + 8 + q.col) * inner + q.row);
                            }
                        }
                    }
                    emulate_mma(left, a, b_left);
                    emulate_mma(right, a, b_right);
                }
                std::array<std::uint32_t, 32> upper, lower;
                for (int lane = 0; lane < 32; ++lane) {
                    upper[lane] = std::uint32_t(left[lane][0]) ^ std::uint32_t(left[lane][1])
                                ^ std::uint32_t(right[lane][0]) ^ std::uint32_t(right[lane][1]);
                    lower[lane] = std::uint32_t(left[lane][2]) ^ std::uint32_t(left[lane][3])
                                ^ std::uint32_t(right[lane][2]) ^ std::uint32_t(right[lane][3]);
                }
                result.transcript[milestone * tiles + upper_tile] = emulate_xor(upper);
                result.transcript[milestone * tiles + upper_tile + tile_cols] = emulate_xor(lower);
            }
            for (int lane = 0; lane < 32; ++lane) {
                for (int reg = 0; reg < 4; ++reg) {
                    const auto p = mma16832::d_coordinate(lane, reg);
                    const int index = (row_base + p.row) * shape.cols + col_base + p.col;
                    result.final_c[index] = left[lane][reg];
                    result.final_c[index + 8] = right[lane][reg];
                }
            }
        }
    }
    return result;
}

} // namespace neuro::fullk
