// See reports/iteration-01-validation.json for compilation and execution evidence.
// This is an instruction/layout correctness probe, not a Pearl miner/benchmark.
#include "neuro/mma_layout.hpp"

#include <cuda_runtime.h>

#include <array>
#include <cstdint>
#include <iostream>
#include <sstream>
#include <stdexcept>
#include <string>

namespace layout = neuro::mma16832;

__global__ __launch_bounds__(32)
void mma_checkpoints(const std::int8_t* a, const std::int8_t* b, std::int32_t* output) {
#if defined(__CUDA_ARCH__) && __CUDA_ARCH__ >= 800
    const int lane = static_cast<int>(threadIdx.x);
    std::int32_t d0 = 0, d1 = 0, d2 = 0, d3 = 0;
    // Exactly two collective warp operations; no polling or persistent loop.
    #pragma unroll
    for (int step = 0; step < 2; ++step) {
        const int k_base = step * 32;
        const auto a0 = layout::pack_a(a, lane, 0, k_base);
        const auto a1 = layout::pack_a(a, lane, 1, k_base);
        const auto a2 = layout::pack_a(a, lane, 2, k_base);
        const auto a3 = layout::pack_a(a, lane, 3, k_base);
        const auto b0 = layout::pack_b(b, lane, 0, k_base);
        const auto b1 = layout::pack_b(b, lane, 1, k_base);
        asm volatile(
            "mma.sync.aligned.m16n8k32.row.col.s32.s8.s8.s32 "
            "{%0, %1, %2, %3}, {%4, %5, %6, %7}, {%8, %9}, {%0, %1, %2, %3};\n"
            : "+r"(d0), "+r"(d1), "+r"(d2), "+r"(d3)
            : "r"(a0), "r"(a1), "r"(a2), "r"(a3), "r"(b0), "r"(b1));
        const std::int32_t values[4] = {d0, d1, d2, d3};
        #pragma unroll
        for (int reg = 0; reg < 4; ++reg) {
            const auto p = layout::d_coordinate(lane, reg);
            output[step * layout::rows * layout::cols + p.row * layout::cols + p.col] = values[reg];
        }
    }
#else
    // Never substitute a CPU or scalar-GPU implementation and report MMA PASS.
    asm volatile("trap;");
#endif
}

namespace {

void check(cudaError_t error, const char* operation) {
    if (error != cudaSuccess) {
        throw std::runtime_error(std::string(operation) + ": " + cudaGetErrorString(error));
    }
}

template <class T>
class DeviceBuffer {
public:
    explicit DeviceBuffer(std::size_t count) {
        check(cudaMalloc(reinterpret_cast<void**>(&data_), count * sizeof(T)), "cudaMalloc");
    }
    ~DeviceBuffer() { if (data_) (void)cudaFree(data_); }
    DeviceBuffer(const DeviceBuffer&) = delete;
    DeviceBuffer& operator=(const DeviceBuffer&) = delete;
    T* get() const { return data_; }
    void release() {
        check(cudaFree(data_), "cudaFree");
        data_ = nullptr;
    }
private:
    T* data_ = nullptr;
};

std::string quote_json(const std::string& text) {
    constexpr char hex[] = "0123456789abcdef";
    std::string result = "\"";
    for (unsigned char c : text) {
        if (c == '\"' || c == '\\') {
            result += '\\'; result += static_cast<char>(c);
        } else if (c < 0x20) {
            result += "\\u00"; result += hex[c >> 4]; result += hex[c & 15];
        } else {
            result += static_cast<char>(c);
        }
    }
    return result + '\"';
}

int device_argument(int argc, char** argv) {
    if (argc == 1) return 0;
    if (argc != 3 || std::string(argv[1]) != "--device") {
        throw std::invalid_argument("usage: neuro_mma_probe [--device NONNEGATIVE_INDEX]");
    }
    std::size_t consumed = 0;
    const std::string argument = argv[2];
    const int device = std::stoi(argument, &consumed);
    if (device < 0 || consumed != argument.size()) {
        throw std::invalid_argument("device index must be a nonnegative integer");
    }
    return device;
}

}  // namespace

