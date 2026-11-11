// SPDX-License-Identifier: MIT
#include "neuro/mining_job.hpp"
#include "fixtures/pearl_golden.hpp"
#include "fixtures/proof_golden.hpp"

#include <algorithm>
#include <atomic>
#include <chrono>
#include <future>
#include <iostream>
#include <stdexcept>
#include <string>
#include <vector>

namespace {
using namespace neuro;
using namespace neuro::host;
std::atomic<std::size_t> checks{0}, fixture_calls{0};
std::size_t fullk_calls = 0, cancellation_cases = 0, concurrency_cases = 0;

void require(bool condition, const std::string& message) {
    if (!condition) throw std::runtime_error(message);
    ++checks;
}

struct ReleaseOnExit {
    std::promise<void>& promise;
    bool released = false;
    void release() { if (!released) { promise.set_value(); released = true; } }
    ~ReleaseOnExit() { release(); }
};

Job fixture_job() {
    const auto& fixture = pearl_golden::fixtures[1];
    Job job;
    job.job_id = "offline-job";
    job.header = fixture.header;
    job.target = proof_golden::pool_target_be;
    job.cert_version = fixture.cert_version;
    job.shape = fixture.shape;
    job.connection = 1;
    return job;
}

Result found(Coordinator& coordinator, const Job& job, const Compute& compute) {
    const auto result = coordinator.run_one(coordinator.publish(job), pearl_golden::fixed_seed, compute);
    require(result.status == Status::found && result.share && !result.share->proof_b64.empty(),
            std::string("expected real verified proof, got ") + status_name(result.status) + ": " + result.error);
    return result;
}

void identity_and_validation(const Job& job) {
    Coordinator coordinator;
    const auto original = coordinator.publish(job);
    const auto duplicate = coordinator.publish(job);
    require(original.generation == duplicate.generation && original.job == duplicate.job,
            "exact duplicate changed ticket");
    const std::vector<std::function<void(Job&)>> changes{
        [](Job& j) { j.job_id += "-new"; }, [](Job& j) { j.header.back() ^= 1; },
        [](Job& j) { j.target.back() ^= 1; }, [](Job& j) { j.cert_version = 2; },
        [](Job& j) { j.shape.rows = 256; }, [](Job& j) { j.shape.cols = 256; },
        [](Job& j) { j.gzip = true; }, [](Job& j) { j.connection = 2; }
    };
    for (const auto& change : changes) {
        const auto before = coordinator.publish(job);
        auto changed = job;
        change(changed);
        const auto after = coordinator.publish(changed);
        require(after.generation > before.generation && coordinator.cancelled(before) &&
                coordinator.current(after), "job field change did not invalidate old generation");
        changed.header.fill(0);
        require(after.job->header != changed.header, "snapshot borrows caller-owned data");
        const auto aba = coordinator.publish(job);
        require(aba.generation > after.generation && coordinator.cancelled(before),
                "A-B-A transition resurrected the old ticket");
    }
    auto live = coordinator.publish(job);
    const std::vector<std::function<void(Job&)>> invalid{
        [](Job& j) { j.job_id.clear(); }, [](Job& j) { j.job_id.assign(128, 'x'); },
        [](Job& j) { j.job_id = std::string("a\0b", 3); }, [](Job& j) { j.connection = 0; },
        [](Job& j) { j.cert_version = 1; }, [](Job& j) { j.cert_version = 4; },
        [](Job& j) { j.shape.rows = 64; }, [](Job& j) { j.shape.cols = -128; },
        [](Job& j) { j.shape.rows = 1152; }, [](Job& j) { j.target[0] = 1; }
    };
    for (const auto& change : invalid) {
        auto bad = job;
        change(bad);
        bool rejected = false;
        try { (void)coordinator.publish(bad); }
        catch (const std::invalid_argument&) { rejected = true; }
        require(rejected && coordinator.current(live), "invalid publish replaced the active job");
    }
    auto reconnected = job;
    reconnected.connection = 2;
    live = coordinator.publish(reconnected);
    require(!coordinator.disconnect(1) && coordinator.current(live),
            "old connection disconnect cancelled the new connection");
    require(coordinator.disconnect(2) && coordinator.cancelled(live), "disconnect left live work");
    const auto restarted = coordinator.publish(reconnected);
    require(restarted.generation > live.generation, "disconnect did not advance generation");
    coordinator.invalidate();
    require(coordinator.cancelled(restarted) && coordinator.cancelled(Ticket{}),
            "invalidation left a usable ticket");
}

void cancellations(const Job& job, const Compute& compute) {
    const Stage stages[]{Stage::before_prepare, Stage::after_prepare, Stage::before_compute,
                         Stage::after_compute, Stage::before_proof, Stage::after_proof,
                         Stage::after_verify};
    for (const auto cancel_at : stages) {
        Coordinator coordinator;
        const auto ticket = coordinator.publish(job);
        bool reached = false;
        int compute_calls = 0, sink_calls = 0;
        const auto result = coordinator.run_one(ticket, pearl_golden::fixed_seed,
            [&](const fullk::Input& input) { ++compute_calls; return compute(input); },
            [&](Stage stage, const Ticket& observed) {
                require(observed.job == ticket.job && observed.generation == ticket.generation,
                        "checkpoint changed the captured ticket");
                if (stage == cancel_at) { reached = true; coordinator.invalidate(); }
            });
        require(reached && result.status == Status::cancelled && !result.share,
                "cancellation checkpoint produced a usable result");
        require(!coordinator.commit(result, [&](const Share&) { ++sink_calls; return true; }) && !sink_calls,
                "cancelled result reached the sink");
        const bool before_compute = cancel_at == Stage::before_prepare || cancel_at == Stage::after_prepare ||
                                    cancel_at == Stage::before_compute;
        require(compute_calls == (before_compute ? 0 : 1), "cancellation ran an unexpected backend stage");
        ++cancellation_cases;
    }
}

void backend_failures(const Job& job, const Compute& compute, const fullk::Result& correct,
                      const Result& accepted) {
    const std::vector<Compute> failures{
        {}, [](const fullk::Input&) -> fullk::Result { throw std::runtime_error("backend failure"); },
        [](const fullk::Input&) -> fullk::Result { throw 42; },
        [&](const fullk::Input&) { auto bad = correct; bad.transcript.pop_back(); return bad; },
        [&](const fullk::Input&) { auto bad = correct; bad.transcript.push_back(0); return bad; },
        [&](const fullk::Input&) { auto bad = correct; bad.final_c.resize(1); return bad; }
    };
    for (const auto& failure : failures) {
        Coordinator coordinator;
        const auto result = coordinator.run_one(coordinator.publish(job), pearl_golden::fixed_seed, failure);
        require(result.status == Status::error && !result.share && !result.error.empty(),
                "broken backend did not return a bounded error");
        require(!coordinator.commit(result, [](const Share&) { return true; }), "backend error was committed");
    }

    // Deliberately preserve the compressed jackpot message/hash while corrupting
    // two of the selected tile's 32 milestones. A valid signal proof alone does
    // not detect this arithmetic mismatch; the selected-tile CPU fence must.
    auto corrupt = correct;
    const auto tile = accepted.share->tile;
    const auto tiles = fullk::tile_count(job.shape);
    const auto index = static_cast<std::size_t>(tile.row / 8) * (job.shape.cols / 16) + tile.col / 16;
    corrupt.transcript[index] ^= 1u;
    corrupt.transcript[16 * tiles + index] ^= 1u << 13;
    pearl::Request request;
    request.shape = job.shape;
    request.cert_version = job.cert_version;
    request.header.assign(job.header.begin(), job.header.end());
    request.seed.assign(pearl_golden::fixed_seed.begin(), pearl_golden::fixed_seed.end());
    const auto prepared = pearl::prepare_zero_b(request);
    const auto altered = proof::evaluate_tile(prepared, corrupt, tile, job.target);
    require(altered.meets_target && altered.hash == accepted.share->hash,
            "negative fixture did not preserve the winning jackpot hash");
    Coordinator coordinator;
    bool built_proof = false;
    const auto rejected = coordinator.run_one(coordinator.publish(job), pearl_golden::fixed_seed,
        [&](const fullk::Input&) { return corrupt; },
        [&](Stage stage, const Ticket&) { if (stage == Stage::after_proof) built_proof = true; });
    require(rejected.status == Status::error && !rejected.share && !built_proof &&
            rejected.error.find("selected-tile transcript") != std::string::npos,
            "corrupt selected milestones escaped the independent CPU check");

    auto impossible = job;
    impossible.target.fill(0);
    const auto miss = coordinator.run_one(coordinator.publish(impossible), pearl_golden::fixed_seed, compute);
    require(miss.status == Status::miss && !miss.share && miss.tiles_scanned == tiles,
            "zero target did not finish as a bounded miss");
}

void commit_failures(const Job& job, const Compute& compute) {
    Coordinator coordinator, foreign;
    const auto result = found(coordinator, job, compute);
    int calls = 0;
    const Sink sink = [&](const Share&) { ++calls; return true; };
    require(!foreign.commit(result, sink), "foreign coordinator accepted a ticket");
    Result forged;
    forged.status = Status::found;
    forged.ticket = result.ticket;
    forged.share = result.share;
    require(!coordinator.commit(forged, sink), "self-reported found status passed verification seal");
    auto altered = result;
    auto payload = *result.share;
    payload.proof_b64[0] = payload.proof_b64[0] == 'A' ? 'B' : 'A';
    altered.share = std::make_shared<const Share>(std::move(payload));
    require(!coordinator.commit(altered, sink), "replaced proof escaped immutable verification seal");
    altered = result;
    ++altered.ticket.generation;
    require(!coordinator.commit(altered, sink), "retagged proof escaped generation binding");
    require(!coordinator.commit(result, {}) && calls == 0, "empty sink changed handoff state");
    require(coordinator.commit(result, sink) && calls == 1, "verified result was not handed off");
    require(!coordinator.commit(result, sink) && calls == 1, "same ticket was handed off twice");

    for (bool throwing : {false, true}) {
        Coordinator failing;
        const auto ready = found(failing, job, compute);
        int attempts = 0;
        bool threw = false, sent = false;
        try {
            sent = failing.commit(ready, [&](const Share&) {
                ++attempts;
                if (throwing) throw std::runtime_error("bounded sink failed");
                return false;
            });
        } catch (const std::runtime_error&) { threw = true; }
        require(!sent && threw == throwing && attempts == 1, "sink failure was misreported");
        require(!failing.commit(ready, sink), "failed or throwing sink left the ticket reusable");
    }
    Coordinator stale;
    const auto ready = found(stale, job, compute);
    auto replacement = job;
    replacement.header.back() ^= 1;
    stale.publish(replacement);
    require(!stale.commit(ready, sink), "job change between proof and commit allowed stale handoff");
}

void controlled_concurrency(const Job& job, const Compute& compute) {
    {
        Coordinator coordinator;
        const auto ticket = coordinator.publish(job);
        std::promise<void> entered, release;
        auto entered_future = entered.get_future();
        auto release_future = release.get_future().share();
        auto worker = std::async(std::launch::async, [&] {
            return coordinator.run_one(ticket, pearl_golden::fixed_seed, [&](const fullk::Input& input) {
                entered.set_value();
                release_future.wait();
                return compute(input);
            });
        });
        ReleaseOnExit unblock{release};
        const bool reached = entered_future.wait_for(std::chrono::seconds(10)) == std::future_status::ready;
        auto replacement = job;
        replacement.connection = 2;
        const auto newer = coordinator.publish(replacement);
        unblock.release();
        const auto result = worker.get();
        require(reached && result.status == Status::cancelled && !result.share && coordinator.current(newer),
                "completion from the old connection survived a concurrent reconnect");
        ++concurrency_cases;
    }
    {
        Coordinator coordinator;
        const auto ticket = coordinator.publish(job);
        std::promise<void> entered, release;
        auto entered_future = entered.get_future();
        auto release_future = release.get_future().share();
        auto worker = std::async(std::launch::async, [&] {
            return coordinator.run_one(ticket, pearl_golden::fixed_seed,
                [&](const fullk::Input&) -> fullk::Result {
                    entered.set_value();
                    release_future.wait();
                    throw std::runtime_error("injected backend failure after invalidation");
                });
        });
        ReleaseOnExit unblock{release};
        const bool reached = entered_future.wait_for(std::chrono::seconds(10)) == std::future_status::ready;
        auto replacement = job;
        replacement.header.back() ^= 1;
        const auto newer = coordinator.publish(replacement);
        unblock.release();
        const auto result = worker.get();
        require(reached && result.status == Status::error && !result.share &&
                result.error.find("injected backend failure") != std::string::npos &&
                coordinator.cancelled(ticket) && coordinator.current(newer),
                "concurrent invalidation hid an arithmetic backend failure as cancellation");
        int sink_calls = 0;
        require(!coordinator.commit(result, [&](const Share&) { ++sink_calls; return true; }) && !sink_calls,
                "failed stale backend reached the sink");
        ++concurrency_cases;
    }
    {
        Coordinator coordinator;
        const auto ready = found(coordinator, job, compute);
        std::promise<void> sink_entered, release, publisher_entered;
        auto sink_future = sink_entered.get_future();
        auto publisher_future = publisher_entered.get_future();
        auto release_future = release.get_future().share();
        auto sender = std::async(std::launch::async, [&] {
            return coordinator.commit(ready, [&](const Share&) {
                sink_entered.set_value();
                release_future.wait();
                return true;
            });
        });
        ReleaseOnExit unblock{release};
        const bool entered = sink_future.wait_for(std::chrono::seconds(10)) == std::future_status::ready;
        auto replacement = job;
        replacement.header.back() ^= 1;
        auto publisher = std::async(std::launch::async, [&] {
            publisher_entered.set_value();
            return coordinator.publish(replacement);
        });
        const bool publishing = publisher_future.wait_for(std::chrono::seconds(10)) == std::future_status::ready;
        const bool serialized = publisher.wait_for(std::chrono::seconds(0)) == std::future_status::timeout;
        unblock.release();
        const bool committed = sender.get();
        const auto newer = publisher.get();
        require(entered && publishing && serialized && committed && coordinator.current(newer) &&
                coordinator.cancelled(ready.ticket), "publish was not serialized after the in-flight sink");
        ++concurrency_cases;
    }
    {
        Coordinator coordinator;
        const auto ready = found(coordinator, job, compute);
        std::promise<void> start;
        auto start_future = start.get_future().share();
        std::atomic<int> sink_calls{0};
        const auto attempt = [&] {
            start_future.wait();
            return coordinator.commit(ready, [&](const Share&) { ++sink_calls; return true; });
        };
        auto first = std::async(std::launch::async, attempt);
        auto second = std::async(std::launch::async, attempt);
        start.set_value();
        const bool a = first.get(), b = second.get();
        require(a != b && sink_calls == 1, "two concurrent commits submitted the same ticket");
        ++concurrency_cases;
    }
}

} // namespace

