# Official Pearl / CPPminer source differences

Comparison reference: [`0dbdb2ffd8b81193a5079bb67a59aecc10ad501c`](https://github.com/pearl-research-labs/pearl/tree/0dbdb2ffd8b81193a5079bb67a59aecc10ad501c).
Donor snapshot: [`5b68506af5e6433ee5c25aa9e6fbb00b537590aa`](https://github.com/1640675651/CPPminer/tree/5b68506af5e6433ee5c25aa9e6fbb00b537590aa).

The counts below describe changes from the official reference to CPPminer's vendored copy. They do not establish which commit was originally imported or who authored an individual changed line. Both references remain unmodified.

## Checks relevant to verification

The official `zk-pow/src/api/verify.rs` additionally rejects MoE under its dense-only rule, applies `PearlCircuitParams::sanity_check` and applies `check_rank_penalty`. The donor copy omits those calls.

The official `zk-pow/src/ffi/plain_proof.rs` checks Merkle `total_leaves` against the declared matrix dimensions and uses checked integer conversions. Those guards are absent in the donor copy. The official-only regression test is `zk-pow/tests/plain_proof_dimension_wraparound.rs`.

The scalar/noise fixtures remain useful, but acceptance by the donor verifier alone does not establish acceptance by the official verifier. `rust/pearl-proof-check` calls the pinned official implementation directly; its own failures remain failures.

The two changed `pearl-blake3` files replace `as_chunks` with `chunks_exact` and alter the `Debug` implementation. Plonky2 has identical contents for all 207 files; the executable mode of `pgo-profile.sh` differs (`100644` in the donor, `100755` in the official tree).

## Complete changed-file inventory

All file-level Git blob IDs and SHA-256 values are recorded in [pearl-proof-provenance.json](pearl-proof-provenance.json). Added/removed line counts use a direct line comparison; they are descriptive, not a semantic equivalence test.

| File | Added in donor | Removed from official |
|---|---:|---:|
| `zk-pow/Cargo.toml` | 3 | 1 |
| `zk-pow/bindings/go/Cargo.lock` | 2 | 2 |
| `zk-pow/bindings/go/src/common.rs` | 2 | 3 |
| `zk-pow/bindings/go/src/mine.rs` | 27 | 60 |
| `zk-pow/bindings/go/src/plain.rs` | 17 | 33 |
| `zk-pow/bindings/go/src/verify.rs` | 4 | 38 |
| `zk-pow/src/api/proof_utils.rs` | 9 | 16 |
| `zk-pow/src/api/prove.rs` | 24 | 9 |
| `zk-pow/src/api/sanity_checks.rs` | 3 | 14 |
| `zk-pow/src/api/verify.rs` | 1 | 7 |
| `zk-pow/src/bin/prove_verify.rs` | 6 | 7 |
| `zk-pow/src/circuit/chip/matmul/constraints.rs` | 1 | 1 |
| `zk-pow/src/circuit/chip/matmul/trace.rs` | 2 | 2 |
| `zk-pow/src/circuit/compat_test.rs` | 8 | 19 |
| `zk-pow/src/circuit/pearl_circuit.rs` | 0 | 10 |
| `zk-pow/src/circuit/pearl_noise.rs` | 19 | 3 |
| `zk-pow/src/circuit/pearl_trace.rs` | 5 | 5 |
| `zk-pow/src/ffi/mine.rs` | 41 | 77 |
| `zk-pow/src/ffi/plain_proof.rs` | 20 | 162 |
| `zk-pow/src/v1/api/proof_utils.rs` | 0 | 9 |
| `zk-pow/src/v1/api/sanity_checks.rs` | 2 | 4 |
| `zk-pow/src/v1/circuit/chip/matmul/constraints.rs` | 1 | 1 |
| `zk-pow/src/v1/circuit/chip/matmul/trace.rs` | 2 | 2 |
| `zk-pow/src/v1/circuit/pearl_trace.rs` | 4 | 4 |
| `zk-pow/src/v1/compat_test.rs` | 4 | 16 |
| `zk-pow/tests/moe_test.rs` | 16 | 36 |
| `pearl-blake3/src/hasher.rs` | 1 | 1 |
| `pearl-blake3/src/merkle.rs` | 1 | 13 |
