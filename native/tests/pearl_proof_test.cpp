// SPDX-License-Identifier: MIT
#include "neuro/pearl_proof.hpp"
#include "fixtures/pearl_golden.hpp"
#include "fixtures/proof_golden.hpp"
#include "cp_noise.h"
#include "cp_proof.h"

#include <algorithm>
#include <array>
#include <chrono>
#include <cstddef>
#include <cstdint>
#include <exception>
#include <iomanip>
#include <iostream>
#include <sstream>
#include <stdexcept>
#include <string>
#include <utility>
#include <vector>

namespace {

using neuro::proof::Target;
using neuro::proof::Tile;
std::size_t verification_checks = 0, negative_checks = 0, semantic_rejections = 0;

void require(bool condition, const std::string& message) {
    if (!condition) throw std::runtime_error(message);
}

template<class F>
void expect_invalid(F&& function, const std::string& label) {
    try { function(); }
    catch (const std::invalid_argument&) { ++negative_checks; return; }
    throw std::runtime_error(label + " did not reject malformed input before FFI");
}

template<class F>
void expect_witness_failure(F&& function, const std::string& label) {
    try { function(); }
    catch (const std::invalid_argument& error) {
        throw std::runtime_error(label + " did not reach the real Rust builder: " + error.what());
    }
    catch (const std::runtime_error& error) {
        require(std::string(error.what()).find("real witness-proof builder:") == 0,
                label + " failed outside the real witness builder");
        ++negative_checks;
        ++semantic_rejections;
        return;
    }
    throw std::runtime_error(label + " was accepted by the real witness builder");
}

neuro::pearl::Request request_for(const neuro::pearl_golden::Fixture& fixture) {
    neuro::pearl::Request request;
    request.shape = fixture.shape;
    request.cert_version = fixture.cert_version;
    request.header.assign(fixture.header.begin(), fixture.header.end());
    request.seed.assign(fixture.seed.begin(), fixture.seed.end());
    return request;
}

std::string hash_be_hex(const neuro::pearl::Digest& hash) {
    std::ostringstream text;
    text << std::hex << std::setfill('0');
    for (auto it = hash.rbegin(); it != hash.rend(); ++it) text << std::setw(2) << unsigned(*it);
    return text.str();
}

std::string message_hex(const std::array<std::uint32_t, 16>& words) {
    std::ostringstream text;
    text << std::hex << std::setfill('0');
    for (const auto word : words) text << std::setw(8) << word;
    return text.str();
}

neuro::proof::Verification verify(const neuro::pearl::Request& request,
                                   const std::string& proof, const Target& target,
                                   bool expected, const std::string& label) {
    const auto result = neuro::proof::verify_plain(request.header, proof, target, request.cert_version);
    ++verification_checks;
    require(result.valid == expected, label + ": " + result.error);
    if (!expected) {
        require(!result.error.empty(), label + " returned an empty rejection");
        ++negative_checks;
    }
    return result;
}

void check_jackpot_rejection(const neuro::proof::Verification& result,
                             const neuro::proof_golden::Tile& golden) {
    require(!result.valid && result.error.find("Jackpot hash 0x" + hash_be_hex(golden.hash)) != std::string::npos,
            "Rust did not recompute the independent golden jackpot hash: " + result.error);
    require(result.error.find("recomputed msg=" + message_hex(golden.message)) != std::string::npos,
            "Rust did not recompute the independent golden jackpot message");
    ++semantic_rejections;
}

int b64_digit(char c) {
    static const std::string alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    const auto position = alphabet.find(c);
    require(position != std::string::npos, "test attempted to mutate a non-base64 payload byte");
    return static_cast<int>(position);
}

std::uint32_t decoded_group(const std::string& text, std::size_t offset) {
    const auto group = (offset / 3) * 4;
    require(group + 3 < text.size(), "test mutation offset exceeds proof");
    return (std::uint32_t(b64_digit(text[group])) << 18) |
           (std::uint32_t(b64_digit(text[group + 1])) << 12) |
           (std::uint32_t(b64_digit(text[group + 2])) << 6) |
           std::uint32_t(b64_digit(text[group + 3]));
}

std::uint8_t decoded_byte(const std::string& text, std::size_t offset) {
    return static_cast<std::uint8_t>(decoded_group(text, offset) >> (16 - unsigned(offset % 3) * 8));
}

std::string flipped_byte(std::string text, std::size_t offset) {
    static constexpr char alphabet[] = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    const auto group = (offset / 3) * 4;
    const auto changed = decoded_group(text, offset) ^ (1u << (16 - unsigned(offset % 3) * 8));
    for (unsigned i = 0; i < 4; ++i) text[group + i] = alphabet[(changed >> (18 - i * 6)) & 63u];
    return text;
}

std::string replaced_byte(std::string text, std::size_t offset, std::uint8_t value) {
    static constexpr char alphabet[] = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    const auto group = (offset / 3) * 4;
    const auto shift = 16 - unsigned(offset % 3) * 8;
    const auto changed = (decoded_group(text, offset) & ~(0xffu << shift)) | (std::uint32_t(value) << shift);
    for (unsigned i = 0; i < 4; ++i) text[group + i] = alphabet[(changed >> (18 - i * 6)) & 63u];
    return text;
}

Tile first_selected(const neuro::pearl::PreparedInput& input, const neuro::fullk::Result& arithmetic,
                     bool want_winner, std::size_t& tile_checks) {
    Tile first{-1, -1}, first_signal{-1, -1};
    const int signal_row = input.changed_row / neuro::fullk::hash_rows * neuro::fullk::hash_rows;
    for (int row = 0; row < input.noisy.shape.rows; row += neuro::fullk::hash_rows) {
        for (int col = 0; col < input.noisy.shape.cols; col += neuro::fullk::hash_cols) {
            const auto result = neuro::proof::evaluate_tile(input, arithmetic, {row, col},
                                                            neuro::proof_golden::pool_target_be);
            ++tile_checks;
            if (result.meets_target == want_winner) {
                if (first.row < 0) first = {row, col};
                if (first_signal.row < 0 && row == signal_row) first_signal = {row, col};
            }
        }
    }
    require(first.row >= 0, "fixed job contains no requested winning/losing tile");
    return first_signal.row >= 0 ? first_signal : first;
}

void abi_boundaries(const neuro::pearl::Request& request,
                     const neuro::pearl::PreparedInput& input, const std::string& proof, Tile tile) {
    std::array<char, 256> error{};
    std::array<char, 1> output{};
    const auto fail = [&](int status, const std::string& label) {
        require(status == -1 && error[0] != '\0', label + " did not return a real FFI error");
        ++negative_checks;
        error.fill('\0');
    };
    fail(cp_proof_build(nullptr, request.header.size(), PEARL_CONTIGUOUS_CONFIG, 52,
                        input.signal_a.data(), nullptr, request.shape.rows, request.shape.cols,
                        neuro::fullk::inner, neuro::fullk::rank, tile.row, tile.col,
                        CP_TILE_LAYOUT_CONTIGUOUS, output.data(), output.size(), error.data(), error.size()),
         "null build header");
    fail(cp_proof_build(request.header.data(), request.header.size(), PEARL_CONTIGUOUS_CONFIG, 51,
                        input.signal_a.data(), nullptr, request.shape.rows, request.shape.cols,
                        neuro::fullk::inner, neuro::fullk::rank, tile.row, tile.col,
                        CP_TILE_LAYOUT_CONTIGUOUS, output.data(), output.size(), error.data(), error.size()),
         "short mining configuration");
    fail(cp_proof_build(request.header.data(), request.header.size(), PEARL_CONTIGUOUS_CONFIG, 52,
                        input.signal_a.data(), nullptr, request.shape.rows, request.shape.cols,
                        neuro::fullk::inner, neuro::fullk::rank, tile.row, tile.col,
                        CP_TILE_LAYOUT_CONTIGUOUS, output.data(), output.size(), error.data(), error.size()),
         "one-byte output buffer");
    fail(cp_proof_verify(nullptr, request.header.size(), reinterpret_cast<const std::uint8_t*>(proof.data()),
                         proof.size(), neuro::proof_golden::pool_target_be.data(), 3, error.data(), error.size()),
         "null verify header");
    fail(cp_proof_build_witness(request.header.data(), request.header.size(), PEARL_CONTIGUOUS_CONFIG, 52,
                                nullptr, nullptr, request.shape.rows, request.shape.cols,
                                neuro::fullk::inner, neuro::fullk::rank, tile.row, tile.col,
                                CP_TILE_LAYOUT_CONTIGUOUS, output.data(), output.size(), error.data(), error.size()),
         "null witness");
    std::array<std::uint32_t, CP_WITNESS_MAX_BLOCKS> blocks{};
    require(cp_proof_witness_blocks(CP_TILE_LAYOUT_CONTIGUOUS, 0, tile.row, request.shape.rows,
                                    neuro::fullk::inner, blocks.data(), 0) == -1,
            "zero-capacity witness output accepted");
    ++negative_checks;
    require(cp_proof_witness_blocks(CP_TILE_LAYOUT_CONTIGUOUS, 0, tile.row, request.shape.rows,
                                    neuro::fullk::inner, nullptr, blocks.size()) == -1,
            "null witness output accepted");
    ++negative_checks;
}

void malformed_boundaries(const neuro::pearl::Request& request,
                           const neuro::pearl::PreparedInput& input,
                           const neuro::fullk::Result& arithmetic,
                           const std::string& proof, Tile tile) {
    for (const auto bad : std::array<Tile, 5>{{{-8, 0}, {0, -16}, {1, 0}, {0, 1}, {128, 0}}}) {
        expect_invalid([&] { (void)neuro::proof::build_plain(request, input, bad); }, "invalid tile");
    }
    auto bad_request = request;
    bad_request.header.pop_back();
    expect_invalid([&] { (void)neuro::proof::build_plain(bad_request, input, tile); }, "short header");
    bad_request = request;
    bad_request.shape.rows = 129;
    expect_invalid([&] { (void)neuro::proof::build_plain(bad_request, input, tile); }, "invalid dimensions");
    auto short_input = input;
    short_input.signal_a.pop_back();
    expect_invalid([&] { (void)neuro::proof::build_plain(request, short_input, tile); }, "short A signal");
    short_input = input;
    short_input.signal_bt.pop_back();
    expect_invalid([&] { (void)neuro::proof::build_plain(request, short_input, tile); }, "short Bt signal");
    short_input = input;
    short_input.signal_bt[0] = 1;
    expect_invalid([&] { (void)neuro::proof::build_plain(request, short_input, tile); }, "nonzero Bt signal");
    short_input = input;
    short_input.a_noise_seed[0] ^= 1;
    expect_invalid([&] { (void)neuro::proof::build_plain(request, short_input, tile); }, "unbound noise seed");
    auto short_arithmetic = arithmetic;
    short_arithmetic.transcript.pop_back();
    expect_invalid([&] { (void)neuro::proof::evaluate_tile(input, short_arithmetic, tile,
                            neuro::proof_golden::pool_target_be); }, "short transcript");
    Target overflow{};
    overflow[2] = 0x20;
    expect_invalid([&] { (void)neuro::proof::scaled_target(overflow); }, "overflow target");
    (void)verify(request, proof, overflow, false, "overflow verification target");
    (void)verify(request, "", neuro::proof_golden::pool_target_be, false, "empty proof");
    (void)verify(request, "AAAA", neuro::proof_golden::pool_target_be, false, "truncated binary proof");
    (void)verify(request, "!!!!", neuro::proof_golden::pool_target_be, false, "bad base64");
    (void)verify(request, std::string(neuro::proof::max_proof_b64_bytes + 1, 'A'),
                 neuro::proof_golden::pool_target_be, false, "oversize proof");
    auto invalid_header = request;
    invalid_header.header.clear();
    (void)verify(invalid_header, proof, neuro::proof_golden::pool_target_be, false, "empty verify header");
    auto invalid_version = request;
    invalid_version.cert_version = 4;
    (void)verify(invalid_version, proof, neuro::proof_golden::pool_target_be, false, "unknown certificate version");
    // A bounded metadata mutation is rejected before the Rust parser allocates
    // or traverses using untrusted dimensions. No huge bincode vector is built.
    (void)verify(request, flipped_byte(proof, 7), neuro::proof_golden::pool_target_be, false,
                 "oversize declared matrix");
    constexpr std::size_t a_root_offset = 4 * 8 + 8 + 32 * (8 + 1024) + 8 + 32 * 8 + 8;
    std::uint64_t sibling_count = 0;
    for (unsigned b = 0; b < 8; ++b)
        sibling_count |= std::uint64_t(decoded_byte(proof, a_root_offset + 32 + b)) << (8 * b);
    require(sibling_count <= 64, "test anchor offset encountered an unexpected sibling count");
    const std::size_t anchor_offset = a_root_offset + 32 + 8 + std::size_t(sibling_count) * 32 + 8;
    auto extreme_anchor = replaced_byte(proof, anchor_offset, 0xf8);
    for (unsigned b = 1; b < 8; ++b) extreme_anchor = replaced_byte(std::move(extreme_anchor), anchor_offset + b, 0xff);
    const auto rejected = verify(request, extreme_anchor, neuro::proof_golden::pool_target_be, false,
                                 "extreme aligned row anchor");
    require(rejected.error.find("row anchor exceeds") != std::string::npos,
            "extreme anchor must fail its bounded subtraction check before Rust FFI");
}

} // namespace

