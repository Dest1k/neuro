# Ten preparation iterations

The owner requested ten substantive web-chat iterations, starting 2026-11-11.
Advance as far as useful evidence permits in each turn. This sequence can be
reordered when findings justify it; it is not ten mandatory approval gates.

| Iteration | Main delivery | Evidence / boundary |
|---|---|---|
| 1 | Pinned reusable source, executable CPU checks, initial Tensor Core probe, model-cost accounting | Local execution and honest GPU/toolchain status |
| 2 | Standalone full-K INT8 CUDA kernel with transcript interface | Compile for SM120; compare every milestone with CPU oracle when hardware is available |
| 3 | Matrix/noise/commitment integration and deterministic vectors | Exact intermediate values; no proof stub substitutes |
| 4 | Rust proof build and independent verification path | Valid and deliberately invalid proof cases |
| 5 | Miner host/backend adapter, job cancellation and offline pool replay | Complete offline job lifecycle and error recovery |
| 6 | Windows SSH worker and DSH usage/cost bridge | Bounded commands, saved logs; live cache/billing only if endpoint access exists |
| 7 | Bounded optimization loop with preserved baseline | Correctness before timing; no-progress and spending stops |
| 8 | End-to-end recovery and stability work | Broken jobs, disconnects, CUDA errors; physical GPU coverage explicitly distinguished |
| 9 | Fresh-directory handoff rehearsal | Rebuild from pinned sources, resolve missing dependencies, verify package contents |
| 10 | Complete archive + DSH starting directive | Extract layout `/home/jericho/neuro/start/`; checksums, exact status, next executable step |

## What the final archive must contain

- The project and required pinned source content (not empty submodule directories).
- Build/verification commands for Linux orchestration and Windows CUDA target.
- Deterministic correctness vectors and their independent expected results.
- Model route configuration templates and an implemented spending/usage boundary.
- A resumable task state with measured results and explicit untested areas.
- The DSH directive explaining authority, first task, useful-work criteria and
  failure handling without assuming a human CUDA specialist.
- License notices, a content manifest and SHA-256 checksums.

The archive must not include API/GitHub/SSH secrets, wallets, raw private logs,
machine-specific caches, or binaries falsely labelled as tested on the laptop.

## Access constraints

The web environment does not mount the owner's `/home/jericho` and currently has
no connection to the laptop or model reseller. Local CPU execution and GPU
compilation can be prepared here. Hardware runs, real accepted shares, DSH's
actual usage events and reseller billing need the actual endpoints. If they are
still unavailable by iteration10, the handoff must make the remaining tasks
directly executable and state the untested coverage. It must not invent success.
