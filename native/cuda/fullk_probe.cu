// Full-K arithmetic verification against independently generated fixed vectors.
// This program does not generate Pearl noise/proofs, connect to a pool, or mine.
#include "neuro/fullk_cuda.cuh"
#include "fixtures/fullk_golden.hpp"

#include <cuda_runtime.h>

#include <cstdint>
#include <iostream>
#include <limits>
#include <sstream>
#include <stdexcept>
#include <string>
#include <vector>

namespace fullk = neuro::fullk;

namespace {

std::string quote(const std::string& text) {
    std::ostringstream out;
    out << '"';
    constexpr char hex[] = "0123456789abcdef";
    for (unsigned char ch : text) {
        if (ch == '"' || ch == '\\') out << '\\' << char(ch);
        else if (ch < 32) out << "\\u00" << hex[ch >> 4] << hex[ch & 15];
        else out << char(ch);
    }
    out << '"';
    return out.str();
}

void check(cudaError_t error, const char* operation) {
    if (error != cudaSuccess)
        throw std::runtime_error(std::string(operation) + ": " + cudaGetErrorString(error));
}

template<class T> class DeviceBuffer {
public:
    explicit DeviceBuffer(std::size_t count) : count_(count) {
        check(cudaMalloc(reinterpret_cast<void**>(&pointer_), count_ * sizeof(T)), "cudaMalloc");
    }
    ~DeviceBuffer() { if (pointer_) (void)cudaFree(pointer_); }
    DeviceBuffer(const DeviceBuffer&) = delete;
    DeviceBuffer& operator=(const DeviceBuffer&) = delete;
    T* data() const { return pointer_; }
    std::size_t bytes() const { return count_ * sizeof(T); }
    void release() {
        T* pointer = pointer_;
        pointer_ = nullptr;
        check(cudaFree(pointer), "cudaFree");
    }
private:
    T* pointer_ = nullptr;
    std::size_t count_;
};

template<class T>
void compare(const std::vector<T>& actual, const T* expected, std::size_t count,
             const char* name, const char* field) {
    if (actual.size() != count)
        throw std::runtime_error(std::string(name) + ": wrong output size");
    for (std::size_t index = 0; index < count; ++index) {
        if (actual[index] != expected[index]) {
            std::ostringstream error;
            error << name << ' ' << field << '[' << index << "]: got "
                  << actual[index] << ", expected " << expected[index];
            throw std::runtime_error(error.str());
        }
    }
}

int host_validation() {
    // Every call must be rejected before any CUDA runtime operation. Non-null
    // pointers below are ordinary host scalars, used only with invalid shapes.
    std::int8_t a = 0, bt = 0;
    std::uint32_t transcript = 0;
    std::int32_t final_c = 0;
    std::size_t checks = 0;
    for (auto shape : {fullk::Shape{0,16}, {16,0}, {-16,16}, {16,-16},
                       {15,16}, {16,17}, {1040,16}, {16,1040},
                       {std::numeric_limits<int>::max(),16}}) {
        if (fullk::launch_fullk(&a, &bt, &transcript, &final_c, shape) != cudaErrorInvalidValue)
            throw std::runtime_error("Invalid shape was not rejected at the host boundary");
        ++checks;
    }
    for (int missing = 0; missing < 3; ++missing) {
        auto result = fullk::launch_fullk(missing == 0 ? nullptr : &a,
                                         missing == 1 ? nullptr : &bt,
                                         missing == 2 ? nullptr : &transcript,
                                         nullptr, {16,16});
        if (result != cudaErrorInvalidValue)
            throw std::runtime_error("Null required buffer was not rejected at the host boundary");
        ++checks;
    }
    std::cout << "{\"schema\":\"neuro.fullk_host_validation.v1\",\"status\":\"PASS\","
              << "\"checks\":" << checks << ",\"gpu_execution_observed\":false,"
              << "\"mining_evidence\":false}\n";
    return 0;
}

} // namespace