int main() {
    try {
        std::size_t tile_checks = 0, transcript_words = 0, message_words = 0, hash_bytes = 0;
        std::size_t proof_bytes = 0, witness_equal_bytes = 0;
        std::string legacy_winner;
        neuro::pearl::Request legacy_request;
        const auto started = std::chrono::steady_clock::now();
        const auto bound = neuro::proof::scaled_target(neuro::proof_golden::pool_target_be);
        Target expected_bound{};
        expected_bound[0] = 0x40;
        require(bound == expected_bound, "fixed target must scale exactly to 2^254");

        for (std::size_t i = 0; i < neuro::proof_golden::fixtures.size(); ++i) {
            const auto& input_gold = neuro::pearl_golden::fixtures[i];
            const auto& proof_gold = neuro::proof_golden::fixtures[i];
            require(std::string(input_gold.name) == proof_gold.name, "input/proof fixture ordering differs");
            const auto request = request_for(input_gold);
            const auto input = neuro::pearl::prepare_zero_b(request);
            const auto arithmetic = neuro::fullk::reference(input.noisy);
            require(arithmetic.transcript.size() == input_gold.transcript_count &&
                    std::equal(arithmetic.transcript.begin(), arithmetic.transcript.end(), input_gold.transcript),
                    "native full-K transcript differs from independent Pearl fixture");
            transcript_words += arithmetic.transcript.size();
            std::string winner_proof;

            for (const bool winning : {true, false}) {
                const auto& golden = winning ? proof_gold.winner : proof_gold.loser;
                const auto selected = first_selected(input, arithmetic, winning, tile_checks);
                require(selected.row == golden.row && selected.col == golden.col,
                        "native first nontrivial winner/loser selection differs from Python");
                const auto candidate = neuro::proof::evaluate_tile(input, arithmetic, selected,
                                                                    neuro::proof_golden::pool_target_be);
                require(candidate.message == golden.message && candidate.hash == golden.hash &&
                        candidate.meets_target == winning, "native jackpot differs from independent golden");
                message_words += candidate.message.size();
                hash_bytes += candidate.hash.size();
                require(neuro::proof::evaluate_tile(input, arithmetic, selected, golden.target_accept_be).meets_target,
                        "native exact boundary accept failed");
                require(!neuro::proof::evaluate_tile(input, arithmetic, selected, golden.target_reject_be).meets_target,
                        "native one-below-boundary reject failed");
                const auto proof = neuro::proof::build_plain(request, input, selected);
                proof_bytes += proof.size();
                const auto fixed = verify(request, proof, neuro::proof_golden::pool_target_be, winning,
                                           std::string(proof_gold.name) + (winning ? " winner" : " loser"));
                if (!winning) check_jackpot_rejection(fixed, golden);
                (void)verify(request, proof, golden.target_accept_be, true, "boundary accepted");
                check_jackpot_rejection(verify(request, proof, golden.target_reject_be, false, "boundary rejected"), golden);
                check_jackpot_rejection(verify(request, proof, Target{}, false, "zero target rejected"), golden);
                if (winning) winner_proof = proof;
                if (i == 0 && winning) { legacy_winner = proof; legacy_request = request; }
                if (i == 1 && !winning) require(proof == legacy_winner, "cert2/3 changed the signal-only plain proof");
            }

            const Tile tile{proof_gold.winner.row, proof_gold.winner.col};
            auto witness = neuro::proof::make_witness(request, input, tile);
            const auto witness_proof = neuro::proof::build_witness(request, tile, witness);
            require(witness_proof == winner_proof, "real witness and full-matrix proof differ byte-for-byte");
            witness_equal_bytes += winner_proof.size();
            (void)verify(request, witness_proof, neuro::proof_golden::pool_target_be, true, "witness winner");
            const auto selected_block = witness.a.block_indices.front();
            witness.a.subroots[selected_block][0] ^= 1;
            expect_witness_failure([&] { (void)neuro::proof::build_witness(request, tile, witness); }, "corrupt A subroot");
            witness.a.subroots[selected_block][0] ^= 1;
            witness.a.blocks[0] ^= 1;
            expect_witness_failure([&] { (void)neuro::proof::build_witness(request, tile, witness); }, "corrupt A block");
            witness.a.blocks[0] ^= 1;
            witness.a.root[0] ^= 1;
            expect_witness_failure([&] { (void)neuro::proof::build_witness(request, tile, witness); }, "corrupt A root");
            witness.a.root[0] ^= 1;
            witness.bt.root[0] ^= 1;
            expect_witness_failure([&] { (void)neuro::proof::build_witness(request, tile, witness); }, "corrupt zero-B root");
            witness.bt.root[0] ^= 1;
            witness.a.blocks.pop_back();
            expect_invalid([&] { (void)neuro::proof::build_witness(request, tile, witness); }, "short witness block");

            auto changed_header = request;
            changed_header.header[0] ^= 1;
            auto rejected = verify(changed_header, winner_proof, neuro::proof_golden::pool_target_be, false, "changed header");
            require(rejected.error.find("Hash A mismatch") != std::string::npos,
                    "header mutation was not rejected by real Merkle binding: " + rejected.error);
            ++semantic_rejections;
            // Four dimensions, then 32 serialized A chunks. Assert root bytes
            // before using this known fixed-int bincode offset for mutation.
            constexpr std::size_t root_offset = 4 * 8 + 8 + 32 * (8 + 1024) + 8 + 32 * 8 + 8;
            for (std::size_t b = 0; b < input.commitment_a.size(); ++b) {
                require(decoded_byte(winner_proof, root_offset + b) == input.commitment_a[b],
                        "test root mutation offset no longer matches the serialized proof");
            }
            for (const auto offset : {std::size_t(48), root_offset}) {
                rejected = verify(request, flipped_byte(winner_proof, offset), neuro::proof_golden::pool_target_be,
                                  false, "tampered authenticated proof bytes");
                require(rejected.error.find("Hash A mismatch") != std::string::npos,
                        "proof mutation did not reach the real Merkle check: " + rejected.error);
                ++semantic_rejections;
            }
            if (i == 0) {
                malformed_boundaries(request, input, arithmetic, winner_proof, tile);
                abi_boundaries(request, input, winner_proof, tile);
            }
        }

        // Certificate version is external context. This chosen legacy winner
        // is an independently known V3 loser at the same target.
        legacy_request.cert_version = 3;
        check_jackpot_rejection(verify(legacy_request, legacy_winner, neuro::proof_golden::pool_target_be,
                                       false, "wrong certificate derivation"),
                                 neuro::proof_golden::fixtures[1].loser);
        const double seconds = std::chrono::duration<double>(std::chrono::steady_clock::now() - started).count();
        std::cout << "PASS real plain proof: fixtures=3 selected_tiles=6 tile_evaluations=" << tile_checks
                  << " transcript_words=" << transcript_words << " message_words=" << message_words
                  << " hash_bytes=" << hash_bytes << " proof_b64_bytes=" << proof_bytes
                  << " witness_equal_bytes=" << witness_equal_bytes
                  << " verify_calls=" << verification_checks << " rejected_cases=" << negative_checks
                  << " real_semantic_rejections=" << semantic_rejections
                  << " elapsed_seconds=" << std::fixed << std::setprecision(3) << seconds << '\n';
        return 0;
    } catch (const std::exception& error) {
        std::cerr << "FAIL real plain proof: " << error.what() << '\n';
        return 1;
    }
}
