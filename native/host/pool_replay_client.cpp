// SPDX-License-Identifier: MIT
// A bounded loopback client using real donor TCP/wire and genuine Pearl proofs.
#include "neuro/mining_job.hpp"
#include "neuro/pool_wire.hpp"
#ifdef NEURO_REPLAY_CUDA
#include "neuro/mining_cuda.hpp"
#endif

#include <atomic>
#include <chrono>
#include <cstdio>
#include <filesystem>
#include <fstream>
#include <future>
#include <iostream>
#include <map>
#include <memory>
#include <mutex>
#include <optional>
#include <sstream>
#include <stdexcept>
#include <string>
#include <thread>

namespace {
namespace host = neuro::host;
namespace wire = neuro::wire;
using Clock = std::chrono::steady_clock;
using namespace std::chrono_literals;

std::string quote(const std::string& value) {
    constexpr char digits[] = "0123456789abcdef";
    std::string text = "\"";
    for (unsigned char ch : value) {
        if (ch == '"' || ch == '\\') { text += '\\'; text += static_cast<char>(ch); }
        else if (ch < 32) { text += "\\u00"; text += digits[ch >> 4]; text += digits[ch & 15]; }
        else text += static_cast<char>(ch);
    }
    return text + '"';
}

std::mutex event_mutex;
std::uint64_t event_sequence = 0;
void event(const char* name, const std::string& fields = "") {
    std::lock_guard<std::mutex> lock(event_mutex);
    const auto text = "NEURO_EVENT {\"event\":" + quote(name) +
        ",\"sequence\":" + std::to_string(++event_sequence) + fields + "}\n";
    // A single stdio write prevents interleaving with the donor's printf logs.
    std::fwrite(text.data(), 1, text.size(), stdout);
    std::fflush(stdout);
}

std::string identity(const host::Ticket& ticket) {
    return ",\"connection\":" + std::to_string(ticket.job ? ticket.job->connection : 0) +
           ",\"generation\":" + std::to_string(ticket.generation) +
           ",\"job_id\":" + quote(ticket.job ? ticket.job->job_id : "");
}

const char* stage_name(host::Stage stage) {
    switch (stage) {
    case host::Stage::before_prepare: return "before_prepare";
    case host::Stage::after_prepare: return "after_prepare";
    case host::Stage::before_compute: return "before_compute";
    case host::Stage::after_compute: return "after_compute";
    case host::Stage::before_proof: return "before_proof";
    case host::Stage::after_proof: return "after_proof";
    case host::Stage::after_verify: return "after_verify";
    }
    return "unknown";
}

int integer(const std::string& value, int low, int high, const std::string& name) {
    std::size_t used = 0;
    const int result = std::stoi(value, &used);
    if (used != value.size() || result < low || result > high)
        throw std::invalid_argument(name + " outside its bounded range");
    return result;
}

host::Seed seed_from_hex(const std::string& text) {
    if (text.size() != 64) throw std::invalid_argument("Seed must contain 64 hex characters");
    auto digit = [](char c) -> int {
        if (c >= '0' && c <= '9') return c - '0';
        if (c >= 'a' && c <= 'f') return c - 'a' + 10;
        if (c >= 'A' && c <= 'F') return c - 'A' + 10;
        throw std::invalid_argument("Seed contains non-hex characters");
    };
    host::Seed result{};
    for (std::size_t i = 0; i < result.size(); ++i)
        result[i] = static_cast<std::uint8_t>(digit(text[i * 2]) * 16 + digit(text[i * 2 + 1]));
    return result;
}

struct Options {
    int port = 0, deadline_ms = 8000, max_connections = 2, max_submissions = 1, device = 0;
    neuro::fullk::Shape shape{128, 128};
    std::filesystem::path report = "artifacts/pool-client.json";
    std::string backend = "cpu", pause_stage, pause_job_id;
    host::Seed seed{};
};

Options options(int argc, char** argv) {
    Options result;
    // A repeatable synthetic attempt, never a live mining seed policy.
    for (std::size_t i = 0; i < result.seed.size(); ++i)
        result.seed[i] = static_cast<std::uint8_t>((i * i * 13 + i * 17 + 29) & 255);
    std::map<std::string, bool> seen;
    for (int i = 1; i < argc; i += 2) {
        const std::string flag = argv[i];
        if (i + 1 >= argc || !seen.emplace(flag, true).second)
            throw std::invalid_argument("Missing value or duplicate option: " + flag);
        const std::string value = argv[i + 1];
        if (flag == "--port") result.port = integer(value, 1, 65535, flag);
        else if (flag == "--deadline-ms") result.deadline_ms = integer(value, 500, 30000, flag);
        else if (flag == "--max-connections") result.max_connections = integer(value, 1, 4, flag);
        else if (flag == "--max-submissions") result.max_submissions = integer(value, 1, 8, flag);
        else if (flag == "--rows") result.shape.rows = integer(value, 128, 256, flag);
        else if (flag == "--cols") result.shape.cols = integer(value, 128, 256, flag);
        else if (flag == "--seed-hex") result.seed = seed_from_hex(value);
        else if (flag == "--report") result.report = value;
        else if (flag == "--backend") result.backend = value;
        else if (flag == "--device") result.device = integer(value, 0, 15, flag);
        else if (flag == "--pause-stage") result.pause_stage = value;
        else if (flag == "--pause-job-id") result.pause_job_id = value;
        else throw std::invalid_argument("Unknown option: " + flag);
    }
    if (!result.port || result.shape.rows % 128 || result.shape.cols % 128)
        throw std::invalid_argument("Replay needs --port and 128/256 matrix dimensions");
    if (result.backend != "cpu" && result.backend != "cuda")
        throw std::invalid_argument("Backend must be cpu or cuda");
#ifndef NEURO_REPLAY_CUDA
    if (result.backend == "cuda") throw std::invalid_argument("CUDA backend is not linked in this executable");
#endif
    if (seen.count("--device") && result.backend != "cuda")
        throw std::invalid_argument("--device requires --backend cuda");
    if (!result.pause_stage.empty() && result.pause_stage != "before_compute" &&
        result.pause_stage != "after_compute" && result.pause_stage != "after_proof" &&
        result.pause_stage != "after_verify")
        throw std::invalid_argument("Unsupported replay checkpoint stage");
    if (result.pause_stage.empty() != result.pause_job_id.empty())
        throw std::invalid_argument("A replay checkpoint requires both --pause-stage and --pause-job-id");
    return result;
}

struct Counts {
    std::size_t connections = 0, jobs_received = 0, jobs_started = 0, attempts_finished = 0;
    std::size_t cancelled = 0, misses = 0, errors = 0, duplicates = 0, protocol_errors = 0;
    std::size_t unmatched_responses = 0, submissions = 0, accepted = 0, rejected = 0, pending_lost = 0;
    std::string json(std::size_t checkpoints) const {
        std::ostringstream out;
        out << "{";
#define COUNT(name) out << "\"" #name "\":" << name << ",";
        COUNT(connections) COUNT(jobs_received) COUNT(jobs_started) COUNT(attempts_finished)
        COUNT(cancelled) COUNT(misses) COUNT(errors) COUNT(duplicates) COUNT(protocol_errors)
        COUNT(unmatched_responses) COUNT(submissions) COUNT(accepted) COUNT(rejected) COUNT(pending_lost)
#undef COUNT
        out << "\"checkpoints\":" << checkpoints << "}";
        return out.str();
    }
};

struct Pending { host::Ticket ticket; Clock::time_point sent; };

int run(const Options& opt) {
    const auto started = Clock::now();
    const auto deadline = started + std::chrono::milliseconds(opt.deadline_ms);
    host::Coordinator coordinator;
    host::Ticket latest;
    std::future<host::Result> worker;
    std::uint64_t started_generation = 0, connection = 0;
    std::unique_ptr<wire::LoopbackPool> pool;
    std::optional<double> difficulty;
    std::map<int, Pending> pending;
    Counts counts;
    std::atomic<std::size_t> checkpoints{0}, cpu_computations{0}, gpu_computations{0};
    std::atomic<bool> pause_used{false};
    int attempts_to_connect = 0, next_id = 1, authorize_id = 0;
    Clock::time_point authorize_sent{}, last_activity{};
    bool completed = false, fatal = false;
    std::string reason = "deadline", last_error;

    auto disconnect = [&](const std::string& why) {
        coordinator.disconnect(connection);
        latest = {};
        for (const auto& item : pending) {
            ++counts.pending_lost;
            event("share_unresolved", identity(item.second.ticket) + ",\"id\":" + std::to_string(item.first));
        }
        pending.clear();
        if (pool) { pool->close(); pool.reset(); }
        difficulty.reset();
        authorize_id = 0;
        event("disconnected", ",\"connection\":" + std::to_string(connection) + ",\"reason\":" + quote(why));
    };

    auto on_message = [&](const std::string& line) {
        wire::Message message;
        try { message = wire::parse_message(line, difficulty); }
        catch (const std::invalid_argument& error) {
            ++counts.protocol_errors;
            event("rejected_message", ",\"connection\":" + std::to_string(connection) + ",\"error\":" + quote(error.what()));
            return;
        }
        last_activity = Clock::now();
        if (message.kind == wire::MessageKind::response) {
            const auto id = message.response.id;
            if (authorize_id && id == authorize_id) {
                if (!message.response.accepted) { ++counts.protocol_errors; disconnect("authorize_rejected"); return; }
                try { pool->accept_authorize(message); }
                catch (const std::exception& error) { ++counts.protocol_errors; disconnect(error.what()); return; }
                authorize_id = 0;
                event("authorized", ",\"connection\":" + std::to_string(connection) +
                      ",\"gzip\":" + (pool->gzip() ? "true" : "false"));
                return;
            }
            const auto found = pending.find(id);
            if (found == pending.end() || found->second.ticket.job->connection != connection) {
                ++counts.unmatched_responses;
                event("unmatched_response", ",\"connection\":" + std::to_string(connection) + ",\"id\":" + std::to_string(id));
                return;
            }
            if (message.response.accepted) ++counts.accepted;
            else ++counts.rejected;
            event("share_response", identity(found->second.ticket) + ",\"id\":" + std::to_string(id) +
                  ",\"accepted\":" + (message.response.accepted ? "true" : "false"));
            pending.erase(found);
            return;
        }
        if (!pool->authorized()) {
            ++counts.protocol_errors;
            event("rejected_message", ",\"connection\":" + std::to_string(connection) + ",\"error\":\"work before authorization\"");
            return;
        }
        if (message.kind == wire::MessageKind::difficulty) {
            difficulty = message.difficulty;
            event("difficulty", ",\"connection\":" + std::to_string(connection));
        } else if (message.kind == wire::MessageKind::notify) {
            message.job.shape = opt.shape;
            message.job.gzip = pool->gzip();
            message.job.connection = connection;
            host::Ticket incoming;
            try { incoming = coordinator.publish(std::move(message.job)); }
            catch (const std::invalid_argument& error) {
                ++counts.protocol_errors;
                event("rejected_message", ",\"error\":" + quote(error.what()));
                return;
            }
            if (latest.job && incoming.generation == latest.generation) {
                ++counts.duplicates;
                event("duplicate_job", identity(incoming));
            } else {
                ++counts.jobs_received;
                latest = incoming;
                event("job_received", identity(incoming));
            }
        } else event("ignored_message", ",\"connection\":" + std::to_string(connection));
    };

    auto drain = [&](int initial_timeout) {
        // Drain complete buffered/coalesced messages before deciding to send.
        // A processed notify is the local publication order; in-flight remote
        // changes can still yield a legitimate stale response from the pool.
        for (int index = 0; pool && index < 64; ++index) {
            const auto received = pool->receive(index == 0 ? initial_timeout : 1);
            if (received.status == wire::ReceiveStatus::lost) { disconnect("connection_lost_or_bad_frame"); return; }
            if (received.status == wire::ReceiveStatus::timeout) return;
            on_message(received.text);
        }
    };

    host::Compute compute = [&](const neuro::fullk::Input& input) {
#ifdef NEURO_REPLAY_CUDA
        if (opt.backend == "cuda") {
            auto output = host::compute_cuda(input, opt.device);
            ++gpu_computations;
            return output;
        }
#endif
        auto output = neuro::fullk::reference(input);
        ++cpu_computations;
        return output;
    };
    host::Checkpoint checkpoint = [&](host::Stage stage, const host::Ticket& ticket) {
        const std::string stage_text = stage_name(stage);
        event("stage", identity(ticket) + ",\"stage\":" + quote(stage_text));
        if (stage_text == opt.pause_stage && ticket.job->job_id == opt.pause_job_id && !pause_used.exchange(true)) {
            ++checkpoints;
            event("checkpoint", identity(ticket) + ",\"stage\":" + quote(stage_text));
            const auto limit = Clock::now() + 3s;
            // Deliberate replay checkpoint. Main continues processing TCP;
            // only invalidation releases this worker, never a fake pool RPC.
            while (!coordinator.cancelled(ticket) && Clock::now() < limit)
                std::this_thread::sleep_for(1ms);
            if (!coordinator.cancelled(ticket))
                throw std::runtime_error("replay checkpoint timed out waiting for job cancellation");
        }
    };

    try {
        while (Clock::now() < deadline && !fatal) {
            if (!pool) {
                if (attempts_to_connect >= opt.max_connections) { reason = "connection_limit"; break; }
                ++attempts_to_connect;
                ++connection;
                try {
                    pool = std::make_unique<wire::LoopbackPool>(opt.port, 500);
                    ++counts.connections;
                    event("connected", ",\"connection\":" + std::to_string(connection));
                    authorize_id = next_id++;
                    pool->authorize(authorize_id, "offline-wallet", "replay-worker", "neuro-replay", "x");
                    authorize_sent = last_activity = Clock::now();
                } catch (const std::exception& error) { disconnect(error.what()); continue; }
            }
            drain(5);
            if (pool && authorize_id && Clock::now() - authorize_sent > 3s)
                disconnect("authorize_timeout");
            if (pool && !pending.empty() && Clock::now() - pending.begin()->second.sent > 3s)
                disconnect("submit_response_timeout");
            if (pool && pool->authorized() && !latest.job && Clock::now() - last_activity > 3s)
                disconnect("first_job_timeout");

            if (worker.valid() && worker.wait_for(0ms) == std::future_status::ready) {
                const auto result = worker.get();
                ++counts.attempts_finished;
                event("attempt_finished", identity(result.ticket) + ",\"status\":" + quote(host::status_name(result.status)) +
                      ",\"tiles_scanned\":" + std::to_string(result.tiles_scanned));
                if (result.status == host::Status::cancelled) ++counts.cancelled;
                else if (result.status == host::Status::miss) ++counts.misses;
                else if (result.status == host::Status::error) {
                    ++counts.errors; fatal = true; reason = "fatal_error"; last_error = result.error;
                } else {
                    drain(1);
                    if (!pool || !pool->authorized() || !coordinator.current(result.ticket)) {
                        ++counts.cancelled;
                        event("stale_discarded", identity(result.ticket));
                    } else if (counts.submissions < static_cast<std::size_t>(opt.max_submissions)) {
                        const int id = next_id++;
                        try {
                            const bool sent = coordinator.commit(result, [&](const host::Share& share) {
                                pending.emplace(id, Pending{share.ticket, Clock::now()});
                                pool->submit(id, share.ticket.job->job_id, share.proof_b64);
                                return true;
                            });
                            if (sent) {
                                ++counts.submissions;
                                event("submitted", identity(result.ticket) + ",\"id\":" + std::to_string(id));
                            } else {
                                ++counts.cancelled;
                                event("stale_discarded", identity(result.ticket));
                            }
                        } catch (const std::exception& error) { disconnect(error.what()); }
                    }
                }
            }
            if (counts.submissions >= static_cast<std::size_t>(opt.max_submissions) && pending.empty()) {
                // Lost responses remain unknown, never accepted/rejected. They
                // cannot satisfy the normal completion gate after reconnect.
                if (counts.accepted + counts.rejected >= static_cast<std::size_t>(opt.max_submissions)) {
                    completed = true; reason = "submission_limit"; break;
                }
            }
            if (pool && pool->authorized() && latest.job && !worker.valid() &&
                started_generation != latest.generation &&
                counts.submissions < static_cast<std::size_t>(opt.max_submissions) && !fatal) {
                const auto ticket = latest;
                started_generation = ticket.generation;
                ++counts.jobs_started;
                worker = std::async(std::launch::async, [&, ticket] {
                    return coordinator.run_one(ticket, opt.seed, compute, checkpoint);
                });
            }
        }
    } catch (const std::exception& error) {
        ++counts.errors; fatal = true; completed = false; reason = "fatal_error"; last_error = error.what();
    }
    coordinator.invalidate();
    if (worker.valid()) {
        const auto result = worker.get();
        ++counts.attempts_finished;
        if (result.status == host::Status::cancelled) ++counts.cancelled;
        else if (result.status == host::Status::error) {
            ++counts.errors; fatal = true; completed = false;
            reason = "fatal_error"; last_error = result.error;
        }
        event("attempt_finished", identity(result.ticket) + ",\"status\":" + quote(host::status_name(result.status)));
    }
    if (!pending.empty()) {
        for (const auto& item : pending) {
            ++counts.pending_lost;
            event("share_unresolved", identity(item.second.ticket) + ",\"id\":" + std::to_string(item.first));
        }
        pending.clear();
    }
    if (pool) { pool->close(); pool.reset(); }
    const auto elapsed = std::chrono::duration_cast<std::chrono::milliseconds>(Clock::now() - started).count();
    const std::string document = "{\n  \"schema\":\"neuro.pool_client.v1\",\n  \"completed\":" +
        std::string(completed && !fatal ? "true" : "false") + ",\n  \"reason\":" + quote(reason) +
        ",\n  \"backend\":" + quote(opt.backend) + ",\n  \"elapsed_ms\":" + std::to_string(elapsed) +
        ",\n  \"cpu_execution\":" + (cpu_computations.load() ? "true" : "false") +
        ",\n  \"gpu_execution\":" + (gpu_computations.load() ? "true" : "false") +
        ",\n  \"live_pool\":false,\n  \"synthetic_work\":true,\n  \"error\":" + quote(last_error) +
        ",\n  \"counters\":" + counts.json(checkpoints.load()) + "\n}\n";
    if (!opt.report.parent_path().empty()) std::filesystem::create_directories(opt.report.parent_path());
    const auto temporary = opt.report.string() + ".tmp";
    { std::ofstream stream(temporary, std::ios::binary | std::ios::trunc);
      if (!stream || !stream.write(document.data(), static_cast<std::streamsize>(document.size())))
          throw std::runtime_error("Cannot write native pool replay report"); }
    std::filesystem::rename(temporary, opt.report);
    event("completed", ",\"completed\":" + std::string(completed && !fatal ? "true" : "false") +
          ",\"reason\":" + quote(reason));
    return completed && !fatal ? 0 : 1;
}
} // namespace

int main(int argc, char** argv) {
    try {
        if (argc == 2 && std::string(argv[1]) == "--help") {
            std::cout << "Usage: neuro_pool_replay_client --port N [--report FILE] [--backend cpu|cuda]\n"
                         "Bounded local TCP replay only; no external host option.\n";
            return 0;
        }
        return run(options(argc, argv));
    } catch (const std::exception& error) {
        event("fatal_error", ",\"error\":" + quote(error.what()));
        return 2;
    }
}
