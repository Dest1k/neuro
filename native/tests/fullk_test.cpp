// SPDX-License-Identifier: MIT
#include "neuro/fullk.hpp"
#include "fixtures/fullk_golden.hpp"

#include <array>
#include <cstddef>
#include <cstdint>
#include <iostream>
#include <limits>
#include <stdexcept>
#include <string>

namespace fullk = neuro::fullk;

namespace {

void require(bool condition, const std::string& message) {
    if (!condition) throw std::runtime_error(message);
}

template <typename T>
std::size_t compare_all(const std::vector<T>& actual, const T* expected,
                        std::size_t expected_size, const std::string& name) {
    require(actual.size() == expected_size, name + ": wrong output length");
    require(expected != nullptr || expected_size == 0, name + ": absent golden data");
    for (std::size_t i = 0; i < expected_size; ++i) {
        if (actual[i] != expected[i]) {
            throw std::runtime_error(name + ": mismatch at index " + std::to_string(i) +
                                     ", got " + std::to_string(actual[i]) +
                                     ", expected " + std::to_string(expected[i]));
        }
    }
    return expected_size;
}

template <typename Callable>
void require_invalid(Callable&& action, const std::string& name) {
    try {
        action();
    } catch (const std::invalid_argument&) {
        return;
    }
    throw std::runtime_error(name + ": expected std::invalid_argument");
}

std::size_t validate_bad_inputs() {
    // Test invalid shape before any input access or allocation. Maximum-sized
    // valid shapes are checked as metadata only, not expensive dense GEMMs.
    const std::array<fullk::Shape, 12> bad_shapes{{
        {-16, 16}, {0, 16}, {15, 16}, {17, 16},
        {16, -16}, {16, 0}, {16, 15}, {16, 17},
        {1040, 16}, {16, 1040},
        {std::numeric_limits<int>::max(), 16},
        {16, std::numeric_limits<int>::max()}
    }};
    std::size_t rejected = 0;
    for (const auto shape : bad_shapes) {
        const fullk::Input input{shape, {}, {}};
        require_invalid([&] { (void)fullk::make_input(shape, fullk::Pattern::zeros); },
                        "make_input invalid shape");
        require_invalid([&] { (void)fullk::reference(input); }, "reference invalid shape");
        require_invalid([&] { (void)fullk::emulate_warp(input); }, "emulator invalid shape");
        rejected += 3;
    }

    for (const auto shape : std::array<fullk::Shape, 4>{{
             {16, 16}, {16, fullk::max_dimension},
             {fullk::max_dimension, 16}, {fullk::max_dimension, fullk::max_dimension}}}) {
        fullk::validate_shape(shape);
        require(fullk::transcript_words(shape) ==
                    static_cast<std::size_t>(fullk::milestones) *
                    (shape.rows / 8) * (shape.cols / 16),
                "valid shape transcript length");
    }

    const auto valid = fullk::make_input({16, 16}, fullk::Pattern::zeros);
    for (int mutation = 0; mutation < 6; ++mutation) {
        auto input = valid;
        switch (mutation) {
            case 0: input.a.pop_back(); break;
            case 1: input.a.push_back(0); break;
            case 2: input.a.clear(); break;
            case 3: input.bt.pop_back(); break;
            case 4: input.bt.push_back(0); break;
            case 5: input.bt.clear(); break;
        }
        require_invalid([&] { (void)fullk::reference(input); }, "reference input length");
        require_invalid([&] { (void)fullk::emulate_warp(input); }, "emulator input length");
        rejected += 2;
    }
    for (const int value : {-1, 6, std::numeric_limits<int>::max()}) {
        require_invalid([&] {
            (void)fullk::make_input({16, 16}, static_cast<fullk::Pattern>(value));
        }, "unknown pattern");
        ++rejected;
    }
    return rejected;
}

}  // namespace

int main() {
    try {
        std::size_t fixture_count = 0;
        std::size_t oracle_words = 0;
        std::size_t emulator_words = 0;
        for (const auto& fixture : neuro::golden::fixtures) {
            const auto input = fullk::make_input(fixture.shape, fixture.pattern, fixture.seed);
            const auto expected = fullk::reference(input);
            const auto emulated = fullk::emulate_warp(input);
            const std::string name = fixture.name;

            require(fixture.transcript_count == fullk::transcript_words(fixture.shape),
                    name + ": golden transcript dimensions");
            require(fixture.final_count ==
                        static_cast<std::size_t>(fixture.shape.rows) * fixture.shape.cols,
                    name + ": golden final matrix dimensions");

            oracle_words += compare_all(expected.transcript, fixture.transcript,
                                        fixture.transcript_count, name + " reference transcript");
            oracle_words += compare_all(expected.final_c, fixture.final_c,
                                        fixture.final_count, name + " reference final C");
            emulator_words += compare_all(emulated.transcript, fixture.transcript,
                                          fixture.transcript_count, name + " emulator transcript");
            emulator_words += compare_all(emulated.final_c, fixture.final_c,
                                          fixture.final_count, name + " emulator final C");
            ++fixture_count;
        }
        require(fixture_count == 6, "expected all six independent golden fixtures");
        const std::size_t rejected = validate_bad_inputs();

        std::cout << "{\"schema\":\"neuro.fullk_cpu_test.v1\",\"status\":\"PASS\","
                     "\"kind\":\"cpu_reference_and_warp_emulation\","
                     "\"gpu_execution_observed\":false,\"mining_evidence\":false,"
                     "\"fixture_count\":" << fixture_count
                  << ",\"reference_checked_values\":" << oracle_words
                  << ",\"emulation_checked_values\":" << emulator_words
                  << ",\"checked_values\":" << oracle_words + emulator_words
                  << ",\"invalid_inputs_rejected\":" << rejected << "}\n";
        return 0;
    } catch (const std::exception& error) {
        std::cerr << "Full-K CPU check failed: " << error.what() << '\n';
        return 1;
    }
}
