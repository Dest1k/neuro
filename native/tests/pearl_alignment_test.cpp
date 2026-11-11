// SPDX-License-Identifier: MIT
// Link the unmodified donor's noise, job-control and pool-state implementations.
// No pool connection, miner worker, or proof implementation is started here.
#include "cp_noise.h"
#include "cp_job_ctrl.h"

#include <cstdint>
#include <iostream>
#include <stdexcept>
#include <vector>

int main() {
    try {
        if (pearl_run_alignment_tests() != 0)
            throw std::runtime_error("Upstream Pearl alignment vectors failed");

        std::size_t checks = 0;
        const auto require = [&checks](bool condition, const char* message) {
            if (!condition) throw std::runtime_error(message);
            ++checks;
        };
        const std::uint8_t seed[32] = {};
        std::vector<std::int8_t> signal(4096), result(4096);
        require(cp_job_mining_active() == 0, "Unexpected initial mining state");
        require(cp_job_should_cancel() == 0, "Inactive job should not cancel");
        require(pearl_build_noisy_a(1, 4096, 128, seed, signal.data(), result.data()) == 0,
                "Inactive noisy A builder failed");
        require(pearl_build_noisy_b(1, 4096, 128, seed, nullptr, result.data()) == 0,
                "Inactive noisy B builder failed");

        cp_job_mine_begin("offline-alignment-cancellation");
        require(cp_job_mining_active() == 1, "Job did not become active");
        require(cp_job_should_cancel() == 0, "Fresh job unexpectedly cancelled");
        cp_job_request_cancel();
        require(cp_job_should_cancel() == 1, "Cancellation request was lost");
        require(pearl_generate_random_a(seed, 32, 1, 4096, result.data()) == -1,
                "Random A generator did not observe cancellation");
        require(pearl_build_noisy_a(1, 4096, 128, seed, signal.data(), result.data()) == -1,
                "Noisy A builder did not observe cancellation");
        require(pearl_build_noisy_b(1, 4096, 128, seed, nullptr, result.data()) == -1,
                "Noisy B builder did not observe cancellation");
        cp_job_mine_end();
        require(cp_job_mining_active() == 0, "Ended job remains active");
        require(cp_job_should_cancel() == 0, "Inactive job retains cancellation");

        cp_job_mine_begin("offline-alignment-next-job");
        require(cp_job_should_cancel() == 0, "New epoch retains the old cancellation");
        cp_job_mine_end();
        std::cout << "{\"schema\":\"neuro.pearl_alignment_test.v1\",\"status\":\"PASS\","
                     "\"kind\":\"upstream_cpu_alignment_and_cancellation\","
                     "\"alignment_vectors_passed\":true,\"cancellation_checks\":" << checks
                  << ",\"gpu_execution_observed\":false,\"pool_connection_observed\":false,"
                     "\"proof_verified\":false,\"mining_evidence\":false}\n";
        return 0;
    } catch (const std::exception& error) {
        cp_job_mine_end();
        std::cerr << "Pearl alignment/cancellation check failed: " << error.what() << '\n';
        std::cout << "{\"schema\":\"neuro.pearl_alignment_test.v1\",\"status\":\"FAIL\","
                     "\"gpu_execution_observed\":false,\"pool_connection_observed\":false,"
                     "\"proof_verified\":false,\"mining_evidence\":false}\n";
        return 1;
    }
}
