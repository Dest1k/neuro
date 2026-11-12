// SPDX-License-Identifier: MIT
#include "neuro/search_journal.hpp"
#include "blake3.h"

#include <array>
#include <chrono>
#include <cstdint>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <stdexcept>
#include <string>
#include <vector>

namespace {
namespace fs = std::filesystem;
using namespace neuro::host;
using Bytes = std::vector<std::uint8_t>;
std::size_t checks = 0, rejections = 0;

void require(bool value, const std::string& message) {
    if (!value) throw std::runtime_error(message);
    ++checks;
}
template<class F> void rejects(F&& action, const std::string& name) {
    try { action(); }
    catch (const std::exception&) { ++rejections; return; }
    throw std::runtime_error(name + " was accepted");
}
void contains(const std::string& text, const std::string& expected) {
    require(text.find(expected) != std::string::npos, "report lacks " + expected);
}

struct TemporaryDirectory {
    fs::path root;
    TemporaryDirectory() {
        const auto now = std::chrono::high_resolution_clock::now().time_since_epoch().count();
        for (unsigned i = 0; i < 100; ++i) {
            auto path = fs::temp_directory_path() / ("neuro-journal-test-" + std::to_string(now) + "-" + std::to_string(i));
            if (fs::create_directory(path)) { root = std::move(path); return; }
        }
        throw std::runtime_error("cannot allocate temporary test directory");
    }
    ~TemporaryDirectory() { std::error_code error; fs::remove_all(root, error); }
};

Job job(std::uint64_t connection = 1) {
    Job value;
    value.job_id = "synthetic-journal-job";
    value.connection = connection;
    value.target[2] = 8;
    for (std::size_t i = 0; i < value.header.size(); ++i) value.header[i] = static_cast<std::uint8_t>(i * 7 + 19);
    return value;
}
Seed seed() {
    Seed value{};
    for (std::size_t i = 0; i < value.size(); ++i) value[i] = static_cast<std::uint8_t>(i * 13 + 29);
    return value;
}
Bytes read(const fs::path& path) {
    std::ifstream input(path, std::ios::binary);
    if (!input) throw std::runtime_error("cannot read test file");
    return {std::istreambuf_iterator<char>(input), std::istreambuf_iterator<char>()};
}
void write(const fs::path& path, const Bytes& data) {
    std::ofstream output(path, std::ios::binary | std::ios::trunc);
    if (!output || !output.write(reinterpret_cast<const char*>(data.data()), static_cast<std::streamsize>(data.size())))
        throw std::runtime_error("cannot write test file");
}

void lifecycle(const fs::path& path) {
    std::uint32_t id = 0;
    {
        SearchJournal journal(path, "local-fixture-peer");
        require(journal.healthy(), "new journal must be healthy");
        contains(journal.report_json(), "\"recovered\":false");
        require(journal.next_connection() == 1, "first durable connection is one");
        const auto binding = journal.bind(job(), seed(), 8, 1);
        id = binding.work_id;
        require(id == 1 && binding.next_attempt == 0 && binding.limit == 8 && !binding.resumed, "first work binding");
        rejects([&] { SearchJournal second(path, "local-fixture-peer"); }, "concurrent writer");
        rejects([&] { journal.begin(id, 1); }, "skipped first attempt");
        journal.begin(id, 0);
        rejects([&] { journal.begin(id, 0); }, "repeated attempt");
        rejects([&] { journal.begin(id, 1); }, "overlapping attempt");
        rejects([&] { journal.send_intent(id, 0, 1, 2); }, "send before found proof");
        rejects([&] { journal.finish(id, 0, AttemptOutcome::accepted); }, "manufactured acceptance");
        journal.finish(id, 0, AttemptOutcome::miss);
        rejects([&] { journal.finish(id, 0, AttemptOutcome::found); }, "reclassified miss");
        journal.begin(id, 1);
        journal.finish(id, 1, AttemptOutcome::found);
        rejects([&] { journal.send_intent(id, 1, 2, 2); }, "foreign connection intent");
        rejects([&] { journal.send_intent(id, 1, 1, 0); }, "zero request id");
        journal.send_intent(id, 1, 1, 2);
        rejects([&] { journal.response(id, 1, 1, 3, true); }, "wrong response id");
        rejects([&] { journal.response(id, 1, 2, 2, true); }, "wrong response connection");
        journal.response(id, 1, 1, 2, true);
        rejects([&] { journal.response(id, 1, 1, 2, false); }, "duplicate contradictory response");
        journal.begin(id, 2); journal.finish(id, 2, AttemptOutcome::found);
        rejects([&] { journal.send_intent(id, 2, 1, 2); }, "reused connection request id");
        journal.send_intent(id, 2, 1, 3); journal.response(id, 2, 1, 3, false);
        journal.begin(id, 3); journal.finish(id, 3, AttemptOutcome::cancelled);
        journal.begin(id, 4); journal.finish(id, 4, AttemptOutcome::error);
        journal.begin(id, 5); journal.finish(id, 5, AttemptOutcome::found); journal.finish(id, 5, AttemptOutcome::discarded);
        journal.begin(id, 6); journal.finish(id, 6, AttemptOutcome::found);
        journal.send_intent(id, 6, 1, 4); journal.uncertain(id, 6);
        rejects([&] { journal.response(id, 6, 1, 4, true); }, "uncertain result relabelled accepted");
        const auto same = journal.bind(job(), seed(), 8, 1);
        require(same.resumed && same.work_id == id && same.next_attempt == 7, "duplicate job preserves progress");
        const auto before = fs::file_size(path);
        rejects([&] { journal.bind(job(), seed(), 7, 1); }, "changed attempt cap");
        require(fs::file_size(path) == before, "invalid limit did not alter journal");
        const auto report = journal.report_json();
        for (const auto& text : {"\"accepted\":1", "\"rejected\":1", "\"miss\":1", "\"cancelled\":1", "\"error\":1", "\"uncertain\":1", "\"discarded\":1"})
            contains(report, text);
    }
    {
        SearchJournal journal(path, "local-fixture-peer");
        contains(journal.report_json(), "\"recovered\":true");
        rejects([&] { journal.begin(id, 7); }, "loaded binding without fresh notify");
        rejects([&] { journal.bind(job(), seed(), 8, 1); }, "reused previous process connection");
        require(journal.next_connection() == 2, "restart allocates a new persisted connection");
        const auto same = journal.bind(job(2), seed(), 8, 1);
        require(same.work_id == id && same.next_attempt == 7 && same.resumed, "restart preserves full progress");
        journal.begin(id, 7);
    }
    {
        SearchJournal journal(path, "local-fixture-peer");
        contains(journal.report_json(), "\"interrupted\":1");
        contains(journal.report_json(), "\"attempts_consumed\":8");
        require(journal.next_connection() == 3, "third process connection");
        require(journal.bind(job(3), seed(), 8, 1).next_attempt == 8, "interrupted attempt remains consumed");
        rejects([&] { journal.begin(id, 8); }, "exhausted plan");
    }
    const auto bytes = read(path);
    rejects([&] { SearchJournal wrong(path, "different-peer"); }, "different peer scope");
    require(read(path) == bytes, "scope mismatch preserved bytes");
}

void unfinished_and_identity(const fs::path& path) {
    {
        SearchJournal journal(path, "recovery-peer");
        journal.next_connection();
        for (std::uint32_t i = 0; i < 3; ++i) {
            auto work = job(); work.job_id += std::to_string(i);
            const auto id = journal.bind(work, seed(), 4, i + 1).work_id;
            journal.begin(id, 0);
            if (i > 0) journal.finish(id, 0, AttemptOutcome::found);
            if (i > 1) journal.send_intent(id, 0, 1, 17);
        }
    }
    {
        SearchJournal journal(path, "recovery-peer");
        const auto report = journal.report_json();
        contains(report, "\"running\":0"); contains(report, "\"found\":0"); contains(report, "\"intent\":0");
        contains(report, "\"interrupted\":2"); contains(report, "\"uncertain\":1");
        journal.next_connection();
        for (std::uint32_t i = 0; i < 3; ++i) {
            auto work = job(2); work.job_id += std::to_string(i);
            // Same search, newly negotiated transport encoding after a send
            // with lost ACK: this must never reset its consumed attempt prefix.
            if (i == 2) work.gzip = true;
            const auto bound = journal.bind(work, seed(), 4, i + 1);
            require(bound.work_id == i + 1 && bound.next_attempt == 1, "recovery retained each original work identity");
            rejects([&] { journal.begin(bound.work_id, 0); }, "replay interrupted or uncertain attempt");
            journal.begin(bound.work_id, 1); journal.finish(bound.work_id, 1, AttemptOutcome::miss);
        }
        contains(journal.report_json(), "\"uncertain\":1");
        contains(journal.report_json(), "\"last_gzip\":true");
        auto a = job(2); a.job_id = "roundtrip-A";
        auto b = a; b.job_id = "roundtrip-B";
        const auto first = journal.bind(a, seed(), 4, 4);
        journal.begin(first.work_id, 0); journal.finish(first.work_id, 0, AttemptOutcome::miss);
        journal.bind(b, seed(), 4, 5);
        const auto returned = journal.bind(a, seed(), 4, 6);
        require(returned.work_id == first.work_id && returned.next_attempt == 1, "A-B-A did not reset A");
        const auto base_id = returned.work_id;
        std::vector<Job> changed;
        auto mutation = a; mutation.header.back() ^= 1; changed.push_back(mutation);
        mutation = a; mutation.target.back() ^= 1; changed.push_back(mutation);
        mutation = a; mutation.cert_version = 2; changed.push_back(mutation);
        mutation = a; mutation.shape.rows = 256; changed.push_back(mutation);
        mutation = a; mutation.shape.cols = 256; changed.push_back(mutation);
        mutation = a; mutation.job_id += "-different"; changed.push_back(mutation);
        std::uint32_t expected = 6;
        for (const auto& item : changed) {
            const auto bound = journal.bind(item, seed(), 4, ++expected);
            require(bound.work_id != base_id && !bound.resumed && bound.next_attempt == 0, "full work field changes identity");
        }
        auto encoded = a; encoded.gzip = true;
        const auto encoding_only = journal.bind(encoded, seed(), 4, 98);
        require(encoding_only.work_id == base_id && encoding_only.resumed && encoding_only.next_attempt == 1,
                "gzip negotiation changes provenance, not consumed search identity");
        auto other_seed = seed(); other_seed.back() ^= 1;
        require(journal.bind(a, other_seed, 4, 99).work_id != base_id, "root seed is part of work identity");
        require(journal.bind(a, seed(), 4, 100).next_attempt == 1, "original identity remains retained after variations");
        journal.next_connection();
        rejects([&] { journal.begin(base_id, 1); }, "new connection without rebind");
        a.connection = 3;
        require(journal.bind(a, seed(), 4, 1).next_attempt == 1, "fresh connection can rebind same complete work");
    }
}

std::uint32_t little32(const Bytes& data, std::size_t offset) {
    if (offset + 4 > data.size()) throw std::runtime_error("test fixture offset overflow");
    std::uint32_t value = 0;
    for (int i = 0; i < 4; ++i) value |= static_cast<std::uint32_t>(data[offset + i]) << (8 * i);
    return value;
}
void resign_last_frame(Bytes& data, std::size_t offset) {
    constexpr char domain[] = "neuro/search-journal/frame/v1";
    const auto size = little32(data, offset);
    const auto digest_offset = offset + 44 + size;
    if (digest_offset + 32 != data.size()) throw std::runtime_error("test requires last frame");
    blake3_hasher hasher;
    blake3_hasher_init(&hasher);
    blake3_hasher_update(&hasher, domain, sizeof(domain) - 1);
    blake3_hasher_update(&hasher, data.data() + offset, 44 + size);
    blake3_hasher_finalize(&hasher, data.data() + digest_offset, 32);
}

void invalid_files(const fs::path& root) {
    const auto valid = root / "valid-for-corruption.journal";
    {
        SearchJournal journal(valid, "corruption-peer");
        journal.next_connection();
        auto b = journal.bind(job(), seed(), 2, 1);
        journal.begin(b.work_id, 0);
        journal.finish(b.work_id, 0, AttemptOutcome::found);
        journal.send_intent(b.work_id, 0, 1, 2);
    }
    const auto original = read(valid);
    const auto reject_file = [&](Bytes bytes, const std::string& name) {
        const auto path = root / (name + ".journal");
        write(path, bytes);
        rejects([&] { SearchJournal journal(path, "corruption-peer"); }, name);
        require(read(path) == bytes, "invalid journal must remain byte-for-byte unchanged: " + name);
    };
    reject_file({}, "existing-empty");
    reject_file(Bytes{1, 2, 3}, "short-length-prefix");
    auto altered = original; altered.back() ^= 1; reject_file(altered, "bad-frame-checksum");
    altered = original; altered[13] ^= 1; reject_file(altered, "bad-frame-parent");
    altered = original; altered[4] ^= 1; reject_file(altered, "bad-sequence");
    altered = original; altered[0] = 0xff; altered[1] = 0xff; reject_file(altered, "oversize-frame");
    for (std::size_t removed : {std::size_t{1}, std::size_t{16}, std::size_t{33}})
        reject_file(Bytes(original.begin(), original.end() - static_cast<std::ptrdiff_t>(removed)), "torn-tail-" + std::to_string(removed));
    altered = original; altered.push_back(0); reject_file(altered, "trailing-partial-frame");
    reject_file(Bytes(4 * 1024 * 1024 + 1, 0), "oversize-file");
    std::size_t last = 0;
    for (std::size_t position = 0; position < original.size();) {
        last = position;
        position += little32(original, position) + 76;
    }
    altered = original;
    // The last frame is an intent. Give it a different connection while
    // retaining a valid digest. Loading must validate semantics as well as hash.
    altered[last + 44 + 9] = 2;
    resign_last_frame(altered, last);
    reject_file(altered, "hash-valid-wrong-connection");
    altered = original; altered[last + 44] = 255;
    resign_last_frame(altered, last);
    reject_file(altered, "hash-valid-unknown-record");
    altered = original; altered[last + 44 + 5] = 1;
    resign_last_frame(altered, last);
    reject_file(altered, "hash-valid-unclaimed-attempt");
    rejects([&] { SearchJournal missing(root / "missing" / "journal", "peer"); }, "missing parent directory");
#ifndef _WIN32
    const auto symlink = root / "symlink.journal";
    fs::create_symlink(valid, symlink);
    rejects([&] { SearchJournal journal(symlink, "corruption-peer"); }, "symlink journal");
    const auto hardlink = root / "hardlink.journal";
    fs::create_hard_link(valid, hardlink);
    rejects([&] { SearchJournal journal(hardlink, "corruption-peer"); }, "hardlinked journal");
    fs::remove(hardlink);
#endif
}

void capacities_and_failed_write(const fs::path& root) {
    {
        SearchJournal journal(root / "capacity.journal", "capacity-peer");
        journal.next_connection();
        for (unsigned i = 0; i < 64; ++i) {
            auto value = job(); value.job_id += std::to_string(i);
            require(journal.bind(value, seed(), 1, i + 1).work_id == i + 1, "retained identity capacity");
        }
        auto value = job(); value.job_id += "overflow";
        rejects([&] { journal.bind(value, seed(), 1, 65); }, "work identity overflow");
        value = job(); value.job_id += "0";
        const auto old = journal.bind(value, seed(), 1, 66);
        require(old.work_id == 1 && old.resumed, "capacity does not evict original identity");
    }
#ifndef _WIN32
    const auto path = root / "write-failure.journal";
    SearchJournal journal(path, "failure-peer");
    journal.next_connection();
    const auto bound = journal.bind(job(), seed(), 2, 1);
    // flock is cooperative. Simulate outside corruption before append; the
    // expected-offset check must poison this handle, not continue from RAM.
    fs::resize_file(path, fs::file_size(path) - 1);
    rejects([&] { journal.begin(bound.work_id, 0); }, "changed file during locked lifetime");
    require(!journal.healthy(), "failed write poisons journal");
    rejects([&] { journal.bind(job(), seed(), 2, 1); }, "no-op bind after failed write");
    rejects([&] { journal.next_connection(); }, "new connection after failed write");
    contains(journal.report_json(), "\"poisoned\":true");
    contains(journal.report_json(), "\"attempts_consumed\":0");
#endif
}

} // namespace

int main() {
    try {
        TemporaryDirectory directory;
        lifecycle(directory.root / "lifecycle.journal");
        unfinished_and_identity(directory.root / "unfinished.journal");
        invalid_files(directory.root);
        capacities_and_failed_write(directory.root);
        std::cout << "search journal: " << checks << " checks, " << rejections << " expected rejections passed\n";
        return 0;
    } catch (const std::exception& error) {
        std::cerr << "search journal failure: " << error.what() << '\n';
        return 1;
    }
}
