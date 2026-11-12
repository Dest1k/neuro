// SPDX-License-Identifier: MIT
#include "neuro/search_journal.hpp"
#include "blake3.h"

#include <algorithm>
#include <array>
#include <cerrno>
#include <cstring>
#include <limits>
#include <set>
#include <sstream>
#include <stdexcept>
#include <utility>
#include <vector>

#ifdef _WIN32
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>
#else
#include <fcntl.h>
#include <sys/file.h>
#include <sys/stat.h>
#include <unistd.h>
#endif

namespace neuro::host {
namespace {
using Bytes = std::vector<std::uint8_t>;
using Hash = std::array<std::uint8_t, 32>;
constexpr std::size_t max_file_bytes = 4 * 1024 * 1024;
constexpr std::size_t max_payload_bytes = 4096;
constexpr std::uint64_t max_frames = 32768;
constexpr std::size_t max_works = 64;
constexpr char policy[] = "neuro/zero-b/row-seed/v1";
constexpr char format[] = "neuro.search_journal.v1";
enum class Kind : std::uint8_t { header = 1, connection, work, binding, begin, finish, intent, response, uncertain, recover };

[[noreturn]] void invalid(const std::string& message) {
    throw std::runtime_error("search journal: " + message);
}
void require(bool condition, const std::string& message) {
    if (!condition) invalid(message);
}

void u8(Bytes& out, std::uint8_t value) { out.push_back(value); }
void u32(Bytes& out, std::uint32_t value) {
    for (int i = 0; i < 4; ++i) out.push_back(static_cast<std::uint8_t>(value >> (i * 8)));
}
void u64(Bytes& out, std::uint64_t value) {
    for (int i = 0; i < 8; ++i) out.push_back(static_cast<std::uint8_t>(value >> (i * 8)));
}
template<class T> void bytes(Bytes& out, const T& value) { out.insert(out.end(), value.begin(), value.end()); }
void string(Bytes& out, const std::string& value) {
    require(value.size() <= max_payload_bytes, "oversized string");
    u32(out, static_cast<std::uint32_t>(value.size()));
    bytes(out, value);
}

struct Reader {
    const Bytes& data;
    std::size_t position = 0;
    std::uint8_t u8() {
        require(position < data.size(), "truncated payload");
        return data[position++];
    }
    std::uint32_t u32() {
        std::uint32_t value = 0;
        for (int i = 0; i < 4; ++i) value |= static_cast<std::uint32_t>(u8()) << (i * 8);
        return value;
    }
    std::uint64_t u64() {
        std::uint64_t value = 0;
        for (int i = 0; i < 8; ++i) value |= static_cast<std::uint64_t>(u8()) << (i * 8);
        return value;
    }
    template<class T> void bytes(T& output) { for (auto& value : output) value = u8(); }
    std::string string(std::size_t maximum) {
        const auto length = u32();
        require(length <= maximum && length <= data.size() - position, "invalid string size");
        const std::string value(data.begin() + static_cast<std::ptrdiff_t>(position),
                                data.begin() + static_cast<std::ptrdiff_t>(position + length));
        position += length;
        return value;
    }
    void end() { require(position == data.size(), "unexpected payload suffix"); }
};

Hash digest(const Bytes& data) {
    constexpr char domain[] = "neuro/search-journal/frame/v1";
    blake3_hasher hasher;
    blake3_hasher_init(&hasher);
    blake3_hasher_update(&hasher, domain, sizeof(domain) - 1);
    blake3_hasher_update(&hasher, data.data(), data.size());
    Hash hash{};
    blake3_hasher_finalize(&hasher, hash.data(), hash.size());
    return hash;
}

std::string quote(const std::string& value) {
    constexpr char digits[] = "0123456789abcdef";
    std::string output = "\"";
    for (unsigned char byte : value) {
        if (byte == '"' || byte == '\\') { output += '\\'; output += static_cast<char>(byte); }
        else if (byte < 32 || byte >= 127) {
            output += "\\u00"; output += digits[byte >> 4]; output += digits[byte & 15];
        } else output += static_cast<char>(byte);
    }
    return output + '"';
}

void validate_scope(const std::string& scope) {
    require(!scope.empty() && scope.size() <= 255, "peer scope must contain 1..255 bytes");
    require(std::all_of(scope.begin(), scope.end(), [](unsigned char c) { return c >= 32 && c < 127; }),
            "peer scope must be printable ASCII");
}
void validate_job(const Job& job) {
    require(!job.job_id.empty() && job.job_id.size() <= 127 && job.job_id.find('\0') == std::string::npos,
            "invalid job id");
    require(job.connection != 0, "job connection must be nonzero");
    require(job.cert_version == 2 || job.cert_version == 3, "invalid certificate version");
    fullk::validate_shape(job.shape);
    require(job.shape.rows % 128 == 0 && job.shape.cols % 128 == 0, "invalid job dimensions");
    (void)proof::scaled_target(job.target);
}
bool same_work(const Job& a, const Job& b) {
    return a.job_id == b.job_id && a.header == b.header && a.target == b.target &&
           a.cert_version == b.cert_version && a.shape.rows == b.shape.rows &&
           a.shape.cols == b.shape.cols;
}

struct Attempt {
    AttemptOutcome outcome = AttemptOutcome::running;
    std::uint64_t connection = 0, generation = 0;
    int request_id = 0;
};
struct Work {
    Job job;
    Seed seed{};
    std::uint32_t limit = 0;
    std::uint64_t first_generation = 0, last_generation = 0, last_connection = 0;
    bool last_gzip = false;
    std::vector<Attempt> attempts;
};
struct State {
    std::string scope;
    std::uint64_t connection = 0;
    std::vector<Work> works;
};
Work& work_at(State& state, std::uint32_t id) {
    require(id > 0 && id <= state.works.size(), "unknown work identity");
    return state.works[id - 1];
}
Attempt& attempt_at(Work& work, std::uint32_t index) {
    require(index < work.attempts.size(), "unknown attempt");
    return work.attempts[index];
}

// Apply to a copy before persistence, then publish that copy only after flush.
// The same semantic parser validates every loaded record, not just its hash.
void apply(const Bytes& payload, State& state) {
    Reader input{payload};
    const auto kind = static_cast<Kind>(input.u8());
    if (kind == Kind::header) {
        require(state.scope.empty(), "duplicate header");
        require(input.string(64) == format && input.string(64) == policy, "unsupported journal format or seed policy");
        auto scope = input.string(255);
        validate_scope(scope);
        state.scope = std::move(scope);
    } else {
        require(!state.scope.empty(), "missing journal header");
        if (kind == Kind::connection) {
            const auto value = input.u64();
            require(state.connection != std::numeric_limits<std::uint64_t>::max() && value == state.connection + 1,
                    "nonconsecutive connection epoch");
            state.connection = value;
        } else if (kind == Kind::work) {
            const auto id = input.u32();
            require(state.works.size() < max_works && id == state.works.size() + 1, "work capacity or index exceeded");
            Work work;
            work.job.job_id = input.string(127);
            input.bytes(work.job.header);
            input.bytes(work.job.target);
            work.job.cert_version = static_cast<int>(input.u32());
            const auto rows = input.u32(), cols = input.u32();
            require(rows <= 1024 && cols <= 1024, "invalid dimensions");
            work.job.shape = {static_cast<int>(rows), static_cast<int>(cols)};
            const auto gzip = input.u8();
            require(gzip <= 1, "invalid encoding flag");
            work.job.gzip = gzip != 0;
            work.last_gzip = work.job.gzip;
            work.job.connection = input.u64();
            input.bytes(work.seed);
            work.limit = input.u32();
            work.first_generation = work.last_generation = input.u64();
            work.last_connection = work.job.connection;
            validate_job(work.job);
            require(work.limit > 0 && work.limit <= pearl::max_attempts, "invalid attempt limit");
            require(work.last_connection == state.connection && work.last_generation > 0, "invalid work provenance");
            for (const auto& old : state.works)
                require(!(same_work(old.job, work.job) && old.seed == work.seed), "duplicate work identity");
            state.works.push_back(std::move(work));
        } else {
            auto& work = work_at(state, input.u32());
            if (kind == Kind::binding) {
                const auto connection = input.u64(), generation = input.u64();
                const auto gzip = input.u8();
                require(gzip <= 1, "invalid refreshed encoding flag");
                require(connection == state.connection && generation > 0, "invalid refreshed work provenance");
                work.last_connection = connection;
                work.last_generation = generation;
                work.last_gzip = gzip != 0;
            } else {
                const auto index = input.u32();
                if (kind == Kind::begin) {
                    require(index == work.attempts.size() && index < work.limit, "attempt is already consumed, skipped or exhausted");
                    require(work.last_connection == state.connection && state.connection > 0, "attempt requires fresh work binding");
                    for (const auto& prior : work.attempts)
                        require(prior.outcome != AttemptOutcome::running && prior.outcome != AttemptOutcome::found &&
                                prior.outcome != AttemptOutcome::intent, "an earlier attempt is still active");
                    work.attempts.push_back({AttemptOutcome::running, work.last_connection, work.last_generation, 0});
                } else {
                    auto& attempt = attempt_at(work, index);
                    if (kind == Kind::finish) {
                        const auto outcome = static_cast<AttemptOutcome>(input.u8());
                        const bool computed = attempt.outcome == AttemptOutcome::running &&
                            (outcome == AttemptOutcome::found || outcome == AttemptOutcome::miss ||
                             outcome == AttemptOutcome::cancelled || outcome == AttemptOutcome::error);
                        const bool discard = attempt.outcome == AttemptOutcome::found && outcome == AttemptOutcome::discarded;
                        require(computed || discard, "invalid attempt completion transition");
                        attempt.outcome = outcome;
                    } else if (kind == Kind::intent) {
                        const auto connection = input.u64();
                        const auto request = input.u32();
                        require(attempt.outcome == AttemptOutcome::found && request > 0 && request <= 0x7fffffff,
                                "send requires an unsent found attempt and positive request id");
                        require(connection == state.connection && connection == attempt.connection,
                                "send belongs to a different connection");
                        for (const auto& item : state.works) for (const auto& prior : item.attempts)
                            require(prior.connection != connection || prior.request_id != static_cast<int>(request),
                                    "request id was already used in this connection");
                        attempt.request_id = static_cast<int>(request);
                        attempt.outcome = AttemptOutcome::intent;
                    } else if (kind == Kind::response) {
                        const auto connection = input.u64();
                        const auto request = input.u32();
                        const auto accepted = input.u8();
                        require(attempt.outcome == AttemptOutcome::intent && accepted <= 1 && request > 0 &&
                                request <= 0x7fffffff && attempt.connection == connection &&
                                attempt.request_id == static_cast<int>(request) && connection == state.connection,
                                "response does not match a live pending intent");
                        attempt.outcome = accepted ? AttemptOutcome::accepted : AttemptOutcome::rejected;
                    } else if (kind == Kind::uncertain) {
                        require(attempt.outcome == AttemptOutcome::intent, "only a pending intent can become uncertain");
                        attempt.outcome = AttemptOutcome::uncertain;
                    } else if (kind == Kind::recover) {
                        const auto outcome = static_cast<AttemptOutcome>(input.u8());
                        require((outcome == AttemptOutcome::interrupted &&
                                 (attempt.outcome == AttemptOutcome::running || attempt.outcome == AttemptOutcome::found)) ||
                                (outcome == AttemptOutcome::uncertain && attempt.outcome == AttemptOutcome::intent),
                                "invalid recovery transition");
                        attempt.outcome = outcome;
                    } else invalid("unknown record kind");
                }
            }
        }
    }
    input.end();
}

Bytes command(Kind kind, std::uint32_t work, std::uint32_t index) {
    Bytes output;
    u8(output, static_cast<std::uint8_t>(kind)); u32(output, work); u32(output, index);
    return output;
}

class LockedFile {
public:
    bool created = false;
    explicit LockedFile(const std::filesystem::path& path) : path_(path) {
        require(!path.empty(), "empty path");
        auto parent = path.parent_path();
        if (parent.empty()) parent = ".";
        require(std::filesystem::is_directory(parent), "parent directory must already exist");
#ifdef _WIN32
        handle_ = CreateFileW(path.c_str(), GENERIC_READ | GENERIC_WRITE, FILE_SHARE_READ, nullptr,
                              CREATE_NEW, FILE_ATTRIBUTE_NORMAL | FILE_FLAG_OPEN_REPARSE_POINT, nullptr);
        if (handle_ != INVALID_HANDLE_VALUE) created = true;
        else if (GetLastError() == ERROR_FILE_EXISTS || GetLastError() == ERROR_ALREADY_EXISTS)
            handle_ = CreateFileW(path.c_str(), GENERIC_READ | GENERIC_WRITE, FILE_SHARE_READ, nullptr,
                                  OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL | FILE_FLAG_OPEN_REPARSE_POINT, nullptr);
        if (handle_ == INVALID_HANDLE_VALUE) {
            const auto error = GetLastError();
            if (error == ERROR_SHARING_VIOLATION || error == ERROR_LOCK_VIOLATION)
                invalid("journal is already locked by another process");
            invalid("cannot open exclusive journal file");
        }
        BY_HANDLE_FILE_INFORMATION info{};
        if (!GetFileInformationByHandle(handle_, &info) || info.nNumberOfLinks != 1 ||
            (info.dwFileAttributes & (FILE_ATTRIBUTE_REPARSE_POINT | FILE_ATTRIBUTE_DIRECTORY))) {
            CloseHandle(handle_); handle_ = INVALID_HANDLE_VALUE;
            invalid("journal must be a regular file without links");
        }
#else
        fd_ = ::open(path.c_str(), O_RDWR | O_CREAT | O_EXCL | O_NOFOLLOW | O_CLOEXEC, 0600);
        if (fd_ >= 0) created = true;
        else if (errno == EEXIST) fd_ = ::open(path.c_str(), O_RDWR | O_NOFOLLOW | O_CLOEXEC);
        if (fd_ < 0) invalid("cannot open journal file");
        struct stat info{};
        if (::fstat(fd_, &info) != 0 || !S_ISREG(info.st_mode) || info.st_nlink != 1 ||
            ::flock(fd_, LOCK_EX | LOCK_NB) != 0) {
            ::close(fd_); fd_ = -1;
            invalid("journal is not a regular unlinked file or is already locked");
        }
#endif
    }
    ~LockedFile() {
#ifdef _WIN32
        if (handle_ != INVALID_HANDLE_VALUE) CloseHandle(handle_);
#else
        if (fd_ >= 0) ::close(fd_);
#endif
    }
    Bytes read() {
        std::uint64_t size = 0;
#ifdef _WIN32
        LARGE_INTEGER length{}, zero{};
        require(GetFileSizeEx(handle_, &length) && length.QuadPart >= 0, "cannot read file size");
        size = static_cast<std::uint64_t>(length.QuadPart);
        require(SetFilePointerEx(handle_, zero, nullptr, FILE_BEGIN) != 0, "cannot seek journal");
#else
        struct stat info{};
        require(::fstat(fd_, &info) == 0 && info.st_size >= 0, "cannot read file size");
        size = static_cast<std::uint64_t>(info.st_size);
        require(::lseek(fd_, 0, SEEK_SET) == 0, "cannot seek journal");
#endif
        require(size <= max_file_bytes, "journal byte capacity exceeded");
        Bytes output(static_cast<std::size_t>(size));
        std::size_t done = 0;
        while (done < output.size()) {
#ifdef _WIN32
            DWORD count = 0;
            require(ReadFile(handle_, output.data() + done, static_cast<DWORD>(output.size() - done), &count, nullptr) && count > 0,
                    "cannot read complete journal");
#else
            const auto count = ::read(fd_, output.data() + done, output.size() - done);
            if (count < 0 && errno == EINTR) continue;
            require(count > 0, "cannot read complete journal");
#endif
            done += static_cast<std::size_t>(count);
        }
        return output;
    }
    void append(const Bytes& data, std::size_t expected_offset) {
#ifdef _WIN32
        LARGE_INTEGER zero{}, position{};
        require(SetFilePointerEx(handle_, zero, &position, FILE_END) &&
                position.QuadPart == static_cast<LONGLONG>(expected_offset), "journal changed during its locked lifetime");
#else
        require(::lseek(fd_, 0, SEEK_END) == static_cast<off_t>(expected_offset), "journal changed during its locked lifetime");
#endif
        std::size_t done = 0;
        while (done < data.size()) {
#ifdef _WIN32
            DWORD count = 0;
            require(WriteFile(handle_, data.data() + done, static_cast<DWORD>(data.size() - done), &count, nullptr) && count > 0,
                    "cannot append complete journal frame");
#else
            const auto count = ::write(fd_, data.data() + done, data.size() - done);
            if (count < 0 && errno == EINTR) continue;
            require(count > 0, "cannot append complete journal frame");
#endif
            done += static_cast<std::size_t>(count);
        }
#ifdef _WIN32
        require(FlushFileBuffers(handle_) != 0, "journal flush failed");
#else
        int status;
        do { status = ::fsync(fd_); } while (status != 0 && errno == EINTR);
        require(status == 0, "journal flush failed");
#endif
    }
    void sync_parent() {
#ifndef _WIN32
        auto parent = path_.parent_path();
        if (parent.empty()) parent = ".";
        const int directory = ::open(parent.c_str(), O_RDONLY | O_DIRECTORY | O_CLOEXEC);
        require(directory >= 0, "cannot open parent for persistence");
        int status;
        do { status = ::fsync(directory); } while (status != 0 && errno == EINTR);
        ::close(directory);
        require(status == 0, "parent directory flush failed");
#endif
    }
private:
    std::filesystem::path path_;
#ifdef _WIN32
    HANDLE handle_ = INVALID_HANDLE_VALUE;
#else
    int fd_ = -1;
#endif
};
} // namespace

const char* outcome_name(AttemptOutcome outcome) noexcept {
    switch (outcome) {
    case AttemptOutcome::running: return "running";
    case AttemptOutcome::found: return "found";
    case AttemptOutcome::miss: return "miss";
    case AttemptOutcome::cancelled: return "cancelled";
    case AttemptOutcome::error: return "error";
    case AttemptOutcome::interrupted: return "interrupted";
    case AttemptOutcome::intent: return "intent";
    case AttemptOutcome::accepted: return "accepted";
    case AttemptOutcome::rejected: return "rejected";
    case AttemptOutcome::uncertain: return "uncertain";
    case AttemptOutcome::discarded: return "discarded";
    }
    return "invalid";
}

struct SearchJournal::Impl {
    LockedFile file;
    State state;
    std::uint64_t sequence = 0;
    Hash previous{};
    std::size_t file_bytes = 0;
    bool recovered = false, poisoned = false;
    bool connection_allocated = false;
    std::set<std::uint32_t> bound;

