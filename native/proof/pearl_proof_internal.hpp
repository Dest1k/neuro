// SPDX-License-Identifier: MIT
#pragma once

#include "neuro/pearl_proof.hpp"

namespace neuro::proof::detail {

inline constexpr std::size_t error_capacity = 4096;
void validate_request(const pearl::Request& request);
void validate_tile(fullk::Shape shape, Tile tile);
void validate_prepared(const pearl::Request& request,
                       const pearl::PreparedInput& input);
void preflight(std::string_view proof_b64);
std::string output_string(const std::vector<char>& output);

} // namespace neuro::proof::detail
