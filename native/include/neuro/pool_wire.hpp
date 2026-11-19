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

// Dial policy for the one donor connect call. No DNS and no address rewrite.
// Only the exact host 127.0.0.1 is dialable. A parsed authorization may name
// one other endpoint; that name stays undialable and is never resolved.
enum class EndpointStatus {
    loopback_dialable,
    external_not_named,
    external_named_undialable,
};

struct NamedEndpoint {
    std::string host;
    int port = 0;
};

NamedEndpoint parse_pool_authorization(std::string_view text);
// Empty path reads nothing and returns nullopt. Any other path must be one
// regular file of 1..512 bytes. The file is an endpoint name, not a wallet.
std::optional<NamedEndpoint> load_pool_authorization(std::string_view path);
EndpointStatus assess_endpoint(std::string_view host, int port,
                               const std::optional<NamedEndpoint>& named = std::nullopt);
// The only host string the donor connect may receive.
std::string loopback_dial_host(int port);

// One process-global donor connection, owned by one event-loop thread. The
// connect host is loopback_dial_host and is checked again before the donor
// call. The donor reader/session is never started. Calls made outside this
// adapter directly to donor pool globals are outside its ownership contract.
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
