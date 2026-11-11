# Pearl input preparation and arithmetic consumer

Iteration 3 connects the pinned donor's actual signal, commitment and noise
functions to the full-K arithmetic interface. CPU comparisons pass, and the
CUDA consumer compiles/links for SM120 with a passing host preparation mode.
The jobs use fixed synthetic headers and seeds. They are not pool-issued work,
valid proofs, hardware execution evidence or a mining-speed measurement.

## Native interface

`native/include/neuro/pearl_input.hpp` declares `neuro::pearl::Request`,
`PreparedInput` and `prepare_zero_b(request)`. The implementation is in
`native/pearl/pearl_input.cpp`.

| Field | Accepted value |
|---|---|
| Header | Exactly 76 bytes |
| Attempt seed | Exactly 32 bytes |
| M and N | Multiples of 128 in [128,1024] |
| K / rank | Fixed 4096 / 128 |
| Certificate version | 2 (legacy seed binding) or 3 (salted dimension binding) |
| Transcript geometry | Contiguous 8x16 |
| Host byte order | Little-endian, checked before donor calls |

These dimensions are bounded test/adapter dimensions, not a complete mining
panel scheduler. Rejecting unsupported certificate versions avoids silently
assuming that later network versions have identical semantics.

Each call constructs the **first attempt of the zero-B strategy**: signal A and
signal Bt start at zero; the existing donor generator changes one A row using
the supplied attempt seed. This is the row strategy used by the donor CPU worker,
but there is no persistent sequence of mutations, cached job state or live pool
lifecycle in this adapter yet.

The preparation order is explicit:

1. Validate lengths, dimensions, certificate version and tile configuration.
2. Select contiguous 8x16 and disable the donor's fused-CUTLASS layout before
   calculating the job key. The default scattered config would change that key.
3. Generate the changed signal A row; signal Bt remains zero.
4. Compute keyed BLAKE3 commitments of the **signals**, padded to 1024-byte chunks.
5. Derive B then A noise seeds. Version 3 first binds commitment roots to their
   respective M/N dimension using the protocol's domain salts.
6. Build permutation pairs and invoke the actual row noise functions to obtain
   signed INT8 noisy A and transposed noisy Bt.

`PreparedInput` retains signal matrices, job key, commitments, noise seeds and
the changed row, plus a ready `fullk::Input`. The chosen signal bounds are
-64..63, noise -63..63, hence noisy A stays within -127..126. Zero-B noisy Bt
stays within -63..63. The INT8 full-K accumulator bound remains valid.

The donor's layout selectors are process globals. A private mutex serializes
our adapter calls, and each valid call resets the selectors. External direct
calls to donor globals are outside that lock. Invalid requests cannot alter
those globals. This bounded synchronous adapter does not implement job
preemption; cancellation tests described below exercise the donor builders.

## Pinned dependencies and linking

CPPminer remains unchanged at
`5b68506af5e6433ee5c25aa9e6fbb00b537590aa`.
Official BLAKE3 1.8.5 is a separate submodule at
`93a431c78a52d7ccf0f366f106467f5070e6075e`.
`upstream/sources.lock.json` records provenance and license files. Preserve both
submodules and notices in the final handoff archive.

`cmake/Blake3.cmake` compiles `blake3.c`, `blake3_dispatch.c` and
`blake3_portable.c`, with the SIMD/NEON backends disabled. No Cargo, assembly,
TBB or upstream installer is needed for this C library. This portable baseline
is chosen for reproducible correctness work, not a hashing-speed claim.

`neuro_pearl_donor` links real `cp_noise.c`, `cp_job_ctrl.cpp` and `cp_pool.cpp`.
Function/data sections plus linker garbage collection discard the unused network
client. The cancellation implementation and pool connection-state accessor are
genuine donor code; no fake cancellation, hash, noise or proof functions satisfy
the tests. No pool connection is started.

The larger `Case33` arithmetic differential retains the documented GCC/Linux
LTO recipe. It explicitly verifies Scalar/ExactS8S8 selection before running,
allowing unused SIMD branches to be discarded. Other toolchains require their
own observed link/execution evidence. Windows build/run remains unverified.