    Impl(const std::filesystem::path& path, const std::string& scope) : file(path) {
        validate_scope(scope);
        const auto input = file.read();
        if (file.created) {
            require(input.empty(), "new file is unexpectedly nonempty");
            Bytes header;
            u8(header, static_cast<std::uint8_t>(Kind::header));
            string(header, format); string(header, policy); string(header, scope);
            append(header);
            file.sync_parent();
        } else {
            require(!input.empty(), "existing journal is empty; refusing to reset history");
            Reader reader{input};
            while (reader.position < input.size()) {
                const auto start = reader.position;
                const auto size = reader.u32();
                require(size > 0 && size <= max_payload_bytes, "invalid frame length");
                require(size + 72 <= input.size() - start - 4, "truncated journal frame");
                require(reader.u64() == sequence + 1 && sequence < max_frames, "invalid frame sequence");
                Hash parent{};
                reader.bytes(parent);
                require(parent == previous, "broken frame chain");
                const Bytes payload(input.begin() + static_cast<std::ptrdiff_t>(reader.position),
                                    input.begin() + static_cast<std::ptrdiff_t>(reader.position + size));
                reader.position += size;
                const Bytes signed_frame(input.begin() + static_cast<std::ptrdiff_t>(start),
                                         input.begin() + static_cast<std::ptrdiff_t>(reader.position));
                Hash recorded{};
                reader.bytes(recorded);
                require(recorded == digest(signed_frame), "journal checksum mismatch");
                apply(payload, state);
                previous = recorded;
                ++sequence;
            }
            require(state.scope == scope, "peer scope differs from existing journal");
            file_bytes = input.size();
            recovered = true;
            for (std::size_t w = 0; w < state.works.size(); ++w) {
                for (std::size_t a = 0; a < state.works[w].attempts.size(); ++a) {
                    const auto old = state.works[w].attempts[a].outcome;
                    if (old == AttemptOutcome::running || old == AttemptOutcome::found || old == AttemptOutcome::intent) {
                        auto payload = command(Kind::recover, static_cast<std::uint32_t>(w + 1), static_cast<std::uint32_t>(a));
                        u8(payload, static_cast<std::uint8_t>(old == AttemptOutcome::intent ?
                            AttemptOutcome::uncertain : AttemptOutcome::interrupted));
                        append(payload);
                    }
                }
            }
        }
    }
    void append(const Bytes& payload) {
        require(!poisoned, "journal is poisoned after a failed write");
        require(sequence < max_frames && !payload.empty() && payload.size() <= max_payload_bytes, "journal frame capacity exceeded");
        State next = state;
        apply(payload, next);
        Bytes frame;
        u32(frame, static_cast<std::uint32_t>(payload.size()));
        u64(frame, sequence + 1);
        bytes(frame, previous); bytes(frame, payload);
        const auto hash = digest(frame);
        bytes(frame, hash);
        require(frame.size() <= max_file_bytes - file_bytes, "journal byte capacity exceeded");
        try { file.append(frame, file_bytes); }
        catch (...) { poisoned = true; throw; }
        state = std::move(next);
        previous = hash;
        ++sequence;
        file_bytes += frame.size();
    }
};

SearchJournal::SearchJournal(const std::filesystem::path& path, std::string scope)
    : impl_(std::make_unique<Impl>(path, scope)) {}
SearchJournal::~SearchJournal() = default;

std::uint64_t SearchJournal::next_connection() {
    require(impl_->state.connection != std::numeric_limits<std::uint64_t>::max(), "connection epochs exhausted");
    const auto epoch = impl_->state.connection + 1;
    Bytes payload; u8(payload, static_cast<std::uint8_t>(Kind::connection)); u64(payload, epoch);
    impl_->append(payload);
    impl_->bound.clear();
    impl_->connection_allocated = true;
    return epoch;
}

SearchBinding SearchJournal::bind(const Job& job, const Seed& seed, std::uint32_t limit, std::uint64_t generation) {
    require(!impl_->poisoned, "journal is poisoned after a failed write");
    require(impl_->connection_allocated, "bind requires a newly allocated connection in this process");
    validate_job(job);
    require(job.connection == impl_->state.connection && generation > 0, "bind requires the current allocated connection and generation");
    require(limit > 0 && limit <= pearl::max_attempts, "invalid attempt limit");
    for (std::size_t i = 0; i < impl_->state.works.size(); ++i) {
        const auto& work = impl_->state.works[i];
        if (!same_work(work.job, job) || work.seed != seed) continue;
        require(work.limit == limit, "matching work has a different pinned attempt limit");
        const SearchBinding result{static_cast<std::uint32_t>(i + 1), static_cast<std::uint32_t>(work.attempts.size()), limit, true};
        if (work.last_connection != job.connection || work.last_generation != generation || work.last_gzip != job.gzip) {
            Bytes payload; u8(payload, static_cast<std::uint8_t>(Kind::binding)); u32(payload, result.work_id);
            u64(payload, job.connection); u64(payload, generation);
            u8(payload, job.gzip ? 1 : 0);
            impl_->append(payload);
        }
        impl_->bound.insert(result.work_id);
        return result;
    }
    require(impl_->state.works.size() < max_works, "work identity capacity exceeded; refusing eviction");
    const auto id = static_cast<std::uint32_t>(impl_->state.works.size() + 1);
    Bytes payload; u8(payload, static_cast<std::uint8_t>(Kind::work)); u32(payload, id);
    string(payload, job.job_id); bytes(payload, job.header); bytes(payload, job.target);
    u32(payload, static_cast<std::uint32_t>(job.cert_version)); u32(payload, static_cast<std::uint32_t>(job.shape.rows));
    u32(payload, static_cast<std::uint32_t>(job.shape.cols)); u8(payload, job.gzip ? 1 : 0); u64(payload, job.connection);
    bytes(payload, seed); u32(payload, limit); u64(payload, generation);
    impl_->append(payload);
    impl_->bound.insert(id);
    return {id, 0, limit, false};
}

void SearchJournal::begin(std::uint32_t work, std::uint32_t index) {
    require(impl_->bound.count(work) != 0, "attempt requires a fresh binding in this process and connection");
    impl_->append(command(Kind::begin, work, index));
}
void SearchJournal::finish(std::uint32_t work, std::uint32_t index, AttemptOutcome outcome) {
    auto payload = command(Kind::finish, work, index); u8(payload, static_cast<std::uint8_t>(outcome));
    impl_->append(payload);
}
void SearchJournal::send_intent(std::uint32_t work, std::uint32_t index, std::uint64_t connection, int request) {
    require(request > 0, "request id must be positive");
    auto payload = command(Kind::intent, work, index); u64(payload, connection); u32(payload, static_cast<std::uint32_t>(request));
    impl_->append(payload);
}
void SearchJournal::response(std::uint32_t work, std::uint32_t index, std::uint64_t connection, int request, bool accepted) {
    require(request > 0, "request id must be positive");
    auto payload = command(Kind::response, work, index); u64(payload, connection);
    u32(payload, static_cast<std::uint32_t>(request)); u8(payload, accepted ? 1 : 0);
    impl_->append(payload);
}
void SearchJournal::uncertain(std::uint32_t work, std::uint32_t index) {
    impl_->append(command(Kind::uncertain, work, index));
}

bool SearchJournal::healthy() const noexcept { return !impl_->poisoned; }

std::string SearchJournal::report_json() const {
    std::array<std::size_t, 12> outcomes{};
    std::size_t total = 0;
    for (const auto& work : impl_->state.works) for (const auto& attempt : work.attempts) {
        ++outcomes[static_cast<std::size_t>(attempt.outcome)]; ++total;
    }
    std::ostringstream output;
    output << "{\"enabled\":true,\"schema\":" << quote(format)
           << ",\"recovered\":" << (impl_->recovered ? "true" : "false")
           << ",\"poisoned\":" << (impl_->poisoned ? "true" : "false")
           << ",\"work_count\":" << impl_->state.works.size()
           << ",\"connections\":" << impl_->state.connection << ",\"attempts_consumed\":" << total
           << ",\"frames\":" << impl_->sequence << ",\"bytes\":" << impl_->file_bytes << ",\"outcomes\":{";
    for (std::size_t i = 1; i < outcomes.size(); ++i) {
        if (i != 1) output << ',';
        output << quote(outcome_name(static_cast<AttemptOutcome>(i))) << ':' << outcomes[i];
    }
    output << "},\"works\":[";
    for (std::size_t i = 0; i < impl_->state.works.size(); ++i) {
        const auto& work = impl_->state.works[i];
        if (i) output << ',';
        output << "{\"work_id\":" << i + 1 << ",\"next_attempt\":" << work.attempts.size()
               << ",\"limit\":" << work.limit << ",\"job_id\":" << quote(work.job.job_id)
               << ",\"last_connection\":" << work.last_connection << ",\"last_generation\":" << work.last_generation
               << ",\"last_gzip\":" << (work.last_gzip ? "true" : "false") << '}';
    }
    return output.str() + "]}";
}

} // namespace neuro::host
