// SPDX-License-Identifier: MIT
#include "neuro/mining_job.hpp"
#include "fixtures/attempt_golden.hpp"
#include "fixtures/pearl_golden.hpp"
#include "fixtures/proof_golden.hpp"
#include "blake3.h"

#include <algorithm>
#include <atomic>
#include <chrono>
#include <future>
#include <iostream>
#include <limits>
#include <set>
#include <stdexcept>
#include <string>
#include <vector>

namespace {
using namespace neuro;
using namespace neuro::host;
std::size_t checks = 0, full_matrices = 0, genuine_proofs = 0;
void require(bool condition, const std::string& message) {
    if (!condition) throw std::runtime_error(message);
    ++checks;
}
pearl::Digest hash(const void* data, std::size_t length) {
    pearl::Digest out{};
    blake3_hasher h; blake3_hasher_init(&h);
    blake3_hasher_update(&h, data, length);
    blake3_hasher_finalize(&h, out.data(), out.size());
    return out;
}
template <class T> pearl::Digest hash(const std::vector<T>& bytes) {
    return hash(bytes.data(), bytes.size() * sizeof(T));
}
Job job(bool changed = false) {
    Job out;
    out.job_id = "bounded-attempts";
    out.header = pearl_golden::fixed_header;
    if (changed) out.header.back() ^= 1;
    out.target = proof_golden::pool_target_be;
    out.connection = 1;
    return out;
}
pearl::Request request(std::uint32_t index, bool changed = false) {
    pearl::Request out;
    const auto j = job(changed);
    out.header.assign(j.header.begin(), j.header.end());
    out.seed.assign(pearl_golden::fixed_seed.begin(), pearl_golden::fixed_seed.end());
    out.attempt_index = index;
    return out;
}
std::vector<fullk::Result> arithmetic;
Compute checked_backend() {
    return [](const fullk::Input& input) {
        const auto a = hash(input.a), b = hash(input.bt);
        for (std::size_t i = 0; i < attempt_golden::fixtures.size(); ++i) {
            const auto& g = attempt_golden::fixtures[i];
            if (a == g.noisy_a && b == g.noisy_bt) {
                auto result = arithmetic[i];
                result.final_c.clear();
                return result;
            }
        }
        throw std::runtime_error("input is not one of the independently checked arithmetic fixtures");
    };
}
const attempt_golden::Fixture& gold(std::uint32_t index, bool changed = false) {
    for (const auto& f : attempt_golden::fixtures)
        if (f.index == index && f.replacement_header == changed) return f;
    throw std::runtime_error("missing independent attempt fixture");
}

void independent_inputs() {
    for (const auto& g : attempt_golden::fixtures) {
        const auto input = pearl::prepare_zero_b(request(g.index, g.replacement_header));
        require(input.row_seed == g.row_seed, "derived row seed differs from independent Python");
        require(input.mutation_rows.size() == g.index + 1 &&
                std::equal(input.mutation_rows.begin(), input.mutation_rows.end(), g.mutation_rows.begin()),
                "cumulative row selection differs from independent Python");
        require(input.changed_row == g.mutation_rows[g.index], "last changed row differs");
        require(hash(input.signal_a) == g.signal_a && hash(input.signal_bt) == g.signal_bt,
                "cumulative signal bytes differ from independent Python");
        require(input.commitment_a == g.commitment_a && input.commitment_b == g.commitment_b &&
                input.a_noise_seed == g.a_noise_seed && input.b_noise_seed == g.b_noise_seed,
                "attempt commitments/noise seeds differ from independent Python");
        require(hash(input.noisy.a) == g.noisy_a && hash(input.noisy.bt) == g.noisy_bt,
                "attempt noisy matrices differ from independent Python");
        auto values = fullk::reference(input.noisy);
        ++full_matrices;
        require(hash(values.transcript) == g.transcript && hash(values.final_c) == g.final_c,
                "complete scalar matrix/transcript differs from independent Python");
        arithmetic.push_back(std::move(values));
    }
    const auto first = pearl::prepare_zero_b(request(0));
    const auto second = pearl::prepare_zero_b(request(1));
    const auto row0 = static_cast<std::size_t>(first.changed_row) * fullk::inner;
    require(first.changed_row != second.changed_row &&
            std::equal(first.signal_a.begin() + row0, first.signal_a.begin() + row0 + fullk::inner,
                       second.signal_a.begin() + row0), "attempt1 reset the earlier mutated row");
    const auto again = pearl::prepare_zero_b(request(1));
    require(second.signal_a == again.signal_a && second.noisy.a == again.noisy.a &&
            second.noisy.bt == again.noisy.bt, "reconstruction is not byte reproducible");
    for (const auto index : {pearl::max_attempts, std::numeric_limits<std::uint32_t>::max()}) {
        bool rejected = false;
        try { pearl::prepare_zero_b(request(index)); } catch (const std::invalid_argument&) { rejected = true; }
        require(rejected, "out-of-bound attempt allocated a preparation");
    }
    bool rejected = false;
    try { pearl::derive_row_seed({}, 0); } catch (const std::invalid_argument&) { rejected = true; }
    require(rejected, "short root seed accepted");
}

void sequential_and_sealed() {
    Coordinator coordinator;
    const auto base = coordinator.publish(job());
    const auto compute = checked_backend();
    {
        Coordinator captured;
        auto caller_seed = pearl_golden::fixed_seed;
        const auto frozen = captured.run_attempt(captured.publish(job()), caller_seed, 0, compute,
            [&](Stage stage, const Ticket&) {
                if (stage == Stage::before_prepare) caller_seed.back() ^= 1;
            });
        require(frozen.status == Status::found && frozen.ticket.attempt->root_seed == pearl_golden::fixed_seed &&
                frozen.share->hash == gold(0).winner_hash,
                "preparation borrowed a caller-mutated root seed after its identity was sealed");
        ++genuine_proofs;
    }
    std::vector<std::string> proofs;
    int sends = 0;
    for (std::uint32_t index = 0; index <= attempt_golden::miss_winning_attempt; ++index) {
        if (index == 1) {
            auto other = pearl_golden::fixed_seed; other.back() ^= 1;
            require(coordinator.run_attempt(base, other, index, compute).status == Status::error,
                    "changed root seed replaced the generation plan");
            require(coordinator.run_attempt(base, pearl_golden::fixed_seed, index + 1, compute).status == Status::error,
                    "skipped attempt index was accepted");
        }
        const auto result = coordinator.run_attempt(base, pearl_golden::fixed_seed, index, compute);
        const auto& g = gold(index);
        require(result.status == Status::found && result.share && result.ticket.attempt &&
                result.ticket.attempt->root_seed == pearl_golden::fixed_seed && result.ticket.attempt->index == index,
                "attempt lost its immutable root/index identity: " + result.error);
        require(result.share->tile.row == g.winner_row && result.share->tile.col == g.winner_col &&
                result.share->hash == g.winner_hash, "selected winner differs from independent gold");
        ++genuine_proofs;
        auto forged = result;
        auto retagged = *result.ticket.attempt;
        ++retagged.index;
        forged.ticket.attempt = std::make_shared<const AttemptIdentity>(retagged);
        require(!coordinator.commit(forged, [&](const Share&) { ++sends; return true; }),
                "result retagged to another attempt was committed");
        retagged = *result.ticket.attempt; retagged.root_seed.back() ^= 1;
        forged.ticket.attempt = std::make_shared<const AttemptIdentity>(retagged);
        require(!coordinator.commit(forged, [&](const Share&) { ++sends; return true; }),
                "result retagged to another root seed was committed");
        require(coordinator.commit(result, [&](const Share& share) {
                    ++sends; proofs.push_back(share.proof_b64); return true;
                }), "fresh verified attempt was not submitted");
        require(!coordinator.commit(result, [&](const Share&) { ++sends; return true; }),
                "attempt was submitted twice");
        require(coordinator.run_attempt(base, pearl_golden::fixed_seed, index, compute).status == Status::error,
                "attempt index was computed twice");
        const auto duplicate = coordinator.publish(job());
        require(duplicate.generation == base.generation && duplicate.job == base.job,
                "duplicate notify reset the attempt plan");
    }
    require(sends == static_cast<int>(proofs.size()) && std::set<std::string>(proofs.begin(), proofs.end()).size() == proofs.size(),
            "different cumulative attempts did not produce distinct verified proof bytes");
    Coordinator repeat;
    const auto again = repeat.publish(job());
    for (std::uint32_t i = 0; i < proofs.size(); ++i) {
        const auto result = repeat.run_attempt(again, pearl_golden::fixed_seed, i, compute);
        require(result.status == Status::found && result.share->proof_b64 == proofs[i],
                "recreated sequence did not produce byte-identical proofs");
        ++genuine_proofs;
    }
    require(coordinator.run_attempt(base, pearl_golden::fixed_seed, pearl::max_attempts, compute).status == Status::error,
            "coordinator exceeded fixed attempt bound");
}

void misses_and_cancellation() {
    const auto compute = checked_backend();
    Coordinator search;
    auto hard = job(); hard.target = attempt_golden::miss_target;
    const auto base = search.publish(hard);
    for (std::uint32_t i = 0; i <= attempt_golden::miss_winning_attempt; ++i) {
        const auto result = search.run_attempt(base, pearl_golden::fixed_seed, i, compute);
        if (i < attempt_golden::miss_winning_attempt) {
            require(result.status == Status::miss && !result.share && result.tiles_scanned == 128,
                    "independently predicted miss did not scan every tile");
            require(!search.commit(result, [](const Share&) { return true; }), "a miss was submitted");
        } else {
            require(result.status == Status::found && result.share &&
                    result.share->tile.row == attempt_golden::miss_winner_row &&
                    result.share->tile.col == attempt_golden::miss_winner_col &&
                    result.share->hash == attempt_golden::miss_winner_hash,
                    "search failed to reach independently predicted later proof");
            require(search.commit(result, [](const Share&) { return true; }), "later proof was not submitted");
            ++genuine_proofs;
        }
    }
    Coordinator cancelled;
    const auto old = cancelled.publish(job());
    const auto first = cancelled.run_attempt(old, pearl_golden::fixed_seed, 0, compute);
    require(first.status == Status::found && cancelled.commit(first, [](const Share&) { return true; }), "first attempt setup failed");
    Ticket replacement;
    const auto stale = cancelled.run_attempt(old, pearl_golden::fixed_seed, 1, compute,
        [&](Stage stage, const Ticket& ticket) {
            require(ticket.attempt && ticket.attempt->index == 1, "checkpoint lost later attempt identity");
            if (stage == Stage::after_compute) replacement = cancelled.publish(job(true));
        });
    require(stale.status == Status::cancelled && !stale.share &&
            !cancelled.commit(stale, [](const Share&) { return true; }), "cancelled later attempt reached submission");
    const auto fresh = cancelled.run_attempt(replacement, pearl_golden::fixed_seed, 0, compute);
    require(fresh.status == Status::found && fresh.share->hash == gold(0, true).winner_hash &&
            fresh.ticket.generation != first.ticket.generation && fresh.ticket.attempt->index == 0,
            "new work did not restart a fresh cumulative sequence");
    require(!cancelled.commit(first, [](const Share&) { return true; }), "old verified attempt survived job replacement");
    require(cancelled.commit(fresh, [](const Share&) { return true; }), "new generation's attempt0 was not submitted");
    genuine_proofs += 2;
}

void in_flight_exclusion() {
    Coordinator coordinator;
    const auto base = coordinator.publish(job());
    const auto compute = checked_backend();
    std::promise<void> entered, release;
    auto entered_future = entered.get_future();
    auto released = release.get_future().share();
    auto worker = std::async(std::launch::async, [&] {
        return coordinator.run_attempt(base, pearl_golden::fixed_seed, 0, [&](const fullk::Input& input) {
            entered.set_value(); released.wait(); return compute(input);
        });
    });
    const bool reached = entered_future.wait_for(std::chrono::seconds(10)) == std::future_status::ready;
    const auto overlap = coordinator.run_attempt(base, pearl_golden::fixed_seed, 1, compute);
    release.set_value();
    const auto first = worker.get();
    require(reached && overlap.status == Status::error && first.status == Status::found,
            "overlapping attempt was not excluded");
    const auto next = coordinator.run_attempt(base, pearl_golden::fixed_seed, 1, compute);
    require(next.status == Status::found, "rejected overlap consumed the next attempt index");
    bool threw = false;
    try { coordinator.commit(first, [](const Share&) -> bool { throw std::runtime_error("sink failed"); }); }
    catch (const std::runtime_error&) { threw = true; }
    require(threw && !coordinator.commit(first, [](const Share&) { return true; }) &&
            coordinator.commit(next, [](const Share&) { return true; }),
            "failed handoff was retried or consumed a different attempt");
    genuine_proofs += 2;
}
void restored_consumed_prefix() {
    const auto compute = checked_backend();
    const auto seed = pearl_golden::fixed_seed;
    auto rejects = [&](const std::function<void()>& action, const char* message) {
        bool rejected = false;
        try { action(); } catch (const std::invalid_argument&) { rejected = true; }
        require(rejected, message);
    };

    Coordinator resumed;
    const auto current = resumed.publish(job());
    rejects([&] { resumed.restore_progress(current, seed, pearl::max_attempts + 1); },
            "out-of-range durable progress was accepted");
    resumed.restore_progress(current, seed, 7);
    rejects([&] { resumed.restore_progress(current, seed, 0); },
            "second restore rewound consumed progress");
    int computed = 0;
    const Compute counting = [&](const fullk::Input& input) { ++computed; return compute(input); };
    require(resumed.run_attempt(current, seed, 0, counting).status == Status::error && computed == 0,
            "recovered prefix was recomputed");
    auto changed_seed = seed; changed_seed.back() ^= 1;
    require(resumed.run_attempt(current, changed_seed, 7, counting).status == Status::error && computed == 0,
            "recovery allowed a different root seed");
    const auto later = resumed.run_attempt(current, seed, 7, counting);
    require(later.status == Status::found && computed == 1 && later.share &&
            later.share->hash == gold(7).winner_hash,
            "recovered attempt did not retain independent cumulative sequence");
    require(resumed.commit(later, [](const Share&) { return true; }) &&
            !resumed.commit(later, [](const Share&) { return true; }),
            "recovery restored duplicate submission permissions");
    ++genuine_proofs;

    Coordinator zero;
    const auto zero_ticket = zero.publish(job());
    zero.restore_progress(zero_ticket, seed, 0);
    rejects([&] { zero.restore_progress(zero_ticket, seed, 1); },
            "zero-index restoration was not single-use");
    const auto first = zero.run_attempt(zero_ticket, seed, 0, compute);
    require(first.status == Status::found, "zero prefix prevented a genuine first attempt");
    ++genuine_proofs;
    rejects([&] { zero.restore_progress(first.ticket, seed, 1); },
            "attempt-bound ticket was accepted for restoration");
    const auto replacement = zero.publish(job(true));
    rejects([&] { zero.restore_progress(zero_ticket, seed, 1); },
            "stale generation was accepted for restoration");
    zero.restore_progress(replacement, seed, pearl::max_attempts);
    require(zero.run_attempt(replacement, seed, 63, counting).status == Status::error && computed == 1,
            "exhausted durable prefix allowed its final attempt again");
    require(!zero.commit(first, [](const Share&) { return true; }),
            "old verified proof survived restoration of a new generation");

    Coordinator used;
    const auto used_ticket = used.publish(job());
    require(used.run_attempt(used_ticket, seed, 0, compute).status == Status::found,
            "used generation fixture failed");
    ++genuine_proofs;
    rejects([&] { used.restore_progress(used_ticket, seed, 1); },
            "active generation accepted externally restored progress");
}
} // namespace

int main() {
    try {
        independent_inputs(); sequential_and_sealed(); misses_and_cancellation(); in_flight_exclusion();
        restored_consumed_prefix();
        std::cout << "{\"schema\":\"neuro.attempt_search_test.v1\",\"status\":\"PASS\",\"checks\":" << checks
                  << ",\"independent_full_matrix_cases\":" << full_matrices
                  << ",\"genuine_proofs\":" << genuine_proofs
                  << ",\"misses_before_later_proof\":" << attempt_golden::miss_winning_attempt
                  << ",\"maximum_attempts\":64,\"gpu_execution\":false,\"live_pool\":false}\n";
        return 0;
    } catch (const std::exception& error) {
        std::cerr << "Attempt search check failed: " << error.what() << '\n';
        return 1;
    }
}
