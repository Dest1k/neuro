#include "neuro/mma_layout.hpp"

#include <array>
#include <cstdint>
#include <iostream>
#include <stdexcept>

namespace layout = neuro::mma16832;

namespace {

void require(bool condition, const char* message) {
    if (!condition) throw std::runtime_error(message);
}

void coverage() {
    std::array<int, layout::rows * layout::inner> a{};
    std::array<int, layout::inner * layout::cols> b{};
    std::array<int, layout::rows * layout::cols> d{};
    for (int lane = 0; lane < 32; ++lane) {
        for (int reg = 0; reg < 4; ++reg) {
            const auto p = layout::d_coordinate(lane, reg);
            require(p.row >= 0 && p.row < 16 && p.col >= 0 && p.col < 8, "D outside tile");
            ++d[p.row * layout::cols + p.col];
            for (int base : {0, 32}) {
                for (int byte = 0; byte < 4; ++byte) {
                    const auto x = layout::a_coordinate(lane, reg, byte, base);
                    require(x.row >= 0 && x.row < 16 && x.col >= 0 && x.col < 64, "A outside tile");
                    ++a[x.row * layout::inner + x.col];
                    if (reg < 2) {
                        const auto y = layout::b_coordinate(lane, reg, byte, base);
                        require(y.row >= 0 && y.row < 64 && y.col >= 0 && y.col < 8, "B outside tile");
                        ++b[y.row * layout::cols + y.col];
                    }
                }
            }
        }
    }
    for (int count : a) require(count == 1, "A packing is not bijective");
    for (int count : b) require(count == 1, "B packing is not bijective");
    for (int count : d) require(count == 1, "D scatter is not bijective");
    require(layout::signed_byte(0x7f00ff80u, 0) == -128, "INT8 minimum decoding");
    require(layout::signed_byte(0x7f00ff80u, 1) == -1, "INT8 sign decoding");
    require(layout::signed_byte(0x7f00ff80u, 2) == 0, "INT8 zero decoding");
    require(layout::signed_byte(0x7f00ff80u, 3) == 127, "INT8 maximum decoding");
}

// Simulate the instruction's register inputs using independent inverse lookup
// equations. Do not call a_coordinate/b_coordinate to decode the packed words.
void compare_register_emulation(int test) {
    const auto input = layout::make_case(test);
    std::int32_t accumulators[32][4]{};
    for (int step = 0; step < 2; ++step) {
        std::uint32_t a[32][4]{}, b[32][2]{};
        for (int lane = 0; lane < 32; ++lane) {
            for (int word = 0; word < 4; ++word) {
                a[lane][word] = layout::pack_a(input.a.data(), lane, word, step * 32);
                if (word < 2) b[lane][word] = layout::pack_b(input.b.data(), lane, word, step * 32);
            }
        }
        for (int row = 0; row < 16; ++row) {
            for (int col = 0; col < 8; ++col) {
                std::int32_t sum = 0;
                for (int k = 0; k < 32; ++k) {
                    const int a_lane = (row % 8) * 4 + (k % 16) / 4;
                    const int a_word = row / 8 + 2 * (k / 16);
                    const int b_lane = col * 4 + (k % 16) / 4;
                    sum += layout::signed_byte(a[a_lane][a_word], k % 4)
                         * layout::signed_byte(b[b_lane][k / 16], k % 4);
                }
                // The inverse mapping fills the instruction's accumulator regs.
                accumulators[(row % 8) * 4 + col / 2][2 * (row / 8) + col % 2] += sum;
            }
        }
        std::array<std::int32_t, 128> actual{};
        for (int lane = 0; lane < 32; ++lane) {
            for (int reg = 0; reg < 4; ++reg) {
                const auto p = layout::d_coordinate(lane, reg);
                actual[p.row * 8 + p.col] = accumulators[lane][reg];
            }
        }
        require(actual == layout::reference(input, 32 * (step + 1)), "register emulation differs from row-major oracle");
    }
}

}  // namespace

int main() {
    try {
        coverage();
        for (int test = 0; test < layout::case_count; ++test) compare_register_emulation(test);
        std::cout << "{\"schema\":\"neuro.mma_layout_test.v1\",\"status\":\"PASS\","
                     "\"kind\":\"cpu_layout_emulation\",\"gpu_execution_observed\":false,"
                     "\"mining_evidence\":false,\"checked_values\":1024}\n";
        return 0;
    } catch (const std::exception& error) {
        std::cerr << "CPU layout test failed: " << error.what() << '\n';
        return 1;
    }
}
