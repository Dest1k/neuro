#include "neuro/fullk_cuda.cuh"
#include "neuro/mma_layout.hpp"

#include <cstdint>
#include <cuda_runtime.h>

namespace neuro::fullk {
namespace {

// Pack bytes explicitly: no alignment, strict-aliasing, or host-endianness
// assumptions are added to the input contract. K coordinates are contiguous
// for both A and the transposed storage Bt.
__device__ __forceinline__ std::uint32_t pack_four(const std::int8_t* values) {
    std::uint32_t word = 0;
    #pragma unroll
    for (int i = 0; i < 4; ++i) {
        word |= std::uint32_t(static_cast<std::uint8_t>(values[i])) << (8 * i);
    }
    return word;
}

// Staged panels are 16-byte aligned and every fragment coordinate starts on a
// 4-byte K boundary. One little-endian word matches pack_four.
__device__ __forceinline__ std::uint32_t load_shared_u32(const std::int8_t* values) {
    const unsigned address = static_cast<unsigned>(__cvta_generic_to_shared(values));
    std::uint32_t word;
    asm volatile("ld.shared.b32 %0, [%1];\n" : "=r"(word) : "r"(address));
    return word;
}

__device__ __forceinline__ void mma_16832(
    std::int32_t (&d)[4], const std::uint32_t (&a)[4], const std::uint32_t (&b)[2]) {
#if defined(__CUDA_ARCH__) && __CUDA_ARCH__ >= 800
    asm volatile(
        "mma.sync.aligned.m16n8k32.row.col.s32.s8.s8.s32 "
        "{%0, %1, %2, %3}, {%4, %5, %6, %7}, {%8, %9}, {%0, %1, %2, %3};\n"
        : "+r"(d[0]), "+r"(d[1]), "+r"(d[2]), "+r"(d[3])
        : "r"(a[0]), "r"(a[1]), "r"(a[2]), "r"(a[3]), "r"(b[0]), "r"(b[1]));
#else
    asm volatile("trap;");
#endif
}

// One complete warp owns a 16x16 output region. This is a finite correctness
// kernel: 32 milestones, four K32 steps per milestone, two N8 MMAs per step.
// Accumulators intentionally remain live across every K32 step and milestone.
__global__ __launch_bounds__(32)
void fullk_kernel(const std::int8_t* a, const std::int8_t* bt,
                 std::uint32_t* transcript, std::int32_t* final_c, Shape shape) {
#if defined(__CUDA_ARCH__) && __CUDA_ARCH__ >= 800
    const int lane = static_cast<int>(threadIdx.x);
    const int row_base = static_cast<int>(blockIdx.y) * 16;
    const int col_base = static_cast<int>(blockIdx.x) * 16;
    const int tile_cols = shape.cols / hash_cols;
    const int tiles = (shape.rows / hash_rows) * tile_cols;
    const int upper_tile = (row_base / hash_rows) * tile_cols + col_base / hash_cols;
    std::int32_t left[4] = {}, right[4] = {};

    #pragma unroll 1
    for (int milestone = 0; milestone < milestones; ++milestone) {
        #pragma unroll
        for (int segment = 0; segment < rank / 32; ++segment) {
            const int k_base = milestone * rank + segment * 32;
            std::uint32_t a_frag[4], b_left[2], b_right[2];
            #pragma unroll
            for (int word = 0; word < 4; ++word) {
                const auto p = mma16832::a_coordinate(lane, word, 0, k_base);
                a_frag[word] = pack_four(a + (row_base + p.row) * inner + p.col);
            }
            #pragma unroll
            for (int word = 0; word < 2; ++word) {
                const auto p = mma16832::b_coordinate(lane, word, 0, k_base);
                b_left[word] = pack_four(bt + (col_base + p.col) * inner + p.row);
                b_right[word] = pack_four(bt + (col_base + 8 + p.col) * inner + p.row);
            }
            mma_16832(left, a_frag, b_left);
            mma_16832(right, a_frag, b_right);
        }

        // Each lane contributes four values from the upper and lower 8x16
        // regions. Full-mask XOR butterflies cover all 128 values in each tile.
        std::uint32_t upper = std::uint32_t(left[0]) ^ std::uint32_t(left[1])
                            ^ std::uint32_t(right[0]) ^ std::uint32_t(right[1]);
        std::uint32_t lower = std::uint32_t(left[2]) ^ std::uint32_t(left[3])
                            ^ std::uint32_t(right[2]) ^ std::uint32_t(right[3]);
        #pragma unroll
        for (int delta = 16; delta > 0; delta /= 2) {
            upper ^= __shfl_xor_sync(0xffffffffu, upper, delta);
            lower ^= __shfl_xor_sync(0xffffffffu, lower, delta);
        }
        if (lane == 0) {
            transcript[milestone * tiles + upper_tile] = upper;
            transcript[milestone * tiles + upper_tile + tile_cols] = lower;
        }
    }

    if (final_c) {
        #pragma unroll
        for (int reg = 0; reg < 4; ++reg) {
            const auto p = mma16832::d_coordinate(lane, reg);
            final_c[(row_base + p.row) * shape.cols + col_base + p.col] = left[reg];
            final_c[(row_base + p.row) * shape.cols + col_base + 8 + p.col] = right[reg];
        }
    }
#else
    asm volatile("trap;");
#endif
}

// Shapes that are multiples of 64 stage one milestone (K=128) of a 64x64
// output tile in shared memory, then run the same per-warp MMA and transcript
// mapping. A and Bt are otherwise reread from global memory once per 16-wide
// neighbor. This is not a persistent kernel and it does not change the launch
// contract. Other shapes keep the direct kernel, including the 32x48 gold.
constexpr int kStageRows = 64;
constexpr int kStageCols = 64;
constexpr int kStageK = rank;
// 128 payload bytes plus 16 of padding. A 128-byte stride lands every row on
// the same shared bank; 144 shifts successive rows by four banks.
constexpr int kStageStride = kStageK + 16;
constexpr int kStageThreads = (kStageRows / 16) * (kStageCols / 16) * 32;
static_assert(kStageThreads == 512, "staged full-K block covers one uint4 per thread");
static_assert(kStageStride % 16 == 0, "staged rows stay 16-byte aligned");

__device__ __forceinline__ void stage_aligned_panel(
    std::int8_t* tile, const std::int8_t* matrix, int origin, int k_base) {
    constexpr int bytes = 16;
    const int packed = static_cast<int>(threadIdx.x) * bytes;
    const int row = packed / kStageK;
    const int col = packed % kStageK;
    const std::int8_t* src = matrix + (static_cast<std::size_t>(origin + row) * inner
                                        + static_cast<std::size_t>(k_base + col));
    *reinterpret_cast<uint4*>(tile + row * kStageStride + col) = *reinterpret_cast<const uint4*>(src);
}

__global__ __launch_bounds__(512)
void fullk_kernel_staged(const std::int8_t* a, const std::int8_t* bt,
                         std::uint32_t* transcript, std::int32_t* final_c, Shape shape) {
#if defined(__CUDA_ARCH__) && __CUDA_ARCH__ >= 800
    constexpr int warps_per_side = kStageRows / 16;
    const int lane = static_cast<int>(threadIdx.x) & 31;
    const int warp = static_cast<int>(threadIdx.x) >> 5;
    const int warp_row = warp / warps_per_side;
    const int warp_col = warp % warps_per_side;
    const int block_row = static_cast<int>(blockIdx.y) * kStageRows;
    const int block_col = static_cast<int>(blockIdx.x) * kStageCols;
    const int row_base = block_row + warp_row * 16;
    const int col_base = block_col + warp_col * 16;
    const int tile_cols = shape.cols / hash_cols;
    const int tiles = (shape.rows / hash_rows) * tile_cols;
    const int upper_tile = (row_base / hash_rows) * tile_cols + col_base / hash_cols;
    __shared__ __align__(16) std::int8_t stage_a[kStageRows * kStageStride];
    __shared__ __align__(16) std::int8_t stage_b[kStageCols * kStageStride];
    std::int32_t left[4] = {}, right[4] = {};

    #pragma unroll 1
    for (int milestone = 0; milestone < milestones; ++milestone) {
        const int k_base = milestone * rank;
        stage_aligned_panel(stage_a, a, block_row, k_base);
        stage_aligned_panel(stage_b, bt, block_col, k_base);
        __syncthreads();
        #pragma unroll
        for (int segment = 0; segment < rank / 32; ++segment) {
            const int segment_k = k_base + segment * 32;
            std::uint32_t a_frag[4], b_left[2], b_right[2];
            #pragma unroll
            for (int word = 0; word < 4; ++word) {
                const auto p = mma16832::a_coordinate(lane, word, 0, segment_k);
                const int local_row = (row_base - block_row) + p.row;
                const int local_k = p.col - k_base;
                a_frag[word] = load_shared_u32(stage_a + local_row * kStageStride + local_k);
            }
            #pragma unroll
            for (int word = 0; word < 2; ++word) {
                const auto p = mma16832::b_coordinate(lane, word, 0, segment_k);
                const int local_k = p.row - k_base;
                const int local_col = (col_base - block_col) + p.col;
                b_left[word] = load_shared_u32(stage_b + local_col * kStageStride + local_k);
                b_right[word] = load_shared_u32(stage_b + (local_col + 8) * kStageStride + local_k);
            }
            mma_16832(left, a_frag, b_left);
            mma_16832(right, a_frag, b_right);
        }
        std::uint32_t upper = std::uint32_t(left[0]) ^ std::uint32_t(left[1])
                            ^ std::uint32_t(right[0]) ^ std::uint32_t(right[1]);
        std::uint32_t lower = std::uint32_t(left[2]) ^ std::uint32_t(left[3])
                            ^ std::uint32_t(right[2]) ^ std::uint32_t(right[3]);
        #pragma unroll
        for (int delta = 16; delta > 0; delta /= 2) {
            upper ^= __shfl_xor_sync(0xffffffffu, upper, delta);
            lower ^= __shfl_xor_sync(0xffffffffu, lower, delta);
        }
        if (lane == 0) {
            transcript[milestone * tiles + upper_tile] = upper;
            transcript[milestone * tiles + upper_tile + tile_cols] = lower;
        }
        __syncthreads();
    }

    if (final_c) {
        #pragma unroll
        for (int reg = 0; reg < 4; ++reg) {
            const auto p = mma16832::d_coordinate(lane, reg);
            final_c[(row_base + p.row) * shape.cols + col_base + p.col] = left[reg];
            final_c[(row_base + p.row) * shape.cols + col_base + 8 + p.col] = right[reg];
        }
    }
#else
    asm volatile("trap;");
#endif
}

// Shapes that are multiples of 128 use a wider 128x128 output tile. The same
// 512-thread block stages 128 K values for both panels once per milestone, but
// each warp owns a 32x32 region (four 16x16 MMA tiles). Doubling the tile in
// both dimensions halves the global/L2 traffic per output element: every A row
// is reread by 8 instead of 16 block columns, and every Bt column by 8 instead
// of 16 block rows. Fragment mapping, transcript words and final C stay the
// same as the 64x64 staged kernel.
constexpr int kStage128Rows = 128;
constexpr int kStage128Cols = 128;
constexpr int kStage128Threads = (kStage128Rows / 32) * (kStage128Cols / 32) * 32;
static_assert(kStage128Threads == 512, "wide staged block covers 32 bytes per thread");
static_assert(kStage128Rows * kStage128Cols == kStage128Threads * 32,
              "wide staged panel stages 32 bytes per thread per matrix");

__device__ __forceinline__ void stage_aligned_panel128(
    std::int8_t* tile, const std::int8_t* matrix, int origin, int k_base) {
    constexpr int bytes = 32;
    const int packed = static_cast<int>(threadIdx.x) * bytes;
    const int row = packed / kStageK;
    const int col = packed % kStageK;
    const std::int8_t* src = matrix + (static_cast<std::size_t>(origin + row) * inner
                                        + static_cast<std::size_t>(k_base + col));
    *reinterpret_cast<uint4*>(tile + row * kStageStride + col) = *reinterpret_cast<const uint4*>(src);
    *reinterpret_cast<uint4*>(tile + row * kStageStride + col + 16) =
        *reinterpret_cast<const uint4*>(src + 16);
}

__global__ __launch_bounds__(kStage128Threads)
void fullk_kernel_staged128(const std::int8_t* a, const std::int8_t* bt,
                            std::uint32_t* transcript, std::int32_t* final_c, Shape shape) {
#if defined(__CUDA_ARCH__) && __CUDA_ARCH__ >= 800
    constexpr int warps_per_side = 4;
    constexpr int region_rows = 32;
    constexpr int region_cols = 32;
    const int lane = static_cast<int>(threadIdx.x) & 31;
    const int warp = static_cast<int>(threadIdx.x) >> 5;
    const int warp_row = warp / warps_per_side;
    const int warp_col = warp % warps_per_side;
    const int block_row = static_cast<int>(blockIdx.y) * kStage128Rows;
    const int block_col = static_cast<int>(blockIdx.x) * kStage128Cols;
    const int row_base = block_row + warp_row * region_rows;
    const int col_base = block_col + warp_col * region_cols;
    const int tile_cols = shape.cols / hash_cols;
    const int tiles = (shape.rows / hash_rows) * tile_cols;
    __shared__ __align__(16) std::int8_t stage_a[kStage128Rows * kStageStride];
    __shared__ __align__(16) std::int8_t stage_b[kStage128Cols * kStageStride];
    std::int32_t acc[2][2][2][4] = {};

    #pragma unroll 1
    for (int milestone = 0; milestone < milestones; ++milestone) {
        const int k_base = milestone * rank;
        stage_aligned_panel128(stage_a, a, block_row, k_base);
        stage_aligned_panel128(stage_b, bt, block_col, k_base);
        __syncthreads();
        #pragma unroll
        for (int segment = 0; segment < rank / 32; ++segment) {
            const int segment_k = k_base + segment * 32;
            std::uint32_t a_frag[2][4];
            #pragma unroll
            for (int group = 0; group < 2; ++group) {
                #pragma unroll
                for (int word = 0; word < 4; ++word) {
                    const auto p = mma16832::a_coordinate(lane, word, 0, segment_k);
                    const int local_row = (row_base + group * 16 - block_row) + p.row;
                    const int local_k = p.col - k_base;
                    a_frag[group][word] = load_shared_u32(stage_a + local_row * kStageStride + local_k);
                }
            }
            std::uint32_t b_left[2][2], b_right[2][2];
            #pragma unroll
            for (int group = 0; group < 2; ++group) {
                #pragma unroll
                for (int word = 0; word < 2; ++word) {
                    const auto p = mma16832::b_coordinate(lane, word, 0, segment_k);
                    const int local_k = p.row - k_base;
                    const int local_col = (col_base + group * 16 - block_col) + p.col;
                    b_left[group][word] = load_shared_u32(stage_b + local_col * kStageStride + local_k);
                    b_right[group][word] = load_shared_u32(stage_b + (local_col + 8) * kStageStride + local_k);
                }
            }
            #pragma unroll
            for (int row_group = 0; row_group < 2; ++row_group) {
                #pragma unroll
                for (int col_group = 0; col_group < 2; ++col_group) {
                    mma_16832(acc[row_group][col_group][0], a_frag[row_group], b_left[col_group]);
                    mma_16832(acc[row_group][col_group][1], a_frag[row_group], b_right[col_group]);
                }
            }
        }
        #pragma unroll
        for (int row_group = 0; row_group < 2; ++row_group) {
            #pragma unroll
            for (int col_group = 0; col_group < 2; ++col_group) {
                std::uint32_t upper = std::uint32_t(acc[row_group][col_group][0][0])
                                    ^ std::uint32_t(acc[row_group][col_group][0][1])
                                    ^ std::uint32_t(acc[row_group][col_group][1][0])
                                    ^ std::uint32_t(acc[row_group][col_group][1][1]);
                std::uint32_t lower = std::uint32_t(acc[row_group][col_group][0][2])
                                    ^ std::uint32_t(acc[row_group][col_group][0][3])
                                    ^ std::uint32_t(acc[row_group][col_group][1][2])
                                    ^ std::uint32_t(acc[row_group][col_group][1][3]);
                #pragma unroll
                for (int delta = 16; delta > 0; delta /= 2) {
                    upper ^= __shfl_xor_sync(0xffffffffu, upper, delta);
                    lower ^= __shfl_xor_sync(0xffffffffu, lower, delta);
                }
                if (lane == 0) {
                    const int region_row = row_base + row_group * 16;
                    const int region_col = col_base + col_group * 16;
                    const int index = milestone * tiles
                                    + (region_row / hash_rows) * tile_cols + region_col / hash_cols;
                    transcript[index] = upper;
                    transcript[index + tile_cols] = lower;
                }
            }
        }
        __syncthreads();
    }

    if (final_c) {
        #pragma unroll
        for (int row_group = 0; row_group < 2; ++row_group) {
            #pragma unroll
            for (int col_group = 0; col_group < 2; ++col_group) {
                #pragma unroll
                for (int reg = 0; reg < 4; ++reg) {
                    const auto p = mma16832::d_coordinate(lane, reg);
                    const int row = row_base + row_group * 16 + p.row;
                    const int col = col_base + col_group * 16 + p.col;
                    final_c[row * shape.cols + col] = acc[row_group][col_group][0][reg];
                    final_c[row * shape.cols + col + 8] = acc[row_group][col_group][1][reg];
                }
            }
        }
    }
#else
    asm volatile("trap;");
#endif
}

} // namespace

cudaError_t launch_fullk(const std::int8_t* a, const std::int8_t* bt,
                        std::uint32_t* transcript, std::int32_t* final_c,
                        Shape shape, cudaStream_t stream) {
    if (!a || !bt || !transcript || shape.rows < 16 || shape.cols < 16
        || shape.rows > max_dimension || shape.cols > max_dimension
        || shape.rows % 16 != 0 || shape.cols % 16 != 0) {
        return cudaErrorInvalidValue;
    }
    const bool aligned = reinterpret_cast<std::uintptr_t>(a) % 16 == 0
                      && reinterpret_cast<std::uintptr_t>(bt) % 16 == 0;
    if (aligned && shape.rows % kStage128Rows == 0 && shape.cols % kStage128Cols == 0) {
        const dim3 grid(static_cast<unsigned>(shape.cols / kStage128Cols),
                        static_cast<unsigned>(shape.rows / kStage128Rows));
        fullk_kernel_staged128<<<grid, kStage128Threads, 0, stream>>>(a, bt, transcript, final_c, shape);
        return cudaGetLastError();
    }
    if (aligned && shape.rows % kStageRows == 0 && shape.cols % kStageCols == 0) {
        const dim3 grid(static_cast<unsigned>(shape.cols / kStageCols),
                        static_cast<unsigned>(shape.rows / kStageRows));
        fullk_kernel_staged<<<grid, kStageThreads, 0, stream>>>(a, bt, transcript, final_c, shape);
        return cudaGetLastError();
    }
    const dim3 grid(static_cast<unsigned>(shape.cols / 16),
                    static_cast<unsigned>(shape.rows / 16));
    fullk_kernel<<<grid, 32, 0, stream>>>(a, bt, transcript, final_c, shape);
    return cudaGetLastError();
}

} // namespace neuro::fullk
