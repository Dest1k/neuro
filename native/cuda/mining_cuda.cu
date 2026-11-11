// SPDX-License-Identifier: MIT
#include "neuro/mining_cuda.hpp"
#include "neuro/fullk_cuda.cuh"

#include <cuda_runtime.h>

#include <cstddef>
#include <cstdint>
#include <stdexcept>
#include <string>

namespace neuro::host {
namespace {

void check(cudaError_t result, const char* operation) {
    if (result != cudaSuccess)
        throw std::runtime_error(std::string(operation) + ": " + cudaGetErrorString(result));
}

class Stream {
public:
    Stream() { check(cudaStreamCreateWithFlags(&stream_, cudaStreamNonBlocking), "cudaStreamCreateWithFlags"); }
    ~Stream() { if (stream_) (void)cudaStreamDestroy(stream_); }
    Stream(const Stream&) = delete;
    Stream& operator=(const Stream&) = delete;
    cudaStream_t get() const { return stream_; }
private:
    cudaStream_t stream_ = nullptr;
};

template<class T> class Buffer {
public:
    explicit Buffer(std::size_t count) : count_(count) {
        check(cudaMalloc(reinterpret_cast<void**>(&data_), bytes()), "cudaMalloc");
    }
    ~Buffer() { if (data_) (void)cudaFree(data_); }
    Buffer(const Buffer&) = delete;
    Buffer& operator=(const Buffer&) = delete;
    T* get() const { return data_; }
    std::size_t bytes() const { return count_ * sizeof(T); }
private:
    T* data_ = nullptr;
    std::size_t count_;
};

} // namespace

fullk::Result compute_cuda(const fullk::Input& input, int device) {
    fullk::validate_shape(input.shape);
    if (device < 0) throw std::invalid_argument("CUDA device index must be non-negative");
    const auto a_count = static_cast<std::size_t>(input.shape.rows) * fullk::inner;
    const auto bt_count = static_cast<std::size_t>(input.shape.cols) * fullk::inner;
    if (input.a.size() != a_count || input.bt.size() != bt_count)
        throw std::invalid_argument("CUDA backend input lengths do not match the bounded shape");

    check(cudaSetDevice(device), "cudaSetDevice");
    cudaDeviceProp properties{};
    check(cudaGetDeviceProperties(&properties, device), "cudaGetDeviceProperties");
    if (properties.major != 12 || properties.minor != 0 || properties.warpSize != 32)
        throw std::runtime_error("The recorded mining CUDA build targets SM120 with 32-thread warps");

    fullk::Result output;
    output.transcript.resize(fullk::transcript_words(input.shape));
    output.final_c.resize(static_cast<std::size_t>(input.shape.rows) * input.shape.cols);
    Stream stream;
    Buffer<std::int8_t> a(a_count), bt(bt_count);
    Buffer<std::uint32_t> transcript(output.transcript.size());
    Buffer<std::int32_t> final_c(output.final_c.size());
    check(cudaMemcpyAsync(a.get(), input.a.data(), a.bytes(), cudaMemcpyHostToDevice, stream.get()),
          "upload noisy A");
    check(cudaMemcpyAsync(bt.get(), input.bt.data(), bt.bytes(), cudaMemcpyHostToDevice, stream.get()),
          "upload noisy Bt");
    check(fullk::launch_fullk(a.get(), bt.get(), transcript.get(), final_c.get(), input.shape, stream.get()),
          "launch full-K kernel");
    check(cudaMemcpyAsync(output.transcript.data(), transcript.get(), transcript.bytes(),
                          cudaMemcpyDeviceToHost, stream.get()), "download transcript");
    check(cudaMemcpyAsync(output.final_c.data(), final_c.get(), final_c.bytes(),
                          cudaMemcpyDeviceToHost, stream.get()), "download final C");
    check(cudaStreamSynchronize(stream.get()), "complete CUDA compute callback");
    // An invalidated ticket still owns these buffers until this synchronization.
    // The Coordinator discards its result before proof or submission afterward.
    return output;
}

} // namespace neuro::host
