// SPDX-License-Identifier: MIT
#pragma once

#include "neuro/pearl_proof.hpp"

#include <array>
#include <cstdint>
#include <functional>
#include <memory>
#include <mutex>
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

struct Ticket {
    std::uint64_t generation = 0;
    std::shared_ptr<const Job> job;
};

using Seed = std::array<std::uint8_t, pearl::attempt_seed_bytes>;
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
    void invalidate();
    // An old connection's late disconnect must not cancel a newer connection.
    bool disconnect(std::uint64_t connection);
    bool current(const Ticket& ticket) const;
    bool cancelled(const Ticket& ticket) const { return !current(ticket); }

    // One bounded first-zero-B attempt. Compute must finish using its input
    // before returning: a CUDA callback owns/synchronizes its buffers even if
    // the captured ticket becomes stale. No stage is forcefully preempted.
    // A Compute exception remains an error even after concurrent invalidation;
    // a damaged CUDA worker must not be reused as if it had only been cancelled.
    // A found tile's 32 transcript words are independently checked on CPU.
    // A miss does not establish correctness of all the backend's other tiles.
    Result run_one(const Ticket& ticket, const Seed& seed, const Compute& compute,
                   const Checkpoint& checkpoint = {});

    // A local handoff, not a pool acknowledgement. The bounded sink executes
    // under the publication mutex and must not reenter this Coordinator.
    // At most one sink invocation per ticket, including a failed/throwing sink.
    // Exceptions propagate after the ticket is consumed. Stale, forged and
    // repeated results return false without calling the sink.
    bool commit(const Result& result, const Sink& sink);

private:
    bool current_locked(const Ticket& ticket) const;
    void advance_locked();
    mutable std::mutex mutex_;
    std::uint64_t generation_ = 0;
    std::shared_ptr<const Job> job_;
    bool committed_ = false;
};

} // namespace neuro::host
