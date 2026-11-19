// SPDX-License-Identifier: MIT
#include "neuro/pool_wire.hpp"
#include "cp_json_text.hpp"
#include "cp_proof.h"
#include "cp_util.h"
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include "cp_platform.h"

#include <algorithm>
#include <array>
#include <chrono>
#include <cstddef>
#include <cstdint>
#include <cstdio>
#include <exception>
#include <fstream>
#include <iostream>
#include <optional>
#include <stdexcept>
#include <string>
#include <string_view>
#include <utility>
#include <vector>

#ifdef _WIN32
#include <process.h>
#else
#include <unistd.h>
#endif

#ifndef _WIN32
#include <sys/select.h>
#endif

namespace {
using namespace neuro::wire;
std::size_t checks = 0, rejections = 0;

void require(bool condition, const std::string& message) {
    if (!condition) throw std::runtime_error(message);
    ++checks;
}

template<class Exception = std::invalid_argument, class F>
void rejects(F&& function, const std::string& label) {
    try { function(); }
    catch (const Exception&) { ++rejections; return; }
    throw std::runtime_error(label + " was accepted");
}

std::string target() { std::string value(64, '0'); value[5] = '8'; return value; }
std::string header() { return std::string(152, 'a'); }
std::string notify(std::string job = "job-1", bool with_target = true) {
    return "{\"id\":null,\"method\":\"mining.notify\",\"params\":{\"job_id\":\"" +
        cp_json_escape(job.c_str()) + "\",\"header\":\"" + header() + "\"" +
        (with_target ? ",\"target\":\"" + target() + "\"" : "") + ",\"cert_version\":3}}";
}

std::string changed(std::string text, const std::string& from, const std::string& to) {
    const auto position = text.find(from);
    if (position == std::string::npos) throw std::logic_error("test mutation did not find its field");
    text.replace(position, from.size(), to);
    return text;
}

void parser_cases() {
    const auto good = notify("a\"b\\c");
    auto parsed = parse_message(good);
    require(parsed.kind == MessageKind::notify && parsed.job.job_id == "a\"b\\c", "real notify job-id decoding");
    require(std::all_of(parsed.job.header.begin(), parsed.job.header.end(), [](auto x) { return x == 0xaa; }),
            "complete header must survive donor decoding");
    neuro::proof::Target expected{};
    expected[2] = 8;
    require(parsed.job.target == expected && parsed.job.cert_version == 3, "target and cert3 decoding");
    require(parsed.job.connection == 0 && !parsed.job.gzip, "parser must not invent session context");
    parsed = parse_message(changed(good, "\"cert_version\":3", "\"cert_version\":2"));
    require(parsed.job.cert_version == 2, "legacy certificate remains explicit");
    const double difficulty = 65535.0 / 134217728.0;
    parsed = parse_message(notify("fallback", false), difficulty);
    require(parsed.job.target == expected, "actual donor difficulty fallback must equal the independent 2^235 target");
    parsed = parse_message("{\"method\":\"mining.set_difficulty\",\"params\":[ 26000 ]}");
    require(parsed.kind == MessageKind::difficulty && parsed.difficulty == 26000, "singleton difficulty");
    require(parse_message("{\"method\":\"mining.set_difficulty\",\"params\":0.5}").difficulty == 0.5,
            "fractional scalar difficulty");
    const auto top = "{\"method\":\"mining.notify\",\"job_id\":\"top\",\"header\":\"" + header() +
        "\",\"target\":\"" + target() + "\",\"cert_version\":3}";
    require(parse_message(top).job.job_id == "top", "top-level notify fields");
    const auto outside_cert = changed(changed(notify(), ",\"cert_version\":3", ""),
                                      "\"id\":null", "\"id\":null,\"cert_version\":3");
    require(parse_message(outside_cert).job.cert_version == 3, "scoped top-level certificate");
    const auto tail_change = changed(notify(), header(), std::string(150, 'a') + "ab");
    require(parse_message(tail_change).job.header.back() == 0xab, "header bytes beyond donor's historical short identity");

    const std::vector<std::string> invalid = {
        "", "[]", "{\"a\":1,}", "{\"a\":1 \"b\":2}", "{\"a\":01}", "{\"a\":NaN}",
        "{\"a\":1e}", "{\"a\":truee}", "{a:1}", "{\"a\":[1}}", "{}{}", "{\"a\":\"\\u0061\"}",
        "{\"a\":\"unterminated}", "{\"a\":1,\"a\":2}", "{\"extra\":{\"x\":1,\"x\":2}}",
        changed(notify(), "\"method\":\"mining.notify\"", "\"method\":\"mining.notify\",\"method\":\"other\""),
        changed(notify(), "\"job_id\":\"job-1\"", "\"job_id\":\"\""),
        changed(notify(), "\"job_id\":\"job-1\"", "\"job_id\":null"),
        notify(std::string(128, 'x')),
        changed(notify(), "\"header\":\"" + header() + "\"", "\"header\":\"aa\""),
        changed(notify(), header(), std::string(152, 'z')),
        changed(notify(), "\"target\":\"" + target() + "\"", "\"target\":null"),
        changed(notify(), target(), std::string(64, 'f')),
        changed(notify(), "\"cert_version\":3", "\"cert_version\":1"),
        changed(notify(), "\"cert_version\":3", "\"cert_version\":3.0"),
        changed(notify(), "\"cert_version\":3", "\"cert_version\":\"3\""),
        changed(notify(), ",\"cert_version\":3", ""),
        changed(notify(), "\"id\":null", "\"id\":null,\"cert_version\":2"),
        "{\"method\":\"mining.notify\",\"params\":[\"j\",\"" + header() + "\",\"" + target() + "\"]}",
        "{\"method\":\"mining.set_difficulty\",\"params\":[1,2]}",
        "{\"method\":\"mining.set_difficulty\",\"params\":0}",
        "{\"method\":\"mining.set_difficulty\",\"params\":-1}",
        "{\"method\":\"mining.set_difficulty\",\"params\":1e999}",
        "{\"method\":\"mining.set_difficulty\",\"params\":\"2\"}",
        "{\"id\":1.5,\"result\":true}", "{\"id\":\"1x\",\"result\":true}",
        "{\"id\":-1,\"result\":true}", "{\"id\":2147483648,\"result\":true}",
        "{\"id\":1,\"method\":\"x\",\"result\":true}",
        "{\"id\":1,\"result\":true,\"type\":2}",
    };
    for (const auto& text : invalid) rejects([&] { (void)parse_message(text); }, "malformed/scoped pool message");
    rejects([&] { (void)parse_message(notify("missing", false)); }, "unconfirmed fallback difficulty");
    rejects([&] { (void)parse_message(changed(notify(), target(), std::string(64, 'z')), difficulty); },
            "invalid explicit target must not fall back");
    rejects([&] { (void)parse_message(std::string(max_message_bytes + 1, ' ')); }, "oversize message");
    std::string deep = "0";
    for (int i = 0; i < 20; ++i) deep = "[" + deep + "]";
    rejects([&] { (void)parse_message("{\"x\":" + deep + "}"); }, "excess nesting");
    std::string many = "{\"x\":[0";
    for (int i = 0; i < 300; ++i) many += ",0";
    many += "]}";
    rejects([&] { (void)parse_message(many); }, "excess values");

    parsed = parse_message("{\"id\":7,\"result\":true,\"error\":null,\"type\":\"v2\"}");
    require(parsed.kind == MessageKind::response && parsed.response.id == 7 && parsed.response.accepted &&
            parsed.response.gzip, "strict positive scoped response");
    require(parse_message("{\"id\":\"7\",\"result\":true}").response.id == 7, "decimal string response id");
    require(!parse_message("{\"id\":7,\"result\":true,\"error\":{\"code\":1}}").response.accepted,
            "non-null error defeats true result");
    require(!parse_message("{\"id\":7,\"result\":false}").response.accepted, "explicit rejection");
    require(!parse_message("{\"id\":7,\"result\":{\"type\":\"v2\"}}").response.accepted,
            "nonboolean result does not authorize this bounded profile");
    require(!parse_message("{\"id\":7,\"result\":true,\"extra\":{\"type\":\"v2\"}}").response.gzip,
            "nested type must not negotiate encoding");
    require(parse_message("{\"method\":\"unknown\",\"params\":{\"method\":\"mining.notify\"}}").kind == MessageKind::unknown,
            "nested method must not dispatch a job");
}

class Socket {
public:
    explicit Socket(cp_sock_t value = CP_INVALID_SOCK) : value_(value) {}
    ~Socket() { close(); }
    Socket(const Socket&) = delete;
    Socket& operator=(const Socket&) = delete;
    Socket(Socket&& other) noexcept : value_(std::exchange(other.value_, CP_INVALID_SOCK)) {}
    cp_sock_t get() const { return value_; }
    void close() { if (value_ != CP_INVALID_SOCK) { ::CP_SOCK_CLOSE(value_); value_ = CP_INVALID_SOCK; } }
private:
    cp_sock_t value_;
};

void readable(cp_sock_t socket, int milliseconds = 2000) {
    fd_set ready;
    FD_ZERO(&ready);
#ifndef _WIN32
    if (socket < 0 || socket >= FD_SETSIZE) throw std::runtime_error("test socket exceeds select bound");
#endif
    FD_SET(socket, &ready);
    timeval timeout{milliseconds / 1000, (milliseconds % 1000) * 1000};
#ifdef _WIN32
    const int count = ::select(0, &ready, nullptr, nullptr, &timeout);
#else
    const int count = ::select(socket + 1, &ready, nullptr, nullptr, &timeout);
#endif
    if (count <= 0) throw std::runtime_error("test loopback peer timed out");
}

void send_text(cp_sock_t socket, std::string_view text) {
    while (!text.empty()) {
        const auto length = static_cast<int>((std::min)(text.size(), std::size_t(65536)));
#ifdef MSG_NOSIGNAL
        const auto sent = ::send(socket, text.data(), length, MSG_NOSIGNAL);
#else
        const auto sent = ::send(socket, text.data(), length, 0);
#endif
        if (sent <= 0) throw std::runtime_error("test peer send failed");
        text.remove_prefix(static_cast<std::size_t>(sent));
    }
}

std::string read_line(cp_sock_t socket) {
    const auto deadline = std::chrono::steady_clock::now() + std::chrono::seconds(2);
    std::string text;
    for (;;) {
        const auto left = std::chrono::duration_cast<std::chrono::milliseconds>(deadline - std::chrono::steady_clock::now()).count();
        if (left <= 0) throw std::runtime_error("test serializer capture timed out");
        readable(socket, static_cast<int>(left));
        char c = 0;
        if (::recv(socket, &c, 1, 0) != 1) throw std::runtime_error("test peer closed before newline");
        if (c == '\n') return text;
        text.push_back(c);
        if (text.size() > neuro::proof::max_proof_b64_bytes + 1024) throw std::runtime_error("test submit line exceeded bound");
    }
}

class Listener {
public:
    Listener() : socket_(::socket(AF_INET, SOCK_STREAM, 0)) {
        if (socket_.get() == CP_INVALID_SOCK) throw std::runtime_error("cannot create test loopback socket");
        sockaddr_in address{};
        address.sin_family = AF_INET;
        address.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
        address.sin_port = 0;
        if (::bind(socket_.get(), reinterpret_cast<sockaddr*>(&address), sizeof(address)) != 0 ||
            ::listen(socket_.get(), 4) != 0) throw std::runtime_error("cannot bind/listen on loopback");
#ifdef _WIN32
        int length = sizeof(address);
#else
        socklen_t length = sizeof(address);
#endif
        if (::getsockname(socket_.get(), reinterpret_cast<sockaddr*>(&address), &length) != 0)
            throw std::runtime_error("cannot discover test port");
        port = ntohs(address.sin_port);
    }
    Socket accept_peer() {
        readable(socket_.get());
        Socket peer(::accept(socket_.get(), nullptr, nullptr));
        if (peer.get() == CP_INVALID_SOCK) throw std::runtime_error("test peer accept failed");
#ifdef _WIN32
        const DWORD timeout = 1000;
        if (::setsockopt(peer.get(), SOL_SOCKET, SO_SNDTIMEO,
                         reinterpret_cast<const char*>(&timeout), sizeof(timeout)) != 0)
#else
        const timeval timeout{1, 0};
        if (::setsockopt(peer.get(), SOL_SOCKET, SO_SNDTIMEO, &timeout, sizeof(timeout)) != 0)
#endif
            throw std::runtime_error("cannot bound test peer sends");
        return peer;
    }
    int port = 0;
private:
    Socket socket_;
};

Message receive_message(LoopbackPool& pool) {
    const auto received = pool.receive(1000);
    require(received.status == ReceiveStatus::message, "expected complete donor-framed pool message");
    return parse_message(received.text);
}

std::string read_string(const char* scope, const char* key, std::size_t maximum = 4096) {
    std::vector<char> output(maximum + 1, '\0');
    if (!cp_json_member_str(scope, key, output.data(), output.size()))
        throw std::runtime_error("captured donor request is missing expected string field");
    return output.data();
}

std::string fixture_path(const char* name) {
#ifdef _WIN32
    const int pid = _getpid();
#else
    const int pid = static_cast<int>(::getpid());
#endif
    return std::string(name) + "-" + std::to_string(pid) + ".txt";
}

class TempFile {
public:
    explicit TempFile(std::string path) : path_(std::move(path)) {}
    ~TempFile() { std::remove(path_.c_str()); }
    TempFile(const TempFile&) = delete;
    TempFile& operator=(const TempFile&) = delete;
    const std::string& path() const { return path_; }
    void write(std::string_view body) const {
        std::ofstream out(path_, std::ios::binary | std::ios::trunc);
        if (!out.write(body.data(), static_cast<std::streamsize>(body.size())))
            throw std::runtime_error("cannot write pool authorization fixture");
    }
private:
    std::string path_;
};

std::string authorization(std::string_view endpoint) {
    return "neuro.pool_endpoint_authorization.v1\nendpoint=" + std::string(endpoint) + "\n";
}

void endpoint_cases() {
    // Classification only. None of these hosts is passed to the donor connect.
    require(assess_endpoint("127.0.0.1", 9) == EndpointStatus::loopback_dialable, "numeric loopback is dialable");
    require(loopback_dial_host(9) == "127.0.0.1", "dial host is the numeric loopback literal");
    require(assess_endpoint("8.8.8.8", 53) == EndpointStatus::external_not_named, "unnamed public address is not dialable");
    require(assess_endpoint("127.0.0.2", 9) == EndpointStatus::external_not_named, "other loopback numbers are not the dial host");
    require(assess_endpoint("localhost", 9) == EndpointStatus::external_not_named, "hostname is not resolved or dialed");
    const auto named = parse_pool_authorization(authorization("203.0.113.9:3333"));
    require(named.host == "203.0.113.9" && named.port == 3333, "authorization names one endpoint");
    require(assess_endpoint("203.0.113.9", 3333, named) == EndpointStatus::external_named_undialable,
            "named public endpoint stays undialable");
    require(assess_endpoint("203.0.113.9", 3334, named) == EndpointStatus::external_not_named,
            "a different port is not the named endpoint");
    require(assess_endpoint("198.51.100.1", 3333, named) == EndpointStatus::external_not_named,
            "a different host is not the named endpoint");
    require(assess_endpoint("127.0.0.1", 3333, named) == EndpointStatus::loopback_dialable,
            "naming an external endpoint does not change the loopback dial");
    require(!load_pool_authorization("").has_value(), "empty authorization path reads no file");
    rejects([&] { (void)loopback_dial_host(0); }, "dial host rejects port 0");
    rejects([&] { (void)loopback_dial_host(65536); }, "dial host rejects port 65536");
    rejects([&] { (void)assess_endpoint("::1", 9); }, "ipv6 text is not a dial host");
    rejects([&] { (void)assess_endpoint("127.0.0.1 ", 9); }, "padded loopback is not the dial host");
    rejects([&] { (void)assess_endpoint("127.0.0.1/x", 9); }, "loopback with a path is not the dial host");
    rejects([&] { (void)parse_pool_authorization(authorization("127.0.0.1:9")); }, "authorization must not rename loopback");
    rejects([&] { (void)parse_pool_authorization(authorization("203.0.113.9:0333")); }, "authorization port must be canonical");
    rejects([&] { (void)parse_pool_authorization(authorization("203.0.113.9:3333") + "endpoint=198.51.100.1:7\n"); },
            "authorization names one endpoint only");
    rejects([&] { (void)parse_pool_authorization(authorization("203.0.113.9:3333") + "wallet=rejected\n"); },
            "authorization must not carry a wallet");
    rejects([&] { (void)parse_pool_authorization("neuro.pool_endpoint_authorization.v1\nendpoint=203.0.113.9:3333\npassword=x\n"); },
            "authorization must not carry a password");
    const auto crlf = parse_pool_authorization("neuro.pool_endpoint_authorization.v1\r\nendpoint=203.0.113.9:3333\r\n");
    require(crlf.host == "203.0.113.9" && crlf.port == 3333, "CRLF authorization parses");
    TempFile file(fixture_path("neuro-pool-auth"));
    file.write(authorization("203.0.113.9:3333"));
    const auto loaded = load_pool_authorization(file.path());
    require(loaded.has_value() &&
            assess_endpoint(loaded->host, loaded->port, loaded) == EndpointStatus::external_named_undialable,
            "loaded authorization still does not dial");
    rejects([&] { (void)load_pool_authorization(fixture_path("neuro-pool-auth-missing")); }, "missing authorization file");
    TempFile oversized(fixture_path("neuro-pool-auth-large"));
    oversized.write(std::string(513, 'a'));
    rejects([&] { (void)load_pool_authorization(oversized.path()); }, "oversized authorization file");
    TempFile secret(fixture_path("neuro-pool-auth-secret"));
    secret.write(authorization("203.0.113.9:3333") + "worker=no\n");
    rejects([&] { (void)load_pool_authorization(secret.path()); }, "authorization file must not name a worker");
#ifndef _WIN32
    TempFile link_target(fixture_path("neuro-pool-auth-target"));
    link_target.write(authorization("203.0.113.9:3333"));
    const auto link = fixture_path("neuro-pool-auth-link");
    std::remove(link.c_str());
    require(::symlink(link_target.path().c_str(), link.c_str()) == 0, "authorization symlink fixture");
    TempFile unlink_link(link);
    rejects([&] { (void)load_pool_authorization(link); }, "symlink authorization is not a regular file");
#endif
}

void socket_cases() {
    Listener listener;
    {
        LoopbackPool pool(listener.port, 250);
        auto peer = listener.accept_peer();
        rejects<std::logic_error>([&] { LoopbackPool duplicate(listener.port); }, "concurrent donor connection owner");
        require(pool.receive(5).status == ReceiveStatus::timeout, "idle receive is a timeout, not disconnect");
        rejects<std::logic_error>([&] { pool.submit(9, "job", "AAECAwQ="); }, "unauthorized submission");
        rejects([&] { pool.authorize(0, "offline", "worker", "neuro-test"); }, "invalid authorize id");
        pool.authorize(7, "offline\"wallet", "worker\\name", "neuro-test", "x\"password");
        const auto auth = read_line(peer.get());
        require(parse_message(auth).method == "mining.authorize", "actual donor authorize method");
        const char* params = cp_json_member(auth.c_str(), "params");
        require(params && read_string(params, "wallet") == "offline\"wallet" &&
                read_string(params, "worker") == "worker\\name" &&
                read_string(params, "password") == "x\"password" && read_string(params, "type") == "v2",
                "actual authorize serializer must escape fields and offer gzip");
        const std::string answer = "{\"id\":7,\"result\":true,\"type\":\"v2\",\"error\":null}";
        send_text(peer.get(), std::string_view(answer).substr(0, 17));
        require(pool.receive(5).status == ReceiveStatus::timeout, "partial JSON must stay buffered");
        send_text(peer.get(), answer.substr(17) + notify("a\"b\\c"));
        const auto authorized = receive_message(pool);
        rejects([&] { pool.accept_authorize(parse_message("{\"id\":8,\"result\":true}")); }, "wrong authorize id");
        pool.accept_authorize(authorized);
        require(pool.authorized() && pool.gzip(), "real authorize negotiation enables gzip only after exact reply");
        require(receive_message(pool).job.job_id == "a\"b\\c", "concatenated notify must survive buffered ACK");
        rejects([&] { pool.submit(9, "job", "AA\"="); }, "unsafe base64 injection");
        const std::string plain = "AAECAwQ="; // Transport payload only, not claimed to be a valid proof.
        pool.submit(9, "a\"b\\c", plain);
        const auto submitted = read_line(peer.get());
        require(parse_message(submitted).method == "mining.submit", "actual submit serializer method");
        params = cp_json_member(submitted.c_str(), "params");
        std::array<char, 1024> expected_gzip{}, error{};
        require(cp_proof_gzip_b64(plain.c_str(), expected_gzip.data(), expected_gzip.size(), error.data(), error.size()) == 0,
                "real Rust gzip function must be linked");
        require(read_string(params, "job_id") == "a\"b\\c" &&
                read_string(params, "plain_proof") == expected_gzip.data() && cp_json_num(params, "hs") == 0,
                "gzip submit must preserve escaped identity and zero unmeasured MAC rate");
        require(cp_json_num(submitted.c_str(), "id") == 9, "request ID survives actual serializer");
        send_text(peer.get(), "{\"id\":9,\"result\":false,\"error\":{\"code\":23}}");
        require(!receive_message(pool).response.accepted, "negative submit acknowledgment stays negative");
        peer.close();
        require(pool.receive(1000).status == ReceiveStatus::lost && !pool.authorized(), "EOF invalidates local connection state");
    }
    {
        LoopbackPool pool(listener.port);
        auto peer = listener.accept_peer();
        pool.authorize(10, "offline", "worker", "neuro-test");
        (void)read_line(peer.get());
        send_text(peer.get(), "{\"id\":10,\"result\":true}");
        pool.accept_authorize(receive_message(pool));
        require(pool.authorized() && !pool.gzip(), "reconnect must reset previous gzip negotiation");
        pool.submit(11, "plain-job", "AAECAwQ=");
        const auto line = read_line(peer.get());
        require(read_string(cp_json_member(line.c_str(), "params"), "plain_proof") == "AAECAwQ=",
                "plain session must submit unchanged base64");
    }
    {
        LoopbackPool pool(listener.port);
        auto peer = listener.accept_peer();
        pool.authorize(12, "offline", "worker", "neuro-test");
        (void)read_line(peer.get());
        send_text(peer.get(), "{\"id\":12,\"result\":false}");
        const auto answer = receive_message(pool);
        rejects<std::runtime_error>([&] { pool.accept_authorize(answer); }, "rejected authorize");
        require(!pool.authorized() && pool.receive(0).status == ReceiveStatus::lost, "rejected auth closes connection");
    }
    {
        LoopbackPool pool(listener.port);
        auto peer = listener.accept_peer();
        pool.authorize(13, "offline", "worker", "neuro-test");
        (void)read_line(peer.get());
        send_text(peer.get(), "{\"id\":13,\"result\":true,\"extra\":{\"type\":\"v2\"}}");
        const auto answer = receive_message(pool);
        rejects<std::runtime_error>([&] { pool.accept_authorize(answer); }, "nested donor encoding decoy");
        require(!pool.authorized(), "donor/scoped negotiation mismatch must not authorize");
    }
    {
        LoopbackPool pool(listener.port);
        auto peer = listener.accept_peer();
        send_text(peer.get(), "{\"broken\":[1}}");
        require(pool.receive(1000).status == ReceiveStatus::lost, "malformed donor frame closes the connection");
    }
    rejects([&] { LoopbackPool pool(0); }, "invalid port");
    rejects([&] { LoopbackPool pool(listener.port, 10001); }, "unbounded socket timeout");
}

} // namespace

int main() {
    try {
        endpoint_cases();
        if (cp_net_init() != 0) throw std::runtime_error("cannot initialize test socket platform");
        parser_cases();
        socket_cases();
        std::cout << "PASS bounded pool wire: positive_checks=" << checks << " rejected_cases=" << rejections
                  << " real_loopback_connections=5 donor_notify_parser=used donor_serializers=used real_gzip=used"
                  << " external_dial=refused\n";
        return 0;
    } catch (const std::exception& error) {
        std::cerr << "FAIL bounded pool wire: " << error.what() << '\n';
        return 1;
    }
}
