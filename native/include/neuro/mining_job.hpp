// SPDX-License-Identifier: MIT
#pragma once

#include "neuro/pearl_proof.hpp"

#include <array>
#include <cstdint>
#include <functional>
#include <memory>
#include <mutex>
#include <optional>
#include <string>

namespace neuro::host {

// One resolved notify. The target is unscaled and big endian. The connection
// identity is local and monotonic; a reconnect must never reuse its old value.
struct Job {
    std::string job_id;
    std::array<std::uint8_t, pearl::header_bytes> header{};
    proof::Target target{};
    int cert_version = 3;
    fullk::Shape shape{128, 128};
    bool gzip = false;
    std::uint64_t connection = 0;
};

using Seed = std::array<std::uint8_t, pearl::attempt_seed_bytes>;
struct AttemptIdentity {
    Seed root_seed{};
    std::uint32_t index = 0;
};

struct Ticket {
    std::uint64_t generation = 0;
    std::shared_ptr<const Job> job;
    // publish returns a job ticket without an attempt. Execution results own
    // an immutable seed/index identity, sealed together with the verified share.
    std::shared_ptr<const AttemptIdentity> attempt;
};

using Compute = std::function<fullk::Result(const fullk::Input&)>;
enum class Stage { before_prepare, after_prepare, before_compute, after_compute,
                   before_proof, after_proof, after_verify };
using Checkpoint = std::function<void(Stage, const Ticket&)>;

enum class Status { found, miss, cancelled, error };
const char* status_name(Status status) noexcept;

// Published only after the real Rust verifier accepts this raw PlainProof.
// Gzip, if negotiated, belongs to the transport; this raw proof is preserved.
struct Share {
    Ticket ticket;
    proof::Tile tile{-1, -1};
    pearl::Digest hash{};
    std::string proof_b64;
};

struct Result {
    Status status = Status::error;
    Ticket ticket;
    std::shared_ptr<const Share> share;
    std::size_t tiles_scanned = 0;
    std::string error;

private:
    // A caller cannot turn an error, mutated proof or foreign object into a
    // verified share merely by assigning public result fields.
    std::shared_ptr<const Share> verified_share_;
    friend class Coordinator;
};

using Sink = std::function<bool(const Share&)>;

class Coordinator {
public:
    // Exact duplicates retain their ticket. All Job fields participate in
    // equality. Invalid jobs throw without invalidating the current one.
    Ticket publish(Job job);
    // Restore only the consumed prefix of a freshly published generation.
    // The caller must have independently matched the durable job/seed plan to
    // a new authorized notify. No saved proof or submission permission is
    // restored. This is single-use even when next_attempt is zero.
    void restore_progress(const Ticket& ticket, const Seed& root_seed,
                          std::uint32_t next_attempt);
    void invalidate();
    // An old connection's late disconnect must not cancel a newer connection.
    bool disconnect(std::uint64_t connection);
    bool current(const Ticket& ticket) const;
    bool cancelled(const Ticket& ticket) const { return !current(ticket); }

    // Compatibility entry for attempt0; it consumes that attempt once.
    // Compute must finish using its input
    // before returning: a CUDA callback owns/synchronizes its buffers even if
    // the captured ticket becomes stale. No stage is forcefully preempted.
    // A Compute exception remains an error even after concurrent invalidation;
    // a damaged CUDA worker must not be reused as if it had only been cancelled.
    // A found tile's 32 transcript words are independently checked on CPU.
    // A miss does not establish correctness of all the backend's other tiles.
    Result run_one(const Ticket& ticket, const Seed& seed, const Compute& compute,
                   const Checkpoint& checkpoint = {});

    // Sequential, single-use indices 0..63 in one generation, with a root seed
    // pinned by the first attempt. No concurrent arithmetic attempts per job.
    // Signal A retains earlier row mutations; reconstruction is deterministic.
    // An error consumes a claimed index; cancellation/new work resets the plan.
    Result run_attempt(const Ticket& job_ticket, const Seed& root_seed, std::uint32_t index,
                       const Compute& compute, const Checkpoint& checkpoint = {});

    // A local handoff, not a pool acknowledgement. The bounded sink executes
    // under the publication mutex and must not reenter this Coordinator.
    // At most one sink invocation per attempt, including a failed/throwing sink.
    // Exceptions propagate after the ticket is consumed. Stale, forged and
    // repeated results return false without calling the sink.
    bool commit(const Result& result, const Sink& sink);

private:
    bool current_locked(const Ticket& ticket) const;
    void advance_locked();
    mutable std::mutex mutex_;
    std::uint64_t generation_ = 0;
    std::shared_ptr<const Job> job_;
    std::uint64_t committed_ = 0;
    std::optional<Seed> root_seed_;
    std::uint32_t next_attempt_ = 0;
    bool running_ = false;
};

} // namespace neuro::host
