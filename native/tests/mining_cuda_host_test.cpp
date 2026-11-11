// SPDX-License-Identifier: MIT
#include "neuro/mining_cuda.hpp"

#include <iostream>
#include <stdexcept>

int main() {
    int rejected = 0;
    auto expect = [&](const neuro::fullk::Input& input, int device) {
        try { (void)neuro::host::compute_cuda(input, device); }
        catch (const std::invalid_argument&) { ++rejected; return; }
        throw std::runtime_error("Malformed CUDA backend call reached the runtime");
    };
    try {
        expect({{0, 128}, {}, {}}, 0);
        expect({{17, 128}, {}, {}}, 0);
        expect({{128, 2048}, {}, {}}, 0);
        expect({{128, 128}, {}, {}}, 0);
        expect({{128, 128}, {}, {}}, -1);
        std::cout << "PASS mining CUDA host boundary: rejected=" << rejected << " gpu_calls=0\n";
        return 0;
    } catch (const std::exception& error) {
        std::cerr << error.what() << '\n';
        return 1;
    }
}
