// SPDX-License-Identifier: MIT
#include "neuro/pool_wire.hpp"

#include "cp_json_frame.h"
#include "cp_pool.h"
#include "cp_proof.h"
#include "cp_util.h"

#ifdef _WIN32
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <winsock2.h>
#else
#include <sys/select.h>
#include <sys/socket.h>
#include <sys/time.h>
#endif

#include <algorithm>
#include <array>
#include <atomic>
#include <cctype>
#include <charconv>
#include <cmath>
#include <cstdlib>
#include <fstream>
#include <optional>
#include <stdexcept>
#include <string>
#include <string_view>
#include <utility>
#include <vector>

#include <sys/stat.h>

namespace neuro::wire {
namespace {

enum class Kind { object, array, string, number, boolean, null_value };
struct Field { std::string name; std::size_t begin, end; Kind kind; };
using Fields = std::vector<Field>;

bool space(char c) { return c == ' ' || c == '\t' || c == '\r' || c == '\n'; }
bool digit(char c) { return c >= '0' && c <= '9'; }
std::string_view trimmed(std::string_view text) {
    while (!text.empty() && space(text.front())) text.remove_prefix(1);
    while (!text.empty() && space(text.back())) text.remove_suffix(1);
    return text;
}

// A local grammar guard for the small pool envelope. It retains field spans
// only for root/params/result, leaving extraction to the actual donor helpers.
class Guard {
public:
    explicit Guard(std::string_view input) : text_(input) {
        if (input.empty() || input.size() > max_message_bytes)
            fail("pool JSON size is outside the bounded profile");
        skip_space();
        if (peek() != '{') fail("pool message must be an object");
        object(0, &root);
        skip_space();
        if (position_ != text_.size()) fail("trailing data after pool JSON object");
    }
    Fields root, params, result;
private:
    std::string_view text_;
    std::size_t position_ = 0, values_ = 0;
    [[noreturn]] static void fail(const char* text) { throw std::invalid_argument(text); }
    char peek() const { return position_ < text_.size() ? text_[position_] : '\0'; }
    void skip_space() { while (space(peek())) ++position_; }
    void take(char expected) {
        if (peek() != expected) fail("malformed pool JSON delimiter");
        ++position_;
    }
    std::string string() {
        take('"');
        std::string decoded;
        while (peek() != '"') {
            const auto c = static_cast<unsigned char>(peek());
            if (c < 0x20 || c > 0x7e) fail("pool strings require bounded ASCII JSON");
            ++position_;
            char output = static_cast<char>(c);
            if (c == '\\') {
                const char escape = peek();
                ++position_;
                switch (escape) {
                    case '"': case '\\': case '/': output = escape; break;
                    case 'b': output = '\b'; break;
                    case 'f': output = '\f'; break;
                    case 'n': output = '\n'; break;
                    case 'r': output = '\r'; break;
                    case 't': output = '\t'; break;
                    default: fail("unsupported pool JSON string escape");
                }
            }
            decoded.push_back(output);
            if (decoded.size() > 8192) fail("pool JSON string exceeds profile limit");
        }
        take('"');
        return decoded;
    }
    void number() {
        if (peek() == '-') ++position_;
        if (peek() == '0') ++position_;
        else {
            if (peek() < '1' || peek() > '9') fail("invalid JSON number");
            while (digit(peek())) ++position_;
        }
        if (peek() == '.') {
            ++position_;
            if (!digit(peek())) fail("invalid JSON number fraction");
            while (digit(peek())) ++position_;
        }
        if (peek() == 'e' || peek() == 'E') {
            ++position_;
            if (peek() == '+' || peek() == '-') ++position_;
            if (!digit(peek())) fail("invalid JSON exponent");
            while (digit(peek())) ++position_;
        }
    }
    void literal(std::string_view value) {
        if (text_.substr(position_, value.size()) != value) fail("invalid JSON literal");
        position_ += value.size();
    }
    Kind value(unsigned depth, Fields* collect) {
        if (depth > 16 || ++values_ > 256) fail("pool JSON depth or value count exceeds profile limit");
        switch (peek()) {
            case '{': object(depth, collect); return Kind::object;
            case '[': {
                ++position_;
                skip_space();
                if (peek() == ']') { ++position_; return Kind::array; }
                for (;;) {
                    (void)value(depth + 1, nullptr);
                    skip_space();
                    if (peek() == ']') { ++position_; return Kind::array; }
                    take(',');
                    skip_space();
                }
            }
            case '"': (void)string(); return Kind::string;
            case 't': literal("true"); return Kind::boolean;
            case 'f': literal("false"); return Kind::boolean;
            case 'n': literal("null"); return Kind::null_value;
            default: number(); return Kind::number;
        }
    }
    void object(unsigned depth, Fields* collect) {
        take('{');
        skip_space();
        if (peek() == '}') { ++position_; return; }
        std::vector<std::string> names;
        for (;;) {
            auto name = string();
            if (std::find(names.begin(), names.end(), name) != names.end()) fail("duplicate pool JSON member");
            names.push_back(name);
            skip_space();
            take(':');
            skip_space();
            const auto begin = position_;
            Fields* child = nullptr;
            if (collect == &root && name == "params") child = &params;
            if (collect == &root && name == "result") child = &result;
            const auto kind = value(depth + 1, child);
            if (collect) collect->push_back({std::move(name), begin, position_, kind});
            skip_space();
            if (peek() == '}') { ++position_; return; }
            take(',');
            skip_space();
        }
    }
};

const Field* find(const Fields& fields, std::string_view name) {
    for (const auto& field : fields) if (field.name == name) return &field;
    return nullptr;
}

const Field& required(const Fields& fields, std::string_view name) {
    if (const auto* field = find(fields, name)) return *field;
    throw std::invalid_argument("pool message is missing " + std::string(name));
}

std::string string_field(const std::string& json, const Fields& fields, std::size_t scope,
                          const char* name, std::size_t maximum) {
    if (required(fields, name).kind != Kind::string) throw std::invalid_argument("pool string field has wrong type");
    std::vector<char> output(maximum + 1, '\0');
    if (!cp_json_member_str(json.c_str() + scope, name, output.data(), static_cast<int>(output.size())))
        throw std::invalid_argument("pool string field is invalid or too long");
    return output.data();
}

double positive_number(std::string_view raw) {
    const std::string text(trimmed(raw));
    char* end = nullptr;
    const double value = std::strtod(text.c_str(), &end);
    if (end != text.c_str() + text.size() || text.empty() || !std::isfinite(value) || value <= 0 || value >= 1e300)
        throw std::invalid_argument("pool difficulty must be a finite positive number below 1e300");
    return value;
}

int certificate(const std::string& json, const Field& field) {
    const auto raw = std::string_view(json).substr(field.begin, field.end - field.begin);
    if (field.kind != Kind::number || (raw != "2" && raw != "3"))
        throw std::invalid_argument("pool notify requires explicit integer cert_version 2 or 3");
    return raw == "2" ? 2 : 3;
}

void ascii_argument(std::string_view value, std::size_t maximum, bool allow_empty = false) {
    if ((!allow_empty && value.empty()) || value.size() > maximum)
        throw std::invalid_argument("pool string argument has invalid size");
    for (const unsigned char c : value)
        if (c < 0x20 || c > 0x7e) throw std::invalid_argument("pool string argument must be printable ASCII");
}

void proof_text(std::string_view value) {
    if (value.empty() || value.size() > proof::max_proof_b64_bytes || value.size() % 4)
        throw std::invalid_argument("submitted base64 proof has invalid size");
    std::size_t padding = 0;
    for (std::size_t i = 0; i < value.size(); ++i) {
        const char c = value[i];
        if (c == '=') { ++padding; if (i + 2 < value.size() || padding > 2) throw std::invalid_argument("invalid base64 padding"); }
        else if (padding || !((c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z') || digit(c) || c == '+' || c == '/'))
            throw std::invalid_argument("submitted proof must be safe base64 text");
    }
}

std::atomic<bool> connection_owned{false};

} // namespace

Message parse_message(std::string_view text, std::optional<double> confirmed_difficulty) {
    Guard guard(text);
    Message message;
    message.raw = std::string(text);
    const auto& json = message.raw;
    const auto* method = find(guard.root, "method");
    if (method) {
        message.method = string_field(json, guard.root, 0, "method", 64);
        if (find(guard.root, "result") || find(guard.root, "error"))
            throw std::invalid_argument("pool method message cannot also be a response");
        if (message.method == "mining.set_difficulty") {
            const auto& params = required(guard.root, "params");
            auto raw = std::string_view(json).substr(params.begin, params.end - params.begin);
            if (params.kind == Kind::array) raw = raw.substr(1, raw.size() - 2);
            else if (params.kind != Kind::number) throw std::invalid_argument("difficulty params must be numeric or a singleton array");
            message.difficulty = positive_number(raw);
            if (cp_pool_parse_difficulty(json.c_str()) != message.difficulty)
                throw std::invalid_argument("donor difficulty parser disagrees with strict envelope");
            message.kind = MessageKind::difficulty;
            return message;
        }
        if (message.method != "mining.notify") return message;
        const auto* params = find(guard.root, "params");
        if (params && params->kind != Kind::object)
            throw std::invalid_argument("bounded Pearl notify supports object params only");
        const Fields& fields = params ? guard.params : guard.root;
        const std::size_t scope = params ? params->begin : 0;
        const std::string job_id = string_field(json, fields, scope, "job_id", 127);
        ascii_argument(job_id, 127);
        const std::string header_hex = string_field(json, fields, scope, "header", 152);
        if (header_hex.size() != 152 || cp_hex_to_bytes(header_hex.c_str(), message.job.header.data(), 76) != 76)
            throw std::invalid_argument("pool header must encode exactly 76 bytes");
        const auto* cert = find(fields, "cert_version");
        const auto* outer_cert = params ? find(guard.root, "cert_version") : nullptr;
        if (!cert && !outer_cert) throw std::invalid_argument("pool notify is missing cert_version");
        const int version = certificate(json, cert ? *cert : *outer_cert);
        if (cert && outer_cert && certificate(json, *outer_cert) != version)
            throw std::invalid_argument("conflicting scoped certificate versions");

        std::string target_hex;
        if (find(fields, "target")) {
            target_hex = string_field(json, fields, scope, "target", 64);
            if (target_hex.size() != 64 || cp_hex_to_bytes(target_hex.c_str(), message.job.target.data(), 32) != 32)
                throw std::invalid_argument("explicit pool target must encode exactly 32 bytes");
        } else {
            if (!confirmed_difficulty || !std::isfinite(*confirmed_difficulty) || *confirmed_difficulty <= 0 || *confirmed_difficulty >= 1e300)
                throw std::invalid_argument("missing target requires confirmed difficulty");
            std::array<std::uint32_t, 8> words{};
            std::array<char, 65> encoded{};
            cp_pool_target_from_difficulty(*confirmed_difficulty, words.data());
            cp_le_words_to_be_target_hex(words.data(), encoded.data());
            if (cp_hex_to_bytes(encoded.data(), message.job.target.data(), 32) != 32)
                throw std::runtime_error("donor target conversion failed");
        }
        (void)proof::scaled_target(message.job.target);
        std::array<char, 128> donor_id{};
        std::array<char, 153> donor_header{};
        std::array<char, 65> donor_target{};
        std::uint32_t donor_cert = 0;
        if (!cp_pool_parse_notify(json.c_str(), donor_id.data(), donor_id.size(),
                                  donor_header.data(), donor_header.size(), donor_target.data(), donor_target.size(), &donor_cert) ||
            job_id != donor_id.data() || header_hex != donor_header.data() || target_hex != donor_target.data() ||
            donor_cert != static_cast<std::uint32_t>(version))
            throw std::invalid_argument("donor notify parser disagrees with strict scoped fields");
        message.job.job_id = job_id;
        message.job.cert_version = version;
        message.kind = MessageKind::notify;
        return message;
    }
    if (!find(guard.root, "id") || (!find(guard.root, "result") && !find(guard.root, "error"))) return message;
    const auto& id = required(guard.root, "id");
    std::string id_text;
    if (id.kind == Kind::string) id_text = string_field(json, guard.root, 0, "id", 11);
    else if (id.kind == Kind::number) id_text = json.substr(id.begin, id.end - id.begin);
    else throw std::invalid_argument("pool response id must be an integer or its decimal string");
    const auto converted = std::from_chars(id_text.data(), id_text.data() + id_text.size(), message.response.id);
    int donor_id = 0;
    if (converted.ec != std::errc{} || converted.ptr != id_text.data() + id_text.size() || message.response.id <= 0 ||
        !cp_json_rpc_response_id(json.c_str(), &donor_id) || donor_id != message.response.id)
        throw std::invalid_argument("invalid bounded pool response id");
    const auto* result = find(guard.root, "result");
    const auto* error = find(guard.root, "error");
    const bool has_error = error && error->kind != Kind::null_value;
    message.response.accepted = result && result->kind == Kind::boolean &&
        json.compare(result->begin, result->end - result->begin, "true") == 0 && !has_error;
    if (!message.response.accepted) message.response.error = has_error ? "pool returned a non-null error" : "pool result is not true";
    if (find(guard.root, "type")) message.response.gzip = string_field(json, guard.root, 0, "type", 16) == "v2";
    message.kind = MessageKind::response;
    return message;
}

namespace {

void validate_endpoint_port(int port) {
    if (port < 1 || port > 65535)
        throw std::invalid_argument("pool port is outside 1..65535");
}

void validate_endpoint_host(std::string_view host) {
    if (host.empty() || host.size() > 253)
        throw std::invalid_argument("pool host length is outside 1..253");
    if (host.front() == '.' || host.back() == '.' || host.front() == '-' || host.back() == '-')
        throw std::invalid_argument("pool host is not a bare name");
    std::size_t label = 0;
    for (const char c : host) {
        const auto u = static_cast<unsigned char>(c);
        if (u >= 128 || c == '/' || c == '\\' || c == '@' || c == ':' || c == '%' ||
            c == ' ' || c == '?' || c == '#' || c == '[' || c == ']' || c == ',')
            throw std::invalid_argument("pool host contains a character this dialer does not accept");
        if (c == '.') {
            if (label == 0) throw std::invalid_argument("pool host has an empty label");
            label = 0;
            continue;
        }
        const bool ok = (c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z') ||
                        (c >= '0' && c <= '9') || c == '-';
        if (!ok) throw std::invalid_argument("pool host contains a character this dialer does not accept");
        if (++label > 63) throw std::invalid_argument("pool host label exceeds 63 characters");
    }
}

int parse_port_text(std::string_view text) {
    if (text.empty() || text.size() > 5 || (text.size() > 1 && text.front() == '0'))
        throw std::invalid_argument("pool authorization port is not a canonical integer");
    int port = 0;
    const auto converted = std::from_chars(text.data(), text.data() + text.size(), port);
    if (converted.ec != std::errc{} || converted.ptr != text.data() + text.size())
        throw std::invalid_argument("pool authorization port is not a canonical integer");
    validate_endpoint_port(port);
    return port;
}

void reject_authorization_secrets(std::string_view text) {
    std::string lower(text);
    for (char& c : lower)
        c = static_cast<char>(std::tolower(static_cast<unsigned char>(c)));
    for (const char* word : {"wallet", "password", "worker", "secret", "mnemonic"}) {
        if (lower.find(word) != std::string::npos)
            throw std::invalid_argument("pool authorization file must name an endpoint only");
    }
}

} // namespace

NamedEndpoint parse_pool_authorization(std::string_view text) {
    if (text.size() > 512 || text.find('\0') != std::string_view::npos)
        throw std::invalid_argument("pool authorization text exceeds 512 bytes");
    reject_authorization_secrets(text);
    if (!text.empty() && text.back() == '\n') text.remove_suffix(1);
    if (!text.empty() && text.back() == '\r') text.remove_suffix(1);
    const auto split = text.find('\n');
    if (split == std::string_view::npos || text.find('\n', split + 1) != std::string_view::npos)
        throw std::invalid_argument("pool authorization must be exactly two lines");
    auto schema = text.substr(0, split);
    auto endpoint = text.substr(split + 1);
    if (!schema.empty() && schema.back() == '\r') schema.remove_suffix(1);
    if (!endpoint.empty() && endpoint.back() == '\r') endpoint.remove_suffix(1);
    if (schema != "neuro.pool_endpoint_authorization.v1")
        throw std::invalid_argument("pool authorization schema is not recognized");
    constexpr std::string_view prefix("endpoint=");
    if (endpoint.size() < prefix.size() || endpoint.compare(0, prefix.size(), prefix) != 0)
        throw std::invalid_argument("pool authorization is missing endpoint");
    const auto value = endpoint.substr(prefix.size());
    const auto colon = value.rfind(':');
    if (colon == std::string_view::npos || colon == 0 || colon + 1 >= value.size())
        throw std::invalid_argument("pool authorization endpoint must be host:port");
    const auto host = value.substr(0, colon);
    validate_endpoint_host(host);
    NamedEndpoint named;
    named.host.assign(host.begin(), host.end());
    named.port = parse_port_text(value.substr(colon + 1));
    if (named.host == "127.0.0.1")
        throw std::invalid_argument("pool authorization must not rename the loopback dial");
    return named;
}

std::optional<NamedEndpoint> load_pool_authorization(std::string_view path) {
    if (path.empty()) return std::nullopt;
    if (path.find('\0') != std::string_view::npos)
        throw std::invalid_argument("pool authorization path is invalid");
    const std::string file(path);
#if defined(_WIN32)
    struct _stat64 info {};
    if (_stat64(file.c_str(), &info) != 0 || (info.st_mode & _S_IFMT) != _S_IFREG)
        throw std::invalid_argument("pool authorization path is not a regular file");
    const auto size = info.st_size;
#else
    struct stat info {};
    if (lstat(file.c_str(), &info) != 0 || !S_ISREG(info.st_mode))
        throw std::invalid_argument("pool authorization path is not a regular file");
    const auto size = static_cast<decltype(info.st_size)>(info.st_size);
#endif
    if (size < 1 || size > 512)
        throw std::invalid_argument("pool authorization file size is outside 1..512 bytes");
    std::ifstream in(file, std::ios::binary);
    if (!in) throw std::invalid_argument("pool authorization file cannot be read");
    std::string body(static_cast<std::size_t>(size), '\0');
    in.read(body.data(), static_cast<std::streamsize>(size));
    if (in.gcount() != static_cast<std::streamsize>(size))
        throw std::invalid_argument("pool authorization file changed while it was read");
    char extra = 0;
    in.read(&extra, 1);
    if (in.gcount() != 0)
        throw std::invalid_argument("pool authorization file changed while it was read");
    return parse_pool_authorization(body);
}

EndpointStatus assess_endpoint(std::string_view host, int port, const std::optional<NamedEndpoint>& named) {
    validate_endpoint_host(host);
    validate_endpoint_port(port);
    if (host == "127.0.0.1") return EndpointStatus::loopback_dialable;
    if (named && named->host == host && named->port == port)
        return EndpointStatus::external_named_undialable;
    return EndpointStatus::external_not_named;
}

std::string loopback_dial_host(int port) {
    if (assess_endpoint("127.0.0.1", port, std::nullopt) != EndpointStatus::loopback_dialable)
        throw std::logic_error("numeric loopback is not dialable");
    return "127.0.0.1";
}

LoopbackPool::LoopbackPool(int port, int io_timeout_ms) {
    if (port < 1 || port > 65535 || io_timeout_ms < 1 || io_timeout_ms > 10000)
        throw std::invalid_argument("loopback port/timeout is outside supported bounds");
    bool expected = false;
    if (!connection_owned.compare_exchange_strong(expected, true))
        throw std::logic_error("the donor pool connection already has an owner");
    owned_ = true;
    try {
        cp_pool_disconnect();
        const std::string host = loopback_dial_host(port);
        if (host != "127.0.0.1")
            throw std::logic_error("donor connect host escaped the loopback dial");
        if (!cp_pool_connect(host.c_str(), port)) throw std::runtime_error("loopback pool connection failed");
        connected_ = true;
#ifdef _WIN32
        const SOCKET socket = static_cast<SOCKET>(cp_pool_socket());
        const DWORD timeout = static_cast<DWORD>(io_timeout_ms);
        if (setsockopt(socket, SOL_SOCKET, SO_SNDTIMEO, reinterpret_cast<const char*>(&timeout), sizeof(timeout)) != 0 ||
            setsockopt(socket, SOL_SOCKET, SO_RCVTIMEO, reinterpret_cast<const char*>(&timeout), sizeof(timeout)) != 0)
#else
        const int socket = cp_pool_socket();
        if (socket >= FD_SETSIZE) throw std::runtime_error("donor socket exceeds select descriptor bound");
        const timeval timeout{io_timeout_ms / 1000, (io_timeout_ms % 1000) * 1000};
        if (setsockopt(socket, SOL_SOCKET, SO_SNDTIMEO, &timeout, sizeof(timeout)) != 0 ||
            setsockopt(socket, SOL_SOCKET, SO_RCVTIMEO, &timeout, sizeof(timeout)) != 0)
#endif
            throw std::runtime_error("cannot set bounded loopback socket timeouts");
    } catch (...) { close(); throw; }
}

LoopbackPool::~LoopbackPool() { close(); }

void LoopbackPool::close() noexcept {
    if (owned_) {
        cp_pool_disconnect();
        owned_ = false;
        connection_owned.store(false);
    }
    connected_ = authorized_ = gzip_ = false;
    authorize_id_ = 0;
}

void LoopbackPool::authorize(int id, std::string_view wallet, std::string_view worker,
                            std::string_view agent, std::string_view password) {
    if (!connected_ || authorized_ || authorize_id_) throw std::logic_error("authorize requires a fresh connected session");
    if (id <= 0) throw std::invalid_argument("authorize id must be positive");
    ascii_argument(wallet, 255); ascii_argument(worker, 63); ascii_argument(agent, 63);
    ascii_argument(password, 127, true);
    const std::string w(wallet), r(worker), a(agent), p(password);
    authorize_id_ = id;
    if (!cp_pool_send_authorize(id, w.c_str(), r.c_str(), a.c_str(), p.c_str())) {
        close(); throw std::runtime_error("loopback authorize send failed");
    }
}

void LoopbackPool::accept_authorize(const Message& message) {
    if (!connected_ || authorize_id_ <= 0) throw std::logic_error("no authorize request is pending");
    const auto checked = parse_message(message.raw);
    if (checked.kind != MessageKind::response || checked.response.id != authorize_id_)
        throw std::invalid_argument("authorize response does not match the pending id");
    if (!checked.response.accepted) {
        close(); throw std::runtime_error("loopback pool rejected authorization");
    }
    if (!cp_pool_on_authorize_response(checked.raw.c_str()) ||
        (cp_pool_proof_gzip() != 0) != checked.response.gzip) {
        close(); throw std::runtime_error("donor authorize negotiation disagrees with scoped response");
    }
    gzip_ = checked.response.gzip;
    authorized_ = true;
    authorize_id_ = 0;
}

Received LoopbackPool::receive(int timeout_ms) {
    if (timeout_ms < 0 || timeout_ms > 10000) throw std::invalid_argument("receive timeout must be in [0,10000] ms");
    if (!connected_) return {ReceiveStatus::lost, {}};
    std::array<char, max_message_bytes + 1> text{};
    const int status = cp_pool_recv_one(text.data(), text.size(), timeout_ms);
    if (status > 0) return {ReceiveStatus::message, text.data()};
    if (status == 0) return {ReceiveStatus::timeout, {}};
    close();
    return {ReceiveStatus::lost, {}};
}

void LoopbackPool::submit(int id, std::string_view job_id, std::string_view plain_proof_b64) {
    if (!connected_ || !authorized_) throw std::logic_error("submit requires a confirmed authorized session");
    if (id <= 0) throw std::invalid_argument("submit id must be positive");
    ascii_argument(job_id, 127);
    proof_text(plain_proof_b64);
    const std::string job(job_id), plain(plain_proof_b64);
    std::string payload = plain;
    if (gzip_) {
        std::vector<char> encoded(proof::max_proof_b64_bytes + 1, '\0');
        std::array<char, 4096> error{};
        if (cp_proof_gzip_b64(plain.c_str(), encoded.data(), encoded.size(), error.data(), error.size()) != 0)
            throw std::runtime_error(std::string("real proof gzip failed: ") + error.data());
        const auto end = std::find(encoded.begin(), encoded.end(), '\0');
        if (end == encoded.end() || end == encoded.begin()) throw std::runtime_error("gzip encoder returned no terminated payload");
        payload.assign(encoded.begin(), end);
        proof_text(payload);
    }
    if (!cp_pool_send_plain_proof_submit(cp_pool_socket(), id, job.c_str(), payload.c_str(), 0.0)) {
        close(); throw std::runtime_error("loopback proof submit send failed");
    }
}

} // namespace neuro::wire
