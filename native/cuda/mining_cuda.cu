// SPDX-License-Identifier: MIT
#include "neuro/mining_cuda.hpp"
#include "neuro/fullk_cuda.cuh"

#include <cuda_runtime.h>

#include <cstddef>
#include <cstdint>
#include <mutex>
#include <stdexcept>
#include <string>

namespace neuro::host {
namespace {

void check(cudaError_t result, const char* operation) {
    if (result != cudaSuccess)
        throw std::runtime_error(std::string(operation) + ": " + cudaGetErrorString(result));
}

// One resident shape for this process. Attempts of the same shape reuse the
// stream and device buffers; a different shape or device releases them first.
// The allocation is intentionally not freed at shutdown: a static destructor
// would call into the CUDA runtime while it is already tearing down.
struct Resident {
    int device = -1;
    int rows = 0;
    int cols = 0;
    cudaStream_t stream = nullptr;
    std::int8_t* a = nullptr;
    std::int8_t* bt = nullptr;
    std::uint32_t* transcript = nullptr;
    std::int32_t* final_c = nullptr;
};

void release(Resident& item) noexcept {
    if (item.a) cudaFree(item.a);
    if (item.bt) cudaFree(item.bt);
    if (item.transcript) cudaFree(item.transcript);
    if (item.final_c) cudaFree(item.final_c);
    if (item.stream) cudaStreamDestroy(item.stream);
    item = {};
}

struct Cache {
    std::mutex mutex;
    Resident resident;
    int checked_device = -1;
};

Cache& cache() {
    static auto* instance = new Cache();
    return *instance;
}

void ensure_resident(Resident& item, int device, const fullk::Shape& shape,
                     std::size_t a_count, std::size_t bt_count,
                     std::size_t transcript_count, std::size_t final_count) {
    if (item.device == device && item.rows == shape.rows && item.cols == shape.cols) return;
    release(item);
    check(cudaStreamCreateWithFlags(&item.stream, cudaStreamNonBlocking), "cudaStreamCreateWithFlags");
    if (cudaMalloc(reinterpret_cast<void**>(&item.a), a_count) != cudaSuccess
        || cudaMalloc(reinterpret_cast<void**>(&item.bt), bt_count) != cudaSuccess
        || cudaMalloc(reinterpret_cast<void**>(&item.transcript),
                      transcript_count * sizeof(std::uint32_t)) != cudaSuccess
        || cudaMalloc(reinterpret_cast<void**>(&item.final_c),
                      final_count * sizeof(std::int32_t)) != cudaSuccess) {
        release(item);
        throw std::runtime_error("cudaMalloc: device buffer for the mining callback was not allocated");
    }
    item.device = device;
    item.rows = shape.rows;
    item.cols = shape.cols;
}

} // namespace

fullk::Result compute_cuda(const fullk::Input& input, int device) {
    fullk::validate_shape(input.shape);
    if (device < 0) throw std::invalid_argument("CUDA device index must be non-negative");
    const auto a_count = static_cast<std::size_t>(input.shape.rows) * fullk::inner;
    const auto bt_count = static_cast<std::size_t>(input.shape.cols) * fullk::inner;
    if (input.a.size() != a_count || input.bt.size() != bt_count)
        throw std::invalid_argument("CUDA backend input lengths do not match the bounded shape");

    std::lock_guard<std::mutex> lock(cache().mutex);
    check(cudaSetDevice(device), "cudaSetDevice");
    if (cache().checked_device != device) {
        cudaDeviceProp properties{};
        check(cudaGetDeviceProperties(&properties, device), "cudaGetDeviceProperties");
        if (properties.major != 12 || properties.minor != 0 || properties.warpSize != 32)
            throw std::runtime_error("The recorded mining CUDA build targets SM120 with 32-thread warps");
        cache().checked_device = device;
    }

    fullk::Result output;
    output.transcript.resize(fullk::transcript_words(input.shape));
    output.final_c.resize(static_cast<std::size_t>(input.shape.rows) * input.shape.cols);
    ensure_resident(cache().resident, device, input.shape, a_count, bt_count,
                    output.transcript.size(), output.final_c.size());
    Resident& resident = cache().resident;
    check(cudaMemcpyAsync(resident.a, input.a.data(), a_count, cudaMemcpyHostToDevice, resident.stream),
          "upload noisy A");
    check(cudaMemcpyAsync(resident.bt, input.bt.data(), bt_count, cudaMemcpyHostToDevice, resident.stream),
          "upload noisy Bt");
    check(fullk::launch_fullk(resident.a, resident.bt, resident.transcript, resident.final_c,
                              input.shape, resident.stream),
          "launch full-K kernel");
    check(cudaMemcpyAsync(output.transcript.data(), resident.transcript, output.transcript.size() * sizeof(std::uint32_t),
                          cudaMemcpyDeviceToHost, resident.stream),
          "download transcript");
    check(cudaMemcpyAsync(output.final_c.data(), resident.final_c, output.final_c.size() * sizeof(std::int32_t),
                          cudaMemcpyDeviceToHost, resident.stream),
          "download final C");
    check(cudaStreamSynchronize(resident.stream), "complete CUDA compute callback");
    // An invalidated ticket still owns this result until the synchronization
    // above. The Coordinator discards it before proof or submission.
    return output;
}

} // namespace neuro::host