int main(int argc, char** argv) {
    if (argc == 2 && std::string(argv[1]) == "--help") {
        std::cout << "neuro_mma_probe [--device NONNEGATIVE_INDEX]\n"
                     "Runs four finite one-warp INT8 MMA correctness cases; no mining.\n";
        return 0;
    }
    try {
        const int device = device_argument(argc, argv);
        int count = 0, runtime_version = 0, driver_version = 0;
        check(cudaGetDeviceCount(&count), "cudaGetDeviceCount");
        if (device >= count) throw std::runtime_error("requested CUDA device does not exist");
        cudaDeviceProp properties{};
        check(cudaGetDeviceProperties(&properties, device), "cudaGetDeviceProperties");
        if (properties.major < 8 || properties.warpSize != layout::warp_threads) {
            throw std::runtime_error("probe requires compute capability >= 8.0 and warp size 32");
        }
        check(cudaSetDevice(device), "cudaSetDevice");
        check(cudaRuntimeGetVersion(&runtime_version), "cudaRuntimeGetVersion");
        check(cudaDriverGetVersion(&driver_version), "cudaDriverGetVersion");
        DeviceBuffer<std::int8_t> a(layout::rows * layout::inner);
        DeviceBuffer<std::int8_t> b(layout::inner * layout::cols);
        DeviceBuffer<std::int32_t> output(2 * layout::rows * layout::cols);
        std::array<std::int32_t, 2 * layout::rows * layout::cols> actual{};
        for (int test = 0; test < layout::case_count; ++test) {
            const auto input = layout::make_case(test);
            check(cudaMemcpy(a.get(), input.a.data(), input.a.size(), cudaMemcpyHostToDevice), "copy A");
            check(cudaMemcpy(b.get(), input.b.data(), input.b.size(), cudaMemcpyHostToDevice), "copy B");
            check(cudaMemset(output.get(), 0xa5, actual.size() * sizeof(actual[0])), "initialize output");
            mma_checkpoints<<<1, layout::warp_threads>>>(a.get(), b.get(), output.get());
            check(cudaGetLastError(), "launch mma_checkpoints");
            check(cudaDeviceSynchronize(), "synchronize mma_checkpoints");
            check(cudaMemcpy(actual.data(), output.get(), actual.size() * sizeof(actual[0]),
                             cudaMemcpyDeviceToHost), "copy checkpoints");
            for (int step = 0; step < 2; ++step) {
                const auto expected = layout::reference(input, (step + 1) * 32);
                for (int i = 0; i < layout::rows * layout::cols; ++i) {
                    const auto got = actual[step * layout::rows * layout::cols + i];
                    if (got != expected[i]) {
                        std::ostringstream error;
                        error << layout::case_name(test) << ": K=" << (step + 1) * 32
                              << " row=" << i / layout::cols << " col=" << i % layout::cols
                              << " expected=" << expected[i] << " actual=" << got;
                        throw std::runtime_error(error.str());
                    }
                }
            }
        }
        output.release(); b.release(); a.release();
        std::cout << "{\"schema\":\"neuro.gpu_instruction_probe.v1\","
                     "\"kind\":\"gpu_instruction_probe\",\"status\":\"PASS\",\"exact\":true,"
                     "\"gpu_execution_observed\":true,\"mining_evidence\":false,\"hashrate\":null,"
                     "\"device_index\":" << device << ",\"device_name\":" << quote_json(properties.name)
                  << ",\"compute_capability\":\"" << properties.major << '.' << properties.minor
                  << "\",\"runtime_version\":" << runtime_version << ",\"driver_version\":" << driver_version
                  << ",\"shape_mnk\":[16,8,64],\"checkpoints_k\":[32,64],\"cases\":["
                     "\"zeros\",\"mixed_signed\",\"int7_boundaries\",\"int8_boundaries\"],"
                     "\"checked_values\":1024,\"kernel_launches\":4,\"threads_per_block\":32}\n";
        return 0;
    } catch (const std::exception& error) {
        std::cout << "{\"schema\":\"neuro.gpu_instruction_probe.v1\","
                     "\"kind\":\"gpu_instruction_probe\",\"status\":\"FAIL\",\"exact\":false,"
                     "\"mining_evidence\":false,\"hashrate\":null,\"error\":"
                  << quote_json(error.what()) << "}\n";
        return 1;
    }
}
