// Offline proof corpus/export and isolated verification process. No pool client.
#include "neuro/pearl_proof.hpp"
#include "fixtures/pearl_golden.hpp"
#include "fixtures/proof_golden.hpp"
#include "cp_proof.h"

#include <algorithm>
#include <array>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <map>
#include <stdexcept>
#include <string>
#include <vector>

namespace {
std::string quote(const std::string& text) {
    constexpr char hex[] = "0123456789abcdef";
    std::string result = "\"";
    for (unsigned char ch : text) {
        if (ch == '"' || ch == '\\') { result += '\\'; result += static_cast<char>(ch); }
        else if (ch < 32) { result += "\\u00"; result += hex[ch >> 4]; result += hex[ch & 15]; }
        else result += static_cast<char>(ch);
    }
    return result + '"';
}

template<class Bytes> std::string hex(const Bytes& bytes) {
    constexpr char digits[] = "0123456789abcdef";
    std::string result;
    for (auto byte : bytes) { result += digits[byte >> 4]; result += digits[byte & 15]; }
    return result;
}

std::vector<std::uint8_t> unhex(const std::string& text, std::size_t bytes) {
    if (text.size() != bytes * 2) throw std::invalid_argument("Wrong hex field length");
    auto digit = [](char ch) -> int {
        if (ch >= '0' && ch <= '9') return ch - '0';
        if (ch >= 'a' && ch <= 'f') return ch - 'a' + 10;
        if (ch >= 'A' && ch <= 'F') return ch - 'A' + 10;
        throw std::invalid_argument("Invalid hex field");
    };
    std::vector<std::uint8_t> result(bytes);
    for (std::size_t i = 0; i < bytes; ++i)
        result[i] = static_cast<std::uint8_t>(digit(text[i * 2]) * 16 + digit(text[i * 2 + 1]));
    return result;
}

std::string read_proof(const std::filesystem::path& path) {
    std::ifstream stream(path, std::ios::binary | std::ios::ate);
    if (!stream) throw std::runtime_error("Cannot open proof file");
    auto length = stream.tellg();
    if (length < 1 || static_cast<std::uint64_t>(length) > neuro::proof::max_proof_b64_bytes)
        throw std::invalid_argument("Proof file outside the supported byte limit");
    std::string result(static_cast<std::size_t>(length), '\0');
    stream.seekg(0);
    if (!stream.read(result.data(), static_cast<std::streamsize>(result.size())))
        throw std::runtime_error("Cannot read complete proof file");
    return result;
}

void write_text(const std::filesystem::path& path, const std::string& value) {
    std::ofstream stream(path, std::ios::binary | std::ios::trunc);
    if (!stream || !stream.write(value.data(), static_cast<std::streamsize>(value.size())))
        throw std::runtime_error("Cannot write proof artifact");
}

std::string gzip_proof(const std::string& proof) {
    std::vector<char> output(neuro::proof::max_proof_b64_bytes + 1, 0);
    std::array<char, 2048> error{};
    if (cp_proof_gzip_b64(proof.c_str(), output.data(), output.size(), error.data(), error.size()) != 0)
        throw std::runtime_error(std::string("Rust gzip failed: ") + error.data());
    return std::string(output.data());
}

int export_corpus(const std::filesystem::path& output) {
    using namespace neuro;
    std::filesystem::create_directories(output);
    std::string manifest = "{\n  \"schema\":\"neuro.proof_corpus.v1\",\n"
        "  \"network_work\":false,\"gpu_execution\":false,\n"
        "  \"pool_target_be_hex\":" + quote(hex(proof_golden::pool_target_be)) + ",\n"
        "  \"pool_nbits\":\"0x1e080000\",\n  \"cases\":[\n";
    std::size_t count = 0;
    for (std::size_t index = 0; index < pearl_golden::fixtures.size(); ++index) {
        const auto& fixture = pearl_golden::fixtures[index];
        const auto& expected = proof_golden::fixtures[index];
        if (std::string(fixture.name) != expected.name)
            throw std::logic_error("Fixture ordering mismatch");
        pearl::Request request;
        request.shape = fixture.shape;
        request.header.assign(fixture.header.begin(), fixture.header.end());
        request.seed.assign(fixture.seed.begin(), fixture.seed.end());
        request.cert_version = fixture.cert_version;
        const auto input = pearl::prepare_zero_b(request);
        for (bool winner : {true, false}) {
            const auto& tile = winner ? expected.winner : expected.loser;
            const std::string name = std::string(fixture.name) + (winner ? "_winner" : "_loser");
            const std::string filename = name + ".proof.b64";
            const std::string zipped_filename = name + ".proof.gz.b64";
            const auto encoded = proof::build_plain(request, input, {tile.row, tile.col});
            write_text(output / filename, encoded);
            write_text(output / zipped_filename, gzip_proof(encoded));
            if (count++) manifest += ",\n";
            manifest += "    {\"name\":" + quote(name) + ",\"fixture\":" + quote(fixture.name) +
                ",\"file\":" + quote(filename) + ",\"gzip_file\":" + quote(zipped_filename) +
                ",\"header_hex\":" + quote(hex(fixture.header)) +
                ",\"cert_version\":" + std::to_string(fixture.cert_version) +
                ",\"row\":" + std::to_string(tile.row) + ",\"col\":" + std::to_string(tile.col) +
                ",\"expected_valid\":" + (winner ? "true" : "false") +
                ",\"jackpot_hash_hex\":" + quote(hex(tile.hash)) + "}";
        }
    }
    manifest += "\n  ]\n}\n";
    write_text(output / "manifest.json", manifest);
    std::cout << "{\"schema\":\"neuro.proof_export.v1\",\"exported\":" << count
              << ",\"manifest\":" << quote((output / "manifest.json").string()) << "}\n";
    return 0;
}

int verify(int argc, char** argv) {
    const std::array<std::string, 4> required{
        "--header-hex", "--proof-base64-file", "--cert-version", "--target-be"};
    std::map<std::string, std::string> args;
    for (int i = 2; i < argc; i += 2) {
        if (i + 1 >= argc) throw std::invalid_argument("Missing option value");
        const std::string flag(argv[i]);
        bool recognized = false;
        for (const auto& name : required) recognized = recognized || name == flag;
        if (!recognized || !args.emplace(flag, argv[i + 1]).second)
            throw std::invalid_argument("Unknown or duplicate option");
    }
    if (args.size() != required.size()) throw std::invalid_argument("Missing required verify option");
    auto header = unhex(args.at("--header-hex"), 76);
    const auto target_bytes = unhex(args.at("--target-be"), 32);
    neuro::proof::Target target{};
    std::copy(target_bytes.begin(), target_bytes.end(), target.begin());
    const auto cert = args.at("--cert-version");
    if (cert != "2" && cert != "3") throw std::invalid_argument("Supported certificate versions: 2, 3");
    const auto result = neuro::proof::verify_plain(header, read_proof(args.at("--proof-base64-file")),
                                                  target, cert == "2" ? 2 : 3);
    std::cout << "{\"schema\":\"neuro.proof_verification.v1\",\"valid\":"
              << (result.valid ? "true" : "false") << ",\"error\":" << quote(result.error) << "}\n";
    return result.valid ? 0 : 1;
}
} // namespace

int main(int argc, char** argv) {
    try {
        if (argc == 3 && std::string(argv[1]) == "export") return export_corpus(argv[2]);
        if (argc >= 2 && std::string(argv[1]) == "verify") return verify(argc, argv);
        throw std::invalid_argument("Usage: neuro_pearl_proof export DIR | verify --header-hex HEX --proof-base64-file FILE --cert-version 2|3 --target-be HEX");
    } catch (const std::exception& error) {
        std::cout << "{\"schema\":\"neuro.proof_verification.v1\",\"status\":\"ERROR\",\"error\":"
                  << quote(error.what()) << "}\n";
        return 2;
    }
}
