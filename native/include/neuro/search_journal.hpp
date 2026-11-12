// SPDX-License-Identifier: MIT
#pragma once

#include "neuro/mining_job.hpp"

#include <cstdint>
#include <filesystem>
#include <memory>
#include <string>

namespace neuro::host {

enum class AttemptOutcome : std::uint8_t {
    running = 1, found, miss, cancelled, error, interrupted, intent,
    accepted, rejected, uncertain, discarded
};
const char* outcome_name(AttemptOutcome outcome) noexcept;

struct SearchBinding {
    std::uint32_t work_id = 0;
    std::uint32_t next_attempt = 0;
    std::uint32_t limit = 0;
    bool resumed = false;
};

// Single-process, externally serialized lifetime. An OS file lock excludes a
// second cooperative writer and is released on process death. The parent must
// already exist. Use a local durable filesystem, not an unverified network FS.
//
// Each state transition is an appended BLAKE3-chained, flushed frame. Malformed,
// truncated, empty existing and unsupported journals fail closed; nothing is
// silently truncated, reset or recreated. A failed append poisons this object.
// Whole-file rollback/deletion and a dishonest storage device are outside this
// boundary: retain the same journal rather than resetting consumed attempts.
//
// Opening recovers running/found as interrupted and intent as uncertain, before
// allowing new work. Neither a saved proof nor a send is automatically retried.
// A fresh authorized notify must bind the complete work before begin is called.
// Maximum 64 retained identities, 64 attempts each, 32768 frames, 4 MiB; no
// eviction. A full journal stops rather than forgetting a completed attempt.
class SearchJournal {
public:
    explicit SearchJournal(const std::filesystem::path& path, std::string peer_scope);
    ~SearchJournal();
    SearchJournal(const SearchJournal&) = delete;
    SearchJournal& operator=(const SearchJournal&) = delete;

    // Allocates and durably records a monotonic connection epoch, before connect.
    std::uint64_t next_connection();

    // Search equality includes all Job fields except ephemeral connection and
    // transport gzip encoding, plus seed. An encoding-only change must not
    // replay the same consumed proof. Original/current gzip values are retained
    // as transport provenance. Scope is fixed by the journal header. A matching
    // search must retain its original limit. Binding records connection,
    // generation and negotiated gzip afresh; Coordinator still binds exact Jobs.
    SearchBinding bind(const Job& job, const Seed& seed, std::uint32_t limit,
                       std::uint64_t generation);

    // Durable consume BEFORE computation. Skipped/repeated indices are errors.
    void begin(std::uint32_t work_id, std::uint32_t index);
    // running -> found/miss/cancelled/error; found -> discarded.
    void finish(std::uint32_t work_id, std::uint32_t index, AttemptOutcome outcome);
    // found -> intent, BEFORE the first network send call. Uncertain sends are
    // never accepted/rejected without their exact live connection/request pair.
    void send_intent(std::uint32_t work_id, std::uint32_t index,
                     std::uint64_t connection, int request_id);
    void response(std::uint32_t work_id, std::uint32_t index,
                  std::uint64_t connection, int request_id, bool accepted);
    void uncertain(std::uint32_t work_id, std::uint32_t index);

    bool healthy() const noexcept;
    std::string report_json() const;

private:
    struct Impl;
    std::unique_ptr<Impl> impl_;
};

} // namespace neuro::host
