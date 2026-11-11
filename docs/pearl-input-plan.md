# Iteration 3 entry plan (historical)

Implemented in iteration 3. See `docs/pearl-input.md` for the actual interface,
build, observed comparisons and remaining boundaries. The notes below preserve
the source-inspection plan that led to that implementation.

This is an implementation entry point for iteration 3, based on source inspection
of CPPminer `5b68506af5e6433ee5c25aa9e6fbb00b537590aa`. It is not completed
integration or protocol/proof evidence.

## Existing source to reuse

- `upstream/cppminer/src/common/cp_noise.c` and `include/cp_noise.h`: job key,
  matrix commitments, noise seeds and noisy matrices.
- `upstream/cppminer/src/cpu/cp_cpu_worker.cpp`: `zero_b_prepare_job` and
  `zero_b_prepare_attempt` show the current CPU attempt preparation.
- `upstream/cppminer/build.sh`: references BLAKE3 1.8.5. Pin the official source
  and preserve its notices separately; do not run this installer into the clean
  reference submodule. The dependency's exact commit is still to be resolved.

## Order matters

| Stage | Existing API / requirement |
|---|---|
| Mining config | Set `pearl_set_cutlass_fused(0)`, `pearl_set_contiguous_tiles(1)` and `pearl_set_contiguous_tile_shape(8,16)` before computing the job key |
| Job key | `pearl_job_key` with a validated 76-byte header; upstream uses a fixed 128-byte temporary buffer for header plus 52-byte config |
| Signal matrices | Zero-B CPU path uses zero Bt and `pearl_fill_random_a_row` for a deterministic A mutation; `pearl_generate_ab` is a separate general path |
| Commitments | Compute on **signal** A/Bt before adding noise, via existing keyed digest/commitment helpers |
| Noise seeds | `pearl_b_noise_seed_from_bt`, `pearl_a_noise_seed_from_hash` or `pearl_derive_noise_seeds`; preserve `salted = cert_version >= 3` and M/N binding |
| Noisy matrices | `pearl_build_noisy_a/b` or permutation pairs plus `pearl_fuse_noise_row_a_buf/b_buf` |
| Arithmetic | Feed **noisy** A and transposed noisy Bt into `fullk::Input` |

Two traps must remain explicit: the default config is scattered, so silently
using it would change the job key; `pearl_precompute_noise` in this pin is an
unimplemented function returning -1. Its presence in the header is not a working
precomputation path.

## Small executable target

Build `neuro_pearl_input_test` for M=N=128, K=4096, with a fixed header and seed:

1. Pin and build portable BLAKE3 C code with appropriate disabled-SIMD definitions.
   Confirm that build recipe against the selected source; it is not tested yet.
2. Build the required upstream noise/hash component and run its existing
   `pearl_run_alignment_tests()` vectors without Cargo or proof stubs.
3. Set the contiguous 8x16 config explicitly. Construct zero A/Bt signals, mutate
   one A row with the existing deterministic row generator, then calculate the
   commitments, seeds and noisy data.
4. Preserve independently checked intermediate vectors: job key, commitments,
   seeds, selected noisy rows and matrix byte hashes. Include malformed lengths
   and config mismatch checks at the adapter boundary.
5. Compare all full-K transcript words and final C through the existing CPU
   oracle/emulator; compare transcript with the pinned donor scalar path.

Full builders call job-cancellation helpers that pull in host state. The row
APIs avoid those calls and offer a smaller initial offline dependency boundary.
If unused code is removed with section GC, verify the actual link rather than
adding fake cancellation/hash/proof implementations. A row-vs-full-builder
differential can follow once the real host dependencies are linked.

Passing this target would establish a CPU preprocessing-to-arithmetic path.
Jackpot/target handling, proof generation and verification, GPU execution, pool
shares and mining speed remain separate subsequent evidence.
