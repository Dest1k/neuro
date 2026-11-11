// SPDX-License-Identifier: MIT
#include "blake3.h"
#include "fixtures/blake3_vectors.hpp"

#include <algorithm>
#include <array>
#include <cstdint>
#include <cstring>
#include <iostream>
#include <stdexcept>
#include <string>
#include <string_view>
#include <vector>

namespace golden = neuro::blake3_vectors;

namespace {

unsigned nibble(char value) {
    if (value >= '0' && value <= '9') return static_cast<unsigned>(value - '0');
    if (value >= 'a' && value <= 'f') return static_cast<unsigned>(value - 'a' + 10);
    throw std::runtime_error("Invalid official-answer hex encoding");
}

void compare(const std::uint8_t* actual, std::size_t count, std::string_view expected,
             std::size_t input_length, std::size_t mode) {
    if (expected.size() != 2 * golden::output_bytes || count > golden::output_bytes)
        throw std::runtime_error("Wrong official-answer output length");
    for (std::size_t index = 0; index < count; ++index) {
        const auto byte = static_cast<std::uint8_t>(
            (nibble(expected[2 * index]) << 4) | nibble(expected[2 * index + 1]));
        if (actual[index] != byte)
            throw std::runtime_error("BLAKE3 mismatch: input_length=" + std::to_string(input_length)
                + " mode=" + std::to_string(mode) + " byte=" + std::to_string(index));
    }
}

} // namespace

int main() {
    try {
        if (std::strcmp(blake3_version(), golden::version) != 0)
            throw std::runtime_error("BLAKE3 runtime version differs from the pinned vectors");
        static_assert(sizeof(golden::key) - 1 == BLAKE3_KEY_LEN, "Official key length");
        std::size_t longest = 0, checked_modes = 0, checked_bytes = 0;
        for (const auto& item : golden::vectors) longest = std::max(longest, item.input_length);
        std::vector<std::uint8_t> input(longest);
        for (std::size_t index = 0; index < input.size(); ++index)
            input[index] = static_cast<std::uint8_t>(index % 251);

        for (const auto& item : golden::vectors) {
            for (std::size_t mode = 0; mode < item.output_hex.size(); ++mode) {
                blake3_hasher hasher;
                if (mode == 0) blake3_hasher_init(&hasher);
                else if (mode == 1)
                    blake3_hasher_init_keyed(&hasher, reinterpret_cast<const std::uint8_t*>(golden::key));
                else blake3_hasher_init_derive_key(&hasher, golden::context);
                blake3_hasher_update(&hasher, input.data(), item.input_length);
                std::array<std::uint8_t, golden::output_bytes> xof{};
                std::array<std::uint8_t, BLAKE3_OUT_LEN> ordinary{};
                blake3_hasher_finalize(&hasher, xof.data(), xof.size());
                blake3_hasher_finalize(&hasher, ordinary.data(), ordinary.size());
                compare(xof.data(), xof.size(), item.output_hex[mode], item.input_length, mode);
                compare(ordinary.data(), ordinary.size(), item.output_hex[mode], item.input_length, mode);
                checked_bytes += xof.size() + ordinary.size();
                ++checked_modes;
            }
        }
        std::cout << "{\"schema\":\"neuro.blake3_vectors_test.v1\",\"status\":\"PASS\","
                     "\"kind\":\"official_cpu_hash_known_answers\",\"exact\":true,"
                     "\"gpu_execution_observed\":false,\"pool_connection_observed\":false,"
                     "\"proof_verified\":false,\"mining_evidence\":false,\"cases\":"
                  << golden::vectors.size() << ",\"checked_modes\":" << checked_modes
                  << ",\"checked_bytes\":" << checked_bytes << "}\n";
        return 0;
    } catch (const std::exception& error) {
        std::cerr << "BLAKE3 known-answer check failed: " << error.what() << '\n';
        std::cout << "{\"schema\":\"neuro.blake3_vectors_test.v1\",\"status\":\"FAIL\","
                     "\"exact\":false,\"gpu_execution_observed\":false,"
                     "\"pool_connection_observed\":false,\"proof_verified\":false,"
                     "\"mining_evidence\":false}\n";
        return 1;
    }
}
