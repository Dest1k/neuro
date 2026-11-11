// SPDX-License-Identifier: MIT
#include "neuro/pearl_proof.hpp"
#include "pearl_proof_internal.hpp"

#include "blake3.h"
#include "cp_noise.h"
#include "cp_proof.h"

#include <algorithm>
#include <array>
#include <cstdint>
#include <stdexcept>
#include <string>
#include <string_view>
#include <vector>

namespace neuro::proof {
namespace {

bool ascii_space(char c) {
    return c == ' ' || c == '\n' || c == '\r' || c == '\t' || c == '\v' || c == '\f';
}

std::string_view trim(std::string_view text) {
    while (!text.empty() && ascii_space(text.front())) text.remove_prefix(1);
    while (!text.empty() && ascii_space(text.back())) text.remove_suffix(1);
    return text;
}

int base64_digit(char c) {
    if (c >= 'A' && c <= 'Z') return c - 'A';
    if (c >= 'a' && c <= 'z') return c - 'a' + 26;
    if (c >= '0' && c <= '9') return c - '0' + 52;
    if (c == '+') return 62;
    if (c == '/') return 63;
    return -1;
}

std::vector<std::uint8_t> decode_bounded(std::string_view text) {
    if (text.empty() || text.size() > max_proof_b64_bytes || text.size() % 4 != 0) {
        throw std::invalid_argument("proof base64 size is outside the bounded profile");
    }
    std::vector<std::uint8_t> bytes;
    bytes.reserve(text.size() / 4 * 3);
    for (std::size_t i = 0; i < text.size(); i += 4) {
        const int a = base64_digit(text[i]);
        const int b = base64_digit(text[i + 1]);
        const bool pad_c = text[i + 2] == '=';
        const bool pad_d = text[i + 3] == '=';
        const int c = pad_c ? 0 : base64_digit(text[i + 2]);
        const int d = pad_d ? 0 : base64_digit(text[i + 3]);
        if (a < 0 || b < 0 || c < 0 || d < 0 ||
            (pad_c && !pad_d) || ((pad_c || pad_d) && i + 4 != text.size()) ||
            (pad_c && (b & 15) != 0) || (!pad_c && pad_d && (c & 3) != 0)) {
            throw std::invalid_argument("proof is not canonical bounded base64");
        }
        const std::uint32_t word = (std::uint32_t(a) << 18) | (std::uint32_t(b) << 12) |
                                   (std::uint32_t(c) << 6) | std::uint32_t(d);
        bytes.push_back(static_cast<std::uint8_t>(word >> 16));
        if (!pad_c) bytes.push_back(static_cast<std::uint8_t>(word >> 8));
        if (!pad_d) bytes.push_back(static_cast<std::uint8_t>(word));
    }
    return bytes;
}

class Reader {
public:
    explicit Reader(const std::vector<std::uint8_t>& bytes) : bytes_(bytes) {}
    std::uint64_t u64() {
        const std::size_t offset = position_;
        skip(8);
        std::uint64_t value = 0;
        for (unsigned i = 0; i < 8; ++i) value |= std::uint64_t(bytes_[offset + i]) << (8 * i);
        return value;
    }
    void skip(std::size_t count) {
        if (count > bytes_.size() - position_) throw std::invalid_argument("truncated plain proof");
        position_ += count;
    }
    std::size_t remaining() const { return bytes_.size() - position_; }
    bool none_tag() const { return remaining() == 1 && bytes_[position_] == 0; }
private:
    const std::vector<std::uint8_t>& bytes_;
    std::size_t position_ = 0;
};

void check_matrix(Reader& reader, std::uint64_t rows, std::uint64_t selected_rows) {
    const std::uint64_t leaves = selected_rows * (fullk::inner / 1024);
    const std::uint64_t total_leaves = rows * (fullk::inner / 1024);
    if (reader.u64() != leaves) throw std::invalid_argument("unexpected proof leaf count");
    for (std::uint64_t i = 0; i < leaves; ++i) {
        if (reader.u64() != 1024) throw std::invalid_argument("proof leaf must contain 1024 bytes");
        reader.skip(1024);
    }
    if (reader.u64() != leaves) throw std::invalid_argument("unexpected leaf-index count");
    std::array<std::uint64_t, 64> indices{};
    for (std::uint64_t i = 0; i < leaves; ++i) {
        indices[static_cast<std::size_t>(i)] = reader.u64();
        if (indices[static_cast<std::size_t>(i)] >= total_leaves) {
            throw std::invalid_argument("proof leaf index exceeds matrix dimensions");
        }
    }
    if (reader.u64() != total_leaves) throw std::invalid_argument("proof tree size differs from matrix dimensions");
    reader.skip(32); // Root bytes are authenticated by the Rust verifier.
    const std::uint64_t sibling_count = reader.u64();
    if (sibling_count > 64) throw std::invalid_argument("proof sibling count exceeds bounded profile");
    reader.skip(static_cast<std::size_t>(sibling_count) * 32);
    if (reader.u64() != selected_rows) throw std::invalid_argument("unexpected proof row count");
    const std::uint64_t anchor = reader.u64();
    if (anchor % selected_rows != 0 || anchor > rows - selected_rows) {
        throw std::invalid_argument("proof row anchor exceeds contiguous tile bounds");
    }
    for (std::uint64_t i = 1; i < selected_rows; ++i) {
        if (reader.u64() != anchor + i) throw std::invalid_argument("proof rows are not the expected contiguous tile");
    }
    for (std::uint64_t i = 0; i < leaves; ++i) {
        if (indices[static_cast<std::size_t>(i)] != anchor * (fullk::inner / 1024) + i) {
            throw std::invalid_argument("proof leaves do not match the selected rows");
        }
    }
}

pearl::Digest digest(const std::int8_t* bytes, std::size_t length, const pearl::Digest& key) {
    pearl::Digest result{};
    blake3_hasher hasher;
    blake3_hasher_init_keyed(&hasher, key.data());
    blake3_hasher_update(&hasher, bytes, length);
    blake3_hasher_finalize(&hasher, result.data(), result.size());
    return result;
}

bool hash_at_most(const pearl::Digest& hash_le, const Target& bound_be) {
    for (std::size_t i = 0; i < hash_le.size(); ++i) {
        const auto h = hash_le[hash_le.size() - 1 - i];
        if (h != bound_be[i]) return h < bound_be[i];
    }
    return true;
}

} // namespace

namespace detail {

void validate_request(const pearl::Request& request) {
    const int m = request.shape.rows;
    const int n = request.shape.cols;
    if (m < 128 || n < 128 || m > fullk::max_dimension || n > fullk::max_dimension ||
        m % 128 != 0 || n % 128 != 0) {
        throw std::invalid_argument("proof dimensions must be multiples of 128 in [128,1024]");
    }
    if (request.header.size() != pearl::header_bytes || request.seed.size() != pearl::attempt_seed_bytes) {
        throw std::invalid_argument("proof request requires header76 and attempt seed32");
    }
    if (request.cert_version != 2 && request.cert_version != 3) {
        throw std::invalid_argument("proof harness supports certificate versions 2 and 3");
    }
    if (request.tile_rows != fullk::hash_rows || request.tile_cols != fullk::hash_cols) {
        throw std::invalid_argument("proof harness requires contiguous 8x16 tiles");
    }
}

void validate_tile(fullk::Shape shape, Tile tile) {
    fullk::validate_shape(shape);
    if (tile.row < 0 || tile.col < 0 || tile.row % fullk::hash_rows != 0 ||
        tile.col % fullk::hash_cols != 0 || tile.row > shape.rows - fullk::hash_rows ||
        tile.col > shape.cols - fullk::hash_cols) {
        throw std::invalid_argument("proof tile anchor is outside the contiguous 8x16 grid");
    }
}

void validate_prepared(const pearl::Request& request, const pearl::PreparedInput& input) {
    validate_request(request);
    if (input.noisy.shape.rows != request.shape.rows || input.noisy.shape.cols != request.shape.cols ||
        input.signal_a.size() != std::size_t(request.shape.rows) * fullk::inner ||
        input.signal_bt.size() != std::size_t(request.shape.cols) * fullk::inner) {
        throw std::invalid_argument("prepared signal dimensions differ from proof request");
    }
    if (!std::all_of(input.signal_bt.begin(), input.signal_bt.end(), [](std::int8_t x) { return x == 0; })) {
        throw std::invalid_argument("proof producer currently requires an all-zero B signal");
    }
    if (!std::all_of(input.signal_a.begin(), input.signal_a.end(), [](std::int8_t x) { return x >= -64 && x <= 64; })) {
        throw std::invalid_argument("A signal is outside the verifier's int7 range [-64,64]");
    }
    pearl::Digest key{};
    blake3_hasher hasher;
    blake3_hasher_init(&hasher);
    blake3_hasher_update(&hasher, request.header.data(), request.header.size());
    blake3_hasher_update(&hasher, PEARL_CONTIGUOUS_CONFIG, 52);
    blake3_hasher_finalize(&hasher, key.data(), key.size());
    if (key != input.job_key || digest(input.signal_a.data(), input.signal_a.size(), key) != input.commitment_a ||
        digest(input.signal_bt.data(), input.signal_bt.size(), key) != input.commitment_b) {
        throw std::invalid_argument("prepared signal commitments do not match the proof job");
    }
    pearl::Digest b_seed{}, a_seed{};
    pearl_derive_noise_seeds(key.data(), input.commitment_a.data(), input.commitment_b.data(),
                             static_cast<std::uint32_t>(request.shape.rows),
                             static_cast<std::uint32_t>(request.shape.cols),
                             request.cert_version == 3, b_seed.data(), a_seed.data());
    if (a_seed != input.a_noise_seed || b_seed != input.b_noise_seed) {
        throw std::invalid_argument("prepared noise seeds do not match certificate version");
    }
}

void preflight(std::string_view proof_b64) {
    const auto bytes = decode_bounded(trim(proof_b64));
    Reader reader(bytes);
    const auto m = reader.u64();
    const auto n = reader.u64();
    const auto k = reader.u64();
    const auto rank = reader.u64();
    if (m < 128 || n < 128 || m > fullk::max_dimension || n > fullk::max_dimension ||
        m % 128 != 0 || n % 128 != 0 || k != fullk::inner || rank != fullk::rank) {
        throw std::invalid_argument("plain proof dimensions are outside the bounded dense profile");
    }
    check_matrix(reader, m, fullk::hash_rows);
    check_matrix(reader, n, fullk::hash_cols);
    if (reader.remaining() != 0 && !reader.none_tag()) {
        throw std::invalid_argument("unexpected trailing data or unsupported MoE proof");
    }
}

std::string output_string(const std::vector<char>& output) {
    const auto end = std::find(output.begin(), output.end(), '\0');
    if (end == output.end() || end == output.begin()) throw std::runtime_error("proof builder did not return a terminated proof");
    std::string result(output.begin(), end);
    preflight(result);
    return result;
}

} // namespace detail

Target scaled_target(const Target& pool_target_be) {
    if (pool_target_be[0] != 0 || pool_target_be[1] != 0 || (pool_target_be[2] & 0xe0u) != 0) {
        throw std::invalid_argument("pool target overflows the 2^19 Pearl work factor");
    }
    Target bound{};
    for (std::size_t i = 0; i + 2 < bound.size(); ++i) {
        const unsigned value = unsigned(pool_target_be[i + 2]) << 3;
        const unsigned low = i + 3 < bound.size() ? unsigned(pool_target_be[i + 3]) >> 5 : 0;
        bound[i] = static_cast<std::uint8_t>(value | low);
    }
    return bound;
}

Jackpot evaluate_tile(const pearl::PreparedInput& input, const fullk::Result& arithmetic,
                      Tile tile, const Target& pool_target_be) {
    detail::validate_tile(input.noisy.shape, tile);
    if (arithmetic.transcript.size() != fullk::transcript_words(input.noisy.shape)) {
        throw std::invalid_argument("jackpot evaluation requires the complete milestone transcript");
    }
    const auto bound = scaled_target(pool_target_be);
    const std::size_t tile_count = fullk::tile_count(input.noisy.shape);
    const std::size_t tile_index = std::size_t(tile.row / fullk::hash_rows) *
                                  std::size_t(input.noisy.shape.cols / fullk::hash_cols) +
                                  std::size_t(tile.col / fullk::hash_cols);
    Jackpot result;
    for (std::size_t j = 0; j < fullk::milestones; ++j) {
        auto& word = result.message[j % result.message.size()];
        word = (word << 13) | (word >> 19);
        word ^= arithmetic.transcript[j * tile_count + tile_index];
    }
    std::array<std::uint8_t, 64> bytes{};
    for (std::size_t i = 0; i < result.message.size(); ++i) {
        for (unsigned b = 0; b < 4; ++b) bytes[i * 4 + b] = static_cast<std::uint8_t>(result.message[i] >> (8 * b));
    }
    blake3_hasher hasher;
    blake3_hasher_init_keyed(&hasher, input.a_noise_seed.data());
    blake3_hasher_update(&hasher, bytes.data(), bytes.size());
    blake3_hasher_finalize(&hasher, result.hash.data(), result.hash.size());
    result.meets_target = hash_at_most(result.hash, bound);
    return result;
}

std::string build_plain(const pearl::Request& request, const pearl::PreparedInput& input, Tile tile) {
    detail::validate_prepared(request, input);
    detail::validate_tile(request.shape, tile);
    std::vector<char> output(max_proof_b64_bytes + 1, '\0');
    std::array<char, detail::error_capacity> error{};
    const int status = cp_proof_build(request.header.data(), request.header.size(),
                                     PEARL_CONTIGUOUS_CONFIG, 52, input.signal_a.data(), nullptr,
                                     request.shape.rows, request.shape.cols, fullk::inner, fullk::rank,
                                     tile.row, tile.col, CP_TILE_LAYOUT_CONTIGUOUS,
                                     output.data(), output.size(), error.data(), error.size());
    if (status != 0) throw std::runtime_error(std::string("real plain-proof builder: ") + error.data());
    return detail::output_string(output);
}

Verification verify_plain(const std::vector<std::uint8_t>& header, std::string_view proof_b64,
                           const Target& pool_target_be, int cert_version) {
    if (header.size() != pearl::header_bytes) return {false, "proof header must contain exactly 76 bytes"};
    if (cert_version != 2 && cert_version != 3) return {false, "proof harness supports certificate versions 2 and 3"};
    if (proof_b64.size() > max_proof_b64_bytes) return {false, "proof exceeds the bounded base64 size"};
    proof_b64 = trim(proof_b64);
    try {
        (void)scaled_target(pool_target_be);
        detail::preflight(proof_b64);
    } catch (const std::invalid_argument& error) {
        return {false, error.what()};
    }
    std::array<char, detail::error_capacity> error{};
    const int status = cp_proof_verify(header.data(), header.size(),
                                      reinterpret_cast<const std::uint8_t*>(proof_b64.data()),
                                      proof_b64.size(), pool_target_be.data(),
                                      static_cast<std::uint32_t>(cert_version), error.data(), error.size());
    if (status == 0) return {true, {}};
    return {false, error[0] ? std::string(error.data()) : "real plain-proof verifier rejected the proof"};
}

} // namespace neuro::proof