## Independent fixtures and comparisons

`tools/generate_pearl_fixtures.py` independently implements signal row generation,
configuration encoding, commitments, root binding, seed chains, permutation pairs,
row noise and incremental scalar arithmetic. It never calls native/candidate code.
The cryptographic primitive comes from the official Rust-backed Python BLAKE3
binding, with package version and wheel SHA-256 pinned in `requirements-fixtures.txt`.
We do not implement a new cryptographic primitive.

Three fixtures isolate legacy/V3 binding and rectangular dimensions:

| Case | Shape | Certificate |
|---|---|---|
| `legacy_zero_b` | 128x128 | 2 |
| `v3_zero_b` | 128x128 | 3 |
| `v3_rectangular_zero_b` | 128x256 | 3 |

All use `header[i]=(i*7+11)&255` and `seed[i]=(i*i*13+i*17+29)&255` for their
respective 76/32-byte lengths. The resulting changed A row is 26. Keeping the
same header/seed makes changes from certificate binding and dimensions visible.

The committed header contains 16,384 complete transcript words and intermediate
32-byte expectations. The Python oracle computes all 65,536 final C cells and
stores BLAKE3/SHA-256 of their exact little-endian INT32 bytes. The native test
checks both CPU paths against the transcript, their final C fingerprints against
Python, and every final cell directly between the two native paths. A fingerprint
comparison is distinguished from a byte-for-byte comparison in the reports.

Observed primary results across the three cases:

- 32,768 exact transcript comparisons (oracle and emulator against Python gold).
- 65,536 exact final C comparisons between the native paths, plus full independent
  final C fingerprints.
- 1,248 digest-byte comparisons covering keys, roots, binding, seeds and matrix
  fingerprints; 7,340,032 signal/noisy matrix bytes covered by those fingerprints.
- 29 malformed requests rejected before donor calls; reset/replay after deliberately
  selecting a different donor layout also passes.

The separate donor differential checks 3,670,016 noisy matrix bytes directly
between row APIs and the full builders, 12,288 A-row bytes against the full A
generator, and 384 seed bytes through alternative genuine wrappers. Each native
arithmetic path agrees with 16,384 donor scalar transcript words.

Additional tests execute 35 official BLAKE3 vectors in three modes (105 cases;
17,115 compared output bytes), existing donor alignment/root/seed vectors, and
13 real cancellation lifecycle checks. Cancellation reaches the actual A
generator and both noisy builders; they return -1 as expected.

## CUDA consumer and observed limit

`native/cuda/pearl_probe.cu` links preparation and the existing full-K CUDA kernel.
Before any explicit device operation, it checks the prepared inputs and CPU
reference against independent fixed expectations. Its GPU mode then compares
the transcript directly with the committed words and final C with the validated
CPU reference. Each case runs with and without a final C output buffer.

After a CUDA build, this mode needs no GPU and has passed:

```bash
./build/cuda/neuro_pearl_probe --host-only
```

It reports three cases, 16,384 CPU transcript comparisons, zero GPU launches,
`gpu_execution_observed=false`, and null device/compute capability. CTest runs
this host mode automatically. CUDA binary startup registration is not a claim
that a GPU calculation took place.

On the actual target, after the small instruction and full-K probes pass:

```bash
timeout 60s ./build/cuda/neuro_pearl_probe --device 0
```

`--case v3_zero_b` selects one case. Windows needs the equivalent `.exe` path and
an external worker timeout. A complete hardware PASS would contain six launches,
32,768 GPU transcript comparisons and 65,536 final C comparisons. Those are
future expected counters; hardware execution has not occurred here.

The result schema is `neuro.pearl_probe.v1`. Success/failure both explicitly
separate GPU execution, proof verification, pool connection and mining evidence.
There is no hashrate claim. Full build commands are in `docs/development.md`.

## Next boundary

Build the genuine Rust proof implementation and an independent verification path,
including deliberately invalid proof/witness cases. The current vectors establish
the preprocessing/arithmetic component for the pinned source, not cryptographic
proof validity, target/jackpot handling or acceptance by a live pool. Preserve this
baseline while adding those checks.