int main() {
    try {
        const auto job = fixture_job();
        fullk::Result arithmetic;
        Coordinator coordinator;
        const auto genuine = found(coordinator, job, [&](const fullk::Input& input) {
            arithmetic = fullk::reference(input);
            ++fullk_calls;
            const auto& expected = pearl_golden::fixtures[1];
            require(arithmetic.transcript.size() == expected.transcript_count &&
                    std::equal(arithmetic.transcript.begin(), arithmetic.transcript.end(), expected.transcript),
                    "actual CPU arithmetic did not match independent committed fixture");
            return arithmetic;
        });
        require(genuine.tiles_scanned > 0 && genuine.share->ticket.job == genuine.ticket.job,
                "found result lost its owned job snapshot");
        const Compute fixture_backend = [&](const fullk::Input& input) {
            require(input.shape.rows == job.shape.rows && input.shape.cols == job.shape.cols,
                    "fixture-only callback received a different shape");
            ++fixture_calls;
            auto output = arithmetic;
            output.final_c.clear(); // A real CUDA backend may request transcript only.
            return output;
        };
        identity_and_validation(job);
        cancellations(job, fixture_backend);
        backend_failures(job, fixture_backend, arithmetic, genuine);
        commit_failures(job, fixture_backend);
        controlled_concurrency(job, fixture_backend);
        std::cout << "{\"schema\":\"neuro.mining_job_test.v1\",\"status\":\"PASS\","
                     "\"kind\":\"bounded_cpu_host_lifecycle\",\"checks\":" << checks.load()
                  << ",\"full_matrix_cpu_reference_calls\":" << fullk_calls
                  << ",\"fixture_backend_calls\":" << fixture_calls.load()
                  << ",\"cancellation_checkpoints\":" << cancellation_cases
                  << ",\"controlled_concurrency_cases\":" << concurrency_cases
                  << ",\"genuine_plain_proof_observed\":true,\"gpu_execution_observed\":false,"
                     "\"pool_connection_observed\":false,\"pool_share_accepted\":false,"
                     "\"mining_performance_evidence\":false}\n";
        return 0;
    } catch (const std::exception& error) {
        std::cerr << "Host lifecycle check failed: " << error.what() << '\n';
        std::cout << "{\"schema\":\"neuro.mining_job_test.v1\",\"status\":\"FAIL\","
                     "\"gpu_execution_observed\":false,\"pool_share_accepted\":false}\n";
        return 1;
    }
}