int main(int argc, char** argv) {
    bool gpu_observed = false;
    std::size_t launches = 0, checked_transcript = 0, checked_final = 0, cases = 0;
    try {
        if (argc == 2 && std::string(argv[1]) == "--host-validation") return host_validation();
        int device = 0;
        std::string selected_case;
        for (int index = 1; index < argc; ++index) {
            const std::string arg = argv[index];
            if (arg == "--help") {
                std::cout << "Usage: neuro_fullk_probe [--device N] [--case NAME]\n"
                             "       neuro_fullk_probe --host-validation\n"
                             "Default: verify all fixed arithmetic fixtures on the GPU.\n";
                return 0;
            }
            if (arg == "--device" && index + 1 < argc) {
                const std::string value = argv[++index];
                std::size_t consumed = 0;
                device = std::stoi(value, &consumed);
                if (consumed != value.size() || device < 0)
                    throw std::invalid_argument("device must be a non-negative integer");
            } else if (arg == "--case" && index + 1 < argc) {
                selected_case = argv[++index];
            } else throw std::invalid_argument("Unknown or incomplete command line option: " + arg);
        }
        if (!selected_case.empty()) {
            bool exists = false;
            for (const auto& fixture : neuro::golden::fixtures)
                exists = exists || selected_case == fixture.name;
            if (!exists) throw std::invalid_argument("Unknown fixture name");
        }
        check(cudaSetDevice(device), "cudaSetDevice");
        cudaDeviceProp properties{};
        check(cudaGetDeviceProperties(&properties, device), "cudaGetDeviceProperties");
        if (properties.major < 8 || properties.warpSize != 32)
            throw std::runtime_error("Probe requires INT8 MMA support and 32-thread warps");

        for (const auto& fixture : neuro::golden::fixtures) {
            if (!selected_case.empty() && selected_case != fixture.name) continue;
            const auto input = fullk::make_input(fixture.shape, fixture.pattern, fixture.seed);
            DeviceBuffer<std::int8_t> a(input.a.size()), bt(input.bt.size());
            DeviceBuffer<std::uint32_t> transcript(fixture.transcript_count);
            DeviceBuffer<std::int32_t> final_c(fixture.final_count);
            check(cudaMemcpy(a.data(), input.a.data(), a.bytes(), cudaMemcpyHostToDevice), "copy A");
            check(cudaMemcpy(bt.data(), input.bt.data(), bt.bytes(), cudaMemcpyHostToDevice), "copy Bt");
            // Also exercise the production-facing path that omits the full C
            // matrix while retaining every milestone word.
            for (bool keep_c : {true, false}) {
                check(cudaMemset(transcript.data(), 0xa5, transcript.bytes()), "poison transcript");
                if (keep_c) check(cudaMemset(final_c.data(), 0x5a, final_c.bytes()), "poison final C");
                check(fullk::launch_fullk(a.data(), bt.data(), transcript.data(),
                                         keep_c ? final_c.data() : nullptr, fixture.shape), "launch full-K kernel");
                check(cudaDeviceSynchronize(), "synchronize full-K kernel");
                gpu_observed = true;
                ++launches;
                std::vector<std::uint32_t> words(fixture.transcript_count);
                check(cudaMemcpy(words.data(), transcript.data(), transcript.bytes(), cudaMemcpyDeviceToHost), "read transcript");
                compare(words, fixture.transcript, fixture.transcript_count, fixture.name, "transcript");
                checked_transcript += words.size();
                if (keep_c) {
                    std::vector<std::int32_t> values(fixture.final_count);
                    check(cudaMemcpy(values.data(), final_c.data(), final_c.bytes(), cudaMemcpyDeviceToHost), "read final C");
                    compare(values, fixture.final_c, fixture.final_count, fixture.name, "final C");
                    checked_final += values.size();
                }
            }
            a.release(); bt.release(); transcript.release(); final_c.release();
            ++cases;
        }
        std::cout << "{\"schema\":\"neuro.fullk_probe.v1\",\"status\":\"PASS\","
                  << "\"exact\":true,\"gpu_execution_observed\":true,\"device\":" << quote(properties.name)
                  << ",\"compute_capability\":" << quote(std::to_string(properties.major)+"."+std::to_string(properties.minor))
                  << ",\"cases\":" << cases << ",\"launches\":" << launches
                  << ",\"transcript_words_checked\":" << checked_transcript
                  << ",\"final_c_values_checked\":" << checked_final
                  << ",\"k\":4096,\"rank\":128,\"mining_evidence\":false,\"hashrate\":null}\n";
        return 0;
    } catch (const std::exception& error) {
        std::cout << "{\"schema\":\"neuro.fullk_probe.v1\",\"status\":\"FAIL\",\"exact\":false,"
                  << "\"gpu_execution_observed\":" << (gpu_observed ? "true" : "false")
                  << ",\"completed_launches\":" << launches << ",\"error\":" << quote(error.what())
                  << ",\"mining_evidence\":false,\"hashrate\":null}\n";
        return 1;
    }
}
