# PlainProof source provenance and dependency notices

Audit date: 2026-11-11. This document covers the real Rust PlainProof builder,
the separately pinned official verifier, and their resolved dependencies. It
records source evidence and declared licenses. It does not establish pool
acceptance, a recursive ZK proof, or a complete binary redistribution package.

## Sources and decision

The builder remains the unmodified CPPminer snapshot
[`5b68506af5e6433ee5c25aa9e6fbb00b537590aa`](https://github.com/1640675651/CPPminer/tree/5b68506af5e6433ee5c25aa9e6fbb00b537590aa).
Its `rust/cp-proof-ffi` crate depends on vendored `zk-pow`, `pearl-blake3`, and
the Pearl fork of Plonky2. CPPminer's MIT notice covers its own code; it is not
the license evidence for the nested Pearl dependencies.

The official Pearl comparison and verifier reference is
[`0dbdb2ffd8b81193a5079bb67a59aecc10ad501c`](https://github.com/pearl-research-labs/pearl/tree/0dbdb2ffd8b81193a5079bb67a59aecc10ad501c),
dated 2026-11-05. Its complete Git tree is
`24d81954ef8af5539193f4f6f82136ab443fe2c7`. The GitHub tree response was not
truncated. This is an inspected reference and the actual verifier dependency
pin; it is **not** a claim that CPPminer originally imported that exact commit.

The official [root LICENSE](https://github.com/pearl-research-labs/pearl/blob/0dbdb2ffd8b81193a5079bb67a59aecc10ad501c/LICENSE)
states ISC and identifies separately licensed subprojects, including Plonky2.
There is no component-specific license file under `zk-pow` or `pearl-blake3`
in either inspected tree. Their source relationship to Pearl is established by
the file comparisons below. The official ISC notice is preserved verbatim in
[pearl-ISC.LICENSE](../third_party/notices/pearl-ISC.LICENSE), including its
copyright lines and subproject list. This preserves the Pearl-origin notice;
it does not assign a new license to donor modifications.

| Component in CPPminer | Crates | Source comparison with the official reference | License evidence |
|---|---|---|---|
| `rust/cp-proof-ffi` | `cp-proof-ffi` 0.1.0 | CPPminer-authored wrapper at the donor pin | CPPminer root MIT; Cargo has no license field |
| `third_party/zk-pow` | `zk-pow` 0.1.0, `layout-macro` 0.1.0 | 109 of 135 files have identical contents; 26 differ; official reference has one additional test | Official Pearl root ISC; no nested license or Cargo license field |
| `third_party/pearl-blake3` | `pearl-blake3` 0.1.0 | 5 of 7 files have identical contents; 2 differ | Official Pearl root ISC; no nested license or Cargo license field |
| `third_party/plonky2` | `plonky2`/`starky` 1.1.0; field, util and maybe_rayon 1.0.0 | All 207 files have identical contents; one script's executable mode differs | Workspace Cargo and retained MIT/Apache-2.0 texts |

Plonky2 declares `MIT OR Apache-2.0` in its
[workspace Cargo metadata](https://github.com/pearl-research-labs/pearl/blob/0dbdb2ffd8b81193a5079bb67a59aecc10ad501c/plonky2/Cargo.toml).
Both its root license files and the separate `util/LICENSE-*` files are retained
inside the donor submodule. Their bytes match the official reference. Keep all
four notices when packaging those sources; the neuro MIT license does not
replace them. The root MIT text credits the Plonky2 Authors and Pearl Research
Labs. `pearl-blake3` is a separate Pearl utility crate, not the `blake3` crate or
the separately pinned BLAKE3 C implementation.

## Verification differences matter

The donor's vendored verifier is not byte-identical to the official verifier.
The complete changed-file inventory is in
[pearl-proof-differences.md](../third_party/notices/pearl-proof-differences.md).
In particular, the official
[verify_plain_proof](https://github.com/pearl-research-labs/pearl/blob/0dbdb2ffd8b81193a5079bb67a59aecc10ad501c/zk-pow/src/api/verify.rs)
additionally applies its dense-only MoE rule, circuit parameter sanity check,
and rank penalty. Its
[PlainProof parser](https://github.com/pearl-research-labs/pearl/blob/0dbdb2ffd8b81193a5079bb67a59aecc10ad501c/zk-pow/src/ffi/plain_proof.rs)
also checks declared dimensions against committed Merkle leaf counts and uses
checked dimension conversions. Those guards are absent in the donor copy.

These findings do not justify modifying the reference or weakening a check.
They justify a second executable that consumes the builder's actual output
and runs the official implementation. Dense rank-128 fixtures exercise a
limited common configuration; passing them is not evidence that arbitrary
donor proofs satisfy all official rules.

## Separate official checker

`rust/pearl-proof-check` depends directly on `zk-pow` from the official Git
repository with `rev = "0dbdb2ffd8b81193a5079bb67a59aecc10ad501c"` and
`default-features = false`. Cargo resolves all eight Pearl/Plonky2 git crates
from that same revision. The checker does not use the donor's path crates and
does not implement its own jackpot or Merkle verification.

Its interface is:

```text
pearl-proof-check --header-hex HEADER76_HEX \
  --proof-base64-file PROOF_FILE --cert-version 3 \
  --nbits 0x1e080000
```

`--cert-version` accepts 2 or 3. The default `--nbits` is `0x1e080000`, the
compact encoding of the unscaled test target `2^235`; the official code applies
the protocol's adjustment and rank checks. `--nbits 0` is passed through to the
official verifier for an actual zero-target rejection. The header is decoded
with the official `IncompleteBlockHeader::from_bytes`; the proof uses official
`PlainProof::deserialize_compat`, certificate eligibility, and
`verify_plain_proof`.

The executable prints one JSON result. Exit 0 means the pinned official
PlainProof verifier accepted that input. Exit 1 means argument, file, decoding,
certificate, verification, or panic failure; the `stage` field distinguishes
them. An upstream panic fails the check and does not produce a proof dump.
Input files are bounded to 8 MiB of base64 text. A parent process timeout is
still required for bounded execution.

The checker is built with the project's pinned Rust toolchain; the local
iteration uses Rust/Cargo 1.99.1. The ordinary locked command is:

```sh
cargo build --manifest-path rust/pearl-proof-check/Cargo.toml \
  --release --locked --jobs 2
```

The project build sets a common Cargo target directory so registry dependencies
can be reused. Cargo compilation is sequential with the donor build. Add
`--offline` after dependencies have been fetched; that mode was used for the
local checker build. No model API or pool access is involved.

The initial Git dependency fetch also follows Pearl's CUTLASS submodule. The
observed isolated Cargo Git cache occupied approximately 312 MiB after that
fetch. This is a fetch/storage consequence, not a CUDA build: the checker does
not compile or execute CUTLASS. No copy of the fetched monorepo is vendored into
neuro. The lockfile and Git revision describe how to fetch it again.

## Registry dependency inventory

The donor `cp-proof-ffi/Cargo.lock` resolves 198 packages: 9 path crates and 189
registry packages. Each downloaded registry `.crate` archive was hashed with
SHA-256 and matched to its exact Cargo.lock checksum: **189 of 189 matched**.
All 189 registry packages declare a license in their Cargo metadata. These
declarations and the hashes of standalone top-level notices are recorded in
the provenance manifest. Cargo metadata includes multiple platforms and
optional/development dependencies; these counts are not a claim that every
listed package is linked into one executable.

The official checker lock resolves 193 packages: its local wrapper, 8 official
Git packages, and 184 registry packages. A single normal Cargo resolution was
seeded with the donor lock to preserve compatible registry pins. Cargo replaced
the path packages with official Git packages and removed five unused gzip
dependencies. All 184 shared registry names, versions, and checksums match the
donor lock; BLAKE3 is 1.8.5. No dependency source or checksum was edited to
obtain that result. Subsequent builds use `--locked`.

This is a declared-license inventory, not a full legal or supply-chain review.
Thirteen registry archives have no standalone top-level notice even though
their Cargo metadata declares a license: the manifest lists them and their
available repository/VCS metadata. Some packages keep notices in a parent
repository or inline. A binary redistribution bundle must collect applicable
notices from the exact pinned sources, including nested notices and native
components such as jemalloc; this audit does not claim that bundle is complete.
This remaining packaging work does not block the local proof build or offline
correctness checks.

## Reproducible evidence

[pearl-proof-provenance.json](../third_party/notices/pearl-proof-provenance.json)
contains both repository pins and trees, file-level Git blob IDs for all 349
vendored files, SHA-256 hashes, changed-file details, license hashes, and the
registry inventory. The official tree was obtained through the GitHub Git Trees
API and compared against `git ls-tree` at the donor pin. Downloaded official
files were checked against their tree blob IDs before their bytes were used.

| Evidence | SHA-256 |
|---|---|
| Official ISC notice, 1,285 bytes | `a59c285e9720924e7e293837faa76ed8ed0a5bedcc5f3a858ddc905741550ecc` |
| Donor `rust/cp-proof-ffi/Cargo.lock` | `562277c9eaac079a308b9fa54d923a69b61c0494ae77eefcae14ebb26de21895` |
| Checker `rust/pearl-proof-check/Cargo.lock` | `78711222bd48509ba382cfbaa5e8ae5482978937932a78b4b502ada3fcbceba0` |

The official notice's Git blob is
`46f4e7c87722312a1af1aeb32e905adfb4c1a78b`; Git object IDs and plain-file SHA-256
are distinct hashes. This audit leaves `upstream/sources.lock.json`, both
reference submodules, and the empty root README unchanged. Proof execution
results belong to the iteration validation report, separate from this
provenance evidence.
