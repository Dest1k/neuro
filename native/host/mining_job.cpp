// SPDX-License-Identifier: MIT
#include "neuro/mining_job.hpp"

#include <limits>
#include <stdexcept>
#include <utility>

namespace neuro::host {
namespace {

bool same_job(const Job& left, const Job& right) {
    return left.job_id == right.job_id && left.header == right.header &&
           left.target == right.target && left.cert_version == right.cert_version &&
           left.shape.rows == right.shape.rows && left.shape.cols == right.shape.cols &&
           left.gzip == right.gzip && left.connection == right.connection;
}

bool same_ticket(const Ticket& left, const Ticket& right) {
    return left.generation == right.generation && left.job == right.job;
}

void validate(const Job& job) {
    if (job.job_id.empty() || job.job_id.size() > 127 ||
        job.job_id.find('\0') != std::string::npos)
        throw std::invalid_argument("job_id must contain 1..127 non-NUL bytes");
    if (!job.connection)
        throw std::invalid_argument("connection identity must be nonzero");
    if (job.cert_version != 2 && job.cert_version != 3)
        throw std::invalid_argument("only certificate versions 2 and 3 are supported");
    fullk::validate_shape(job.shape);
    if (job.shape.rows % 128 || job.shape.cols % 128)
        throw std::invalid_argument("job dimensions must be multiples of 128");
    (void)proof::scaled_target(job.target);
}

void check_selected_tile(const fullk::Input& input, const fullk::Result& candidate,
                         proof::Tile tile) {
    // The proof is reconstructed from signals, not from candidate transcripts.
    // A broken backend could therefore pick a coincidentally genuine winner
    // while reporting an incorrect hash. Check all 32 words for this one tile
    // using the already independent scalar oracle, on a 16x16 extracted panel.
    const int first_row = tile.row / 16 * 16;
    const auto a_first = input.a.begin() + static_cast<std::size_t>(first_row) * fullk::inner;
    const auto b_first = input.bt.begin() + static_cast<std::size_t>(tile.col) * fullk::inner;
    const auto panel_bytes = static_cast<std::size_t>(16) * fullk::inner;
    const fullk::Input panel{{16, 16}, {a_first, a_first + panel_bytes},
                             {b_first, b_first + panel_bytes}};
    const auto expected = fullk::reference(panel);
    const auto tiles = fullk::tile_count(input.shape);
    const auto spatial = static_cast<std::size_t>(tile.row / fullk::hash_rows) *
                         (input.shape.cols / fullk::hash_cols) + tile.col / fullk::hash_cols;
    const auto local = static_cast<std::size_t>((tile.row - first_row) / fullk::hash_rows);
    for (int milestone = 0; milestone < fullk::milestones; ++milestone) {
        if (candidate.transcript[static_cast<std::size_t>(milestone) * tiles + spatial] !=
            expected.transcript[static_cast<std::size_t>(milestone) * 2 + local])
            throw std::runtime_error("selected-tile transcript differs from independent CPU reference");
    }
}

} // namespace

const char* status_name(Status status) noexcept {
    switch (status) {
    case Status::found: return "found";
    case Status::miss: return "miss";
    case Status::cancelled: return "cancelled";
    case Status::error: return "error";
    }
    return "error";
}

Ticket Coordinator::publish(Job job) {
    validate(job);
    auto immutable = std::make_shared<const Job>(std::move(job));
    std::lock_guard<std::mutex> lock(mutex_);
    if (job_ && same_job(*job_, *immutable)) return {generation_, job_};
    advance_locked();
    job_ = std::move(immutable);
    committed_ = false;
    return {generation_, job_};
}

void Coordinator::advance_locked() {
    if (generation_ == std::numeric_limits<std::uint64_t>::max())
        throw std::overflow_error("job generation exhausted");
    ++generation_;
}

void Coordinator::invalidate() {
    std::lock_guard<std::mutex> lock(mutex_);
    advance_locked();
    job_.reset();
    committed_ = false;
}

bool Coordinator::disconnect(std::uint64_t connection) {
    std::lock_guard<std::mutex> lock(mutex_);
    if (!job_ || job_->connection != connection) return false;
    advance_locked();
    job_.reset();
    committed_ = false;
    return true;
}

bool Coordinator::current_locked(const Ticket& ticket) const {
    return job_ && ticket.job == job_ && ticket.generation == generation_;
}

bool Coordinator::current(const Ticket& ticket) const {
    std::lock_guard<std::mutex> lock(mutex_);
    return current_locked(ticket);
}

Result Coordinator::run_one(const Ticket& ticket, const Seed& seed, const Compute& compute,
                            const Checkpoint& checkpoint) {
    Result result;
    result.ticket = ticket;
    const auto stale = [&] {
        if (current(ticket)) return false;
        result.status = Status::cancelled;
        result.error.clear();
        return true;
    };
    const auto step = [&](Stage stage) {
        if (stale()) return false;
        if (checkpoint) checkpoint(stage, ticket);
        return !stale();
    };
    try {
        if (!step(Stage::before_prepare)) return result;
        if (!compute) throw std::invalid_argument("missing arithmetic backend");
        pearl::Request request;
        request.shape = ticket.job->shape;
        request.header.assign(ticket.job->header.begin(), ticket.job->header.end());
        request.seed.assign(seed.begin(), seed.end());
        request.cert_version = ticket.job->cert_version;
        auto prepared = pearl::prepare_zero_b(request);
        if (!step(Stage::after_prepare) || !step(Stage::before_compute)) return result;
        fullk::Result arithmetic;
        try {
            arithmetic = compute(prepared.noisy);
        } catch (const std::exception& error) {
            // Compute is not preempted by ticket invalidation. In particular,
            // a concurrent new job must not hide a failed CUDA context behind
            // an ordinary cancelled result and permit reuse of that worker.
            result.status = Status::error;
            result.error = std::string("arithmetic backend failed: ") + error.what();
            return result;
        } catch (...) {
            result.status = Status::error;
            result.error = "arithmetic backend failed: non-standard exception";
            return result;
        }
        if (!step(Stage::after_compute)) return result;
        if (arithmetic.transcript.size() != fullk::transcript_words(request.shape) ||
            (!arithmetic.final_c.empty() && arithmetic.final_c.size() !=
             static_cast<std::size_t>(request.shape.rows) * request.shape.cols))
            throw std::runtime_error("backend returned an invalid arithmetic buffer size");

        proof::Tile tile{-1, -1};
        pearl::Digest hash{};
        for (int row = 0; row < request.shape.rows && tile.row < 0; row += fullk::hash_rows) {
            for (int col = 0; col < request.shape.cols; col += fullk::hash_cols) {
                if (stale()) return result;
                const auto jackpot = proof::evaluate_tile(prepared, arithmetic, {row, col},
                                                           ticket.job->target);
                ++result.tiles_scanned;
                if (jackpot.meets_target) {
                    tile = {row, col};
                    hash = jackpot.hash;
                    break;
                }
            }
        }
        if (tile.row < 0) {
            if (!stale()) result.status = Status::miss;
            return result;
        }
        if (!step(Stage::before_proof)) return result;
        check_selected_tile(prepared.noisy, arithmetic, tile);
        if (stale()) return result;
        auto raw_proof = proof::build_plain(request, prepared, tile);
        if (!step(Stage::after_proof)) return result;
        const auto verification = proof::verify_plain(request.header, raw_proof,
                                                      ticket.job->target, request.cert_version);
        if (!step(Stage::after_verify)) return result;
        if (!verification.valid)
            throw std::runtime_error("real proof verification failed: " + verification.error);
        result.share = std::make_shared<const Share>(Share{ticket, tile, hash, std::move(raw_proof)});
        result.verified_share_ = result.share;
        result.status = Status::found;
    } catch (const std::exception& error) {
        if (!stale()) {
            result.status = Status::error;
            result.error = error.what();
        }
    } catch (...) {
        if (!stale()) {
            result.status = Status::error;
            result.error = "unknown backend or checkpoint exception";
        }
    }
    return result;
}

bool Coordinator::commit(const Result& result, const Sink& sink) {
    std::lock_guard<std::mutex> lock(mutex_);
    if (result.status != Status::found || !result.share ||
        result.share != result.verified_share_ ||
        !same_ticket(result.ticket, result.share->ticket) ||
        !current_locked(result.ticket) || committed_ || !sink)
        return false;
    committed_ = true;
    return sink(*result.share);
}

} // namespace neuro::host
