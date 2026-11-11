// Protocol-prepared arithmetic fixtures; no proof generation or pool connection.
#include "neuro/fullk_cuda.cuh"
#include "neuro/pearl_input.hpp"
#include "fixtures/pearl_golden.hpp"

#include <blake3.h>
#include <cuda_runtime.h>

#include <cstdint>
#include <iostream>
#include <sstream>
#include <stdexcept>
#include <string>
#include <vector>

namespace fullk = neuro::fullk;
namespace pearl = neuro::pearl;

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
        check(cudaMalloc(reinterpret_cast<void**>(&pointer_), bytes()), "cudaMalloc");
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

pearl::Digest digest(const void* data, std::size_t size) {
    pearl::Digest result{};
    blake3_hasher hasher;
    blake3_hasher_init(&hasher);
    blake3_hasher_update(&hasher, data, size);
    blake3_hasher_finalize(&hasher, result.data(), result.size());
    return result;
}

pearl::Digest digest_final_c(const std::vector<std::int32_t>& values) {
    std::vector<std::uint8_t> bytes(values.size() * 4);
    for (std::size_t i = 0; i < values.size(); ++i) {
        const auto word = static_cast<std::uint32_t>(values[i]);
        for (int byte = 0; byte < 4; ++byte)
            bytes[4 * i + byte] = static_cast<std::uint8_t>(word >> (8 * byte));
    }
    return digest(bytes.data(), bytes.size());
}

void expect_digest(const pearl::Digest& actual, const pearl::Digest& expected,
                   const char* name, const char* field) {
    if (actual != expected)
        throw std::runtime_error(std::string(name) + ": independent fixture mismatch: " + field);
}

template<class T>
void compare(const std::vector<T>& actual, const T* expected, std::size_t count,
             const char* name, const char* field) {
    if (actual.size() != count)
        throw std::runtime_error(std::string(name) + ": wrong output size: " + field);
    for (std::size_t i = 0; i < count; ++i) {
        if (actual[i] != expected[i]) {
            std::ostringstream error;
            error << name << ' ' << field << '[' << i << "]: got "
                  << actual[i] << ", expected " << expected[i];
            throw std::runtime_error(error.str());
        }
    }
}

// Check the entire host input path against fixed independent expectations
// before using the CPU reference as the final-matrix GPU comparison target.
fullk::Result verify_host(const neuro::pearl_golden::Fixture& fixture,
                         const pearl::PreparedInput& prepared) {
    if (prepared.changed_row != fixture.changed_row)
        throw std::runtime_error(std::string(fixture.name) + ": changed signal row mismatch");
    expect_digest(prepared.job_key, fixture.job_key, fixture.name, "job key");
    expect_digest(prepared.commitment_a, fixture.commitment_a, fixture.name, "A commitment");
    expect_digest(prepared.commitment_b, fixture.commitment_b, fixture.name, "Bt commitment");
    expect_digest(prepared.a_noise_seed, fixture.a_noise_seed, fixture.name, "A noise seed");
    expect_digest(prepared.b_noise_seed, fixture.b_noise_seed, fixture.name, "Bt noise seed");
    expect_digest(digest(prepared.signal_a.data(), prepared.signal_a.size()),
                  fixture.signal_a_hash, fixture.name, "signal A bytes");
    expect_digest(digest(prepared.signal_bt.data(), prepared.signal_bt.size()),
                  fixture.signal_bt_hash, fixture.name, "signal Bt bytes");
    expect_digest(digest(prepared.noisy.a.data(), prepared.noisy.a.size()),
                  fixture.noisy_a_hash, fixture.name, "noisy A bytes");
    expect_digest(digest(prepared.noisy.bt.data(), prepared.noisy.bt.size()),
                  fixture.noisy_bt_hash, fixture.name, "noisy Bt bytes");
    auto expected = fullk::reference(prepared.noisy);
    compare(expected.transcript, fixture.transcript, fixture.transcript_count,
            fixture.name, "CPU reference transcript");
    expect_digest(digest_final_c(expected.final_c), fixture.final_c_hash,
                  fixture.name, "CPU reference final C");
    return expected;
}

} // namespace

