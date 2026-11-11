# neuro: autonomous Pearl miner development

## Read on entry

Read `README.md`, `state/current.json`, and the latest entry in
`docs/iterations/`. Read other documents only when needed. Do not reload the
whole repository, raw logs, dependency trees, or this entire project history
into every model request. The repository is the durable project memory.

## Owner objective and authority

Build a Pearl (PRL) miner through an autonomous DSH development loop. The owner
will supply a Linux VM and a Windows RTX 5080 Laptop test host reachable by SSH.
No human CUDA programming or debugging is available. Choose and implement
ordinary engineering changes, run bounded local tests, fix failures, and keep
useful changes without repeatedly asking the owner to select implementation
details. User instructions override this file.

The current preparation is iteration 1 of 10 in web chat. Iteration 10 must
deliver a complete archive for `/home/jericho/neuro/start/` and a DSH starting
directive. Before then, record the completed iteration and the next concrete
task. Do not describe a plan, an unrun probe, or a CPU test as a working GPU miner.

## Architecture decisions

- CUDA C++17 for GPU kernels; selective inline PTX when measurement justifies it.
- Reuse pinned CPPminer protocol/host/proof code. Keep existing Rust proof code;
  do not rewrite cryptography or the host in Rust merely to change language.
- `upstream/cppminer` is a pinned, unmodified submodule. New implementations live
  in this repository. If an upstream change is needed, keep its patch in this
  repository and apply it to a separate worktree, preserving the reference.
- The upstream CUDA backend is Pascal/SIMT-oriented, not an RTX 50 Tensor Core
  baseline. A compiler architecture flag alone does not make it competitive.
- Root MIT license covers our code. Retain upstream and dependency notices.
  Do not copy Open-Pearl-Miner's custom-license kernel into an MIT-only release.

## Correctness and useful progress

Run `python tools/neuroctl.py source-check` and the relevant checks after code
changes. `cpu-check` is offline and does not connect to a pool. Our GPU probe
only checks an instruction and its data layout; it is not a miner. Full proof,
pool acceptance, sustained GPU operation and Windows execution need their own
observed evidence. Upstream proof stubs must never satisfy a proof gate.

An optimization is useful only if exact results remain correct and repeated
measurements under comparable conditions support it. Keep independent expected
results separate from candidate code. Never weaken a verifier to accept a
candidate or invent timings, shares, cache hits, or successful execution.

Preserve the fastest verified version. After repeated failure of one hypothesis,
record what failed and choose a different concrete approach. Do not build another
agent framework, generalized installer, permissions system, or dashboard as a
substitute for miner progress.

## Costs and operation

The provisional executor is `openai/gpt-6.1-sol`, budgeted without a cache
discount. `minimax/minimax-m3` is the first cheaper challenger. Rates in
`configs/model-prices.json` are the owner's reseller quote, not verified billing.
Unknown cache usage is UNKNOWN, not a zero-cost cache hit. A provider/DSH usage
adapter and actual billing verification remain required before unattended API
spending. Never claim the offline cost tool already controls DSH's API calls.

Keep private endpoint settings in ignored `configs/local.json` and keys in the
environment or the owner's credential store. Never commit, print, or embed
credentials in a command line or archive. Do not inspect unrelated credentials.

Use bounded subprocesses and small GPU jobs first. After a CUDA context error,
restart the worker. Do not retry a hanging candidate forever. Preserve the
owner's existing power/clock profile; TDR changes, overclocking, and driver
replacement are not fixes for incorrect kernels. SSH cannot recover a fully
hung operating system; record this limit honestly.
