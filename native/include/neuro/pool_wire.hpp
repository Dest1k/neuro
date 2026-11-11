// SPDX-License-Identifier: MIT
#pragma once

#include "neuro/mining_job.hpp"

#include <cstddef>
#include <optional>
#include <string>
#include <string_view>

namespace neuro::wire {

inline constexpr std::size_t max_message_bytes = 65535;

enum class MessageKind { notify, difficulty, response, unknown };

struct Response {
    int id = 0;
    bool accepted = false; // Exactly result:true and no non-null error.
    bool gzip = false;     // Scoped top-level type:"v2", never a nested decoy.
    std::string error;
};

struct Message {
    MessageKind kind = MessageKind::unknown;
    host::Job job{};       // Caller fills shape, gzip and nonzero connection ID.
    double difficulty = 0;
    Response response{};
    std::string method;
    std::string raw;       // Retained bounded input for actual donor negotiation.
};

// Object-form Pearl notifications only: params object, or fields at top level.
// Requires explicit cert2/3. Missing target needs a previously confirmed positive
// difficulty; malformed explicit target never falls back. Strict bounded JSON
// grammar/type/duplicate checks precede the real donor notification parser.
// This profile uses ASCII strings and ordinary JSON escapes, without \u escapes.
Message parse_message(std::string_view text,
                       std::optional<double> confirmed_difficulty = std::nullopt);

enum class ReceiveStatus { message, timeout, lost };
struct Received {
    ReceiveStatus status = ReceiveStatus::timeout;
    std::string text;
};

// One process-global donor connection, owned by one event-loop thread. Only
// numeric 127.0.0.1 is supported here; there is no DNS or external pool access.
// The donor reader/session is never started. Calls made outside this adapter
// directly to donor pool globals are outside its ownership contract.
class LoopbackPool {
public:
    explicit LoopbackPool(int port, int io_timeout_ms = 1000);
    ~LoopbackPool();
    LoopbackPool(const LoopbackPool&) = delete;
    LoopbackPool& operator=(const LoopbackPool&) = delete;
    LoopbackPool(LoopbackPool&&) = delete;
    LoopbackPool& operator=(LoopbackPool&&) = delete;

    void authorize(int id, std::string_view wallet, std::string_view worker,
                    std::string_view agent, std::string_view password = "x");
    // Accepts only the exact pending authorize ID with result:true. The bounded
    // profile negotiates gzip via the top-level response type:"v2" field.
    void accept_authorize(const Message& message);
    Received receive(int timeout_ms);
    bool gzip() const noexcept { return gzip_; }
    bool authorized() const noexcept { return authorized_; }
    void submit(int id, std::string_view job_id, std::string_view plain_proof_b64);
    void close() noexcept;

private:
    bool owned_ = false;
    bool connected_ = false;
    bool authorized_ = false;
    bool gzip_ = false;
    int authorize_id_ = 0;
};

} // namespace neuro::wire