int main(int argc, char** argv) {
    bool host_only = false, gpu_observed = false, device_ready = false;
    std::size_t cases = 0, host_words = 0, gpu_words = 0, gpu_cells = 0, launches = 0;
    cudaDeviceProp properties{};
    try {
        int device = 0;
        bool device_selected = false;
        std::string selected_case;
        for (int i = 1; i < argc; ++i) {
            const std::string arg = argv[i];
            if (arg == "--help") {
                std::cout << "Usage: neuro_pearl_probe [--device N] [--case NAME]\n"
                             "       neuro_pearl_probe --host-only [--case NAME]\n"
                             "Default: verify protocol-prepared fixed inputs on the GPU.\n";
                return 0;
            }
            if (arg == "--host-only") host_only = true;
            else if (arg == "--case" && i + 1 < argc) {
                selected_case = argv[++i];
                if (selected_case.empty()) throw std::invalid_argument("fixture name is empty");
            } else if (arg == "--device" && i + 1 < argc) {
                const std::string value = argv[++i];
                std::size_t consumed = 0;
                device = std::stoi(value, &consumed);
                if (consumed != value.size() || device < 0)
                    throw std::invalid_argument("device must be a non-negative integer");
                device_selected = true;
            } else throw std::invalid_argument("Unknown or incomplete option: " + arg);
        }
        if (host_only && device_selected)
            throw std::invalid_argument("--device cannot be combined with --host-only");
        if (!selected_case.empty()) {
            bool found = false;
            for (const auto& fixture : neuro::pearl_golden::fixtures)
                found = found || selected_case == fixture.name;
            if (!found) throw std::invalid_argument("Unknown fixture name");
        }
        for (const auto& fixture : neuro::pearl_golden::fixtures) {
            if (!selected_case.empty() && selected_case != fixture.name) continue;
            pearl::Request request;
            request.shape = fixture.shape;
            request.cert_version = fixture.cert_version;
            request.header.assign(fixture.header.begin(), fixture.header.end());
            request.seed.assign(fixture.seed.begin(), fixture.seed.end());
            const auto prepared = pearl::prepare_zero_b(request);
            const auto expected = verify_host(fixture, prepared);
            host_words += expected.transcript.size();
            if (!host_only) {
                if (!device_ready) {
                    check(cudaSetDevice(device), "cudaSetDevice");
                    check(cudaGetDeviceProperties(&properties, device), "cudaGetDeviceProperties");
                    if (properties.major < 8 || properties.warpSize != 32)
                        throw std::runtime_error("Probe requires INT8 MMA and 32-thread warps");
                    device_ready = true;
                }
                DeviceBuffer<std::int8_t> a(prepared.noisy.a.size()), bt(prepared.noisy.bt.size());
                DeviceBuffer<std::uint32_t> transcript(expected.transcript.size());
                DeviceBuffer<std::int32_t> final_c(expected.final_c.size());
                check(cudaMemcpy(a.data(), prepared.noisy.a.data(), a.bytes(), cudaMemcpyHostToDevice), "copy A");
                check(cudaMemcpy(bt.data(), prepared.noisy.bt.data(), bt.bytes(), cudaMemcpyHostToDevice), "copy Bt");
                for (bool keep_c : {true, false}) {
                    check(cudaMemset(transcript.data(), 0xa5, transcript.bytes()), "poison transcript");
                    if (keep_c) check(cudaMemset(final_c.data(), 0x5a, final_c.bytes()), "poison final C");
                    check(fullk::launch_fullk(a.data(), bt.data(), transcript.data(),
                                             keep_c ? final_c.data() : nullptr, request.shape), "launch kernel");
                    check(cudaDeviceSynchronize(), "synchronize kernel");
                    gpu_observed = true;
                    ++launches;
                    std::vector<std::uint32_t> words(expected.transcript.size());
                    check(cudaMemcpy(words.data(), transcript.data(), transcript.bytes(), cudaMemcpyDeviceToHost), "read transcript");
                    compare(words, fixture.transcript, fixture.transcript_count, fixture.name, "GPU transcript");
                    gpu_words += words.size();
                    if (keep_c) {
                        std::vector<std::int32_t> cells(expected.final_c.size());
                        check(cudaMemcpy(cells.data(), final_c.data(), final_c.bytes(), cudaMemcpyDeviceToHost), "read final C");
                        compare(cells, expected.final_c.data(), expected.final_c.size(), fixture.name, "GPU final C");
                        gpu_cells += cells.size();
                    }
                }
                a.release(); bt.release(); transcript.release(); final_c.release();
            }
            ++cases;
        }
        if (!cases) throw std::runtime_error("No fixtures were checked");
        std::cout << "{\"schema\":\"neuro.pearl_probe.v1\",\"status\":\"PASS\",\"exact\":true,"
                  << "\"mode\":" << quote(host_only ? "host_preparation" : "gpu_arithmetic")
                  << ",\"gpu_execution_observed\":" << (gpu_observed ? "true" : "false")
                  << ",\"device\":" << (device_ready ? quote(properties.name) : "null")
                  << ",\"compute_capability\":" << (device_ready ? quote(std::to_string(properties.major)+"."+std::to_string(properties.minor)) : "null")
                  << ",\"cases\":" << cases << ",\"launches\":" << launches
                  << ",\"cpu_golden_transcript_words_checked\":" << host_words
                  << ",\"gpu_transcript_words_checked\":" << gpu_words
                  << ",\"gpu_final_c_values_checked\":" << gpu_cells
                  << ",\"k\":4096,\"rank\":128,\"proof_verified\":false,"
                     "\"pool_connected\":false,\"mining_evidence\":false,\"hashrate\":null}\n";
        return 0;
    } catch (const std::exception& error) {
        std::cout << "{\"schema\":\"neuro.pearl_probe.v1\",\"status\":\"FAIL\",\"exact\":false,"
                  << "\"mode\":" << quote(host_only ? "host_preparation" : "gpu_arithmetic")
                  << ",\"gpu_execution_observed\":" << (gpu_observed ? "true" : "false")
                  << ",\"completed_launches\":" << launches << ",\"error\":" << quote(error.what())
                  << ",\"proof_verified\":false,\"pool_connected\":false,"
                     "\"mining_evidence\":false,\"hashrate\":null}\n";
        return 1;
    }
}
