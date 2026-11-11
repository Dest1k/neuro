# Complete public candidate identity

Iteration 7 binds an action on the prepositioned Windows checkout to the public
source bytes selected by the Linux controller. This closes the iteration-6 gap
where worker and entrypoint hashes could match while a kernel was different.

## Identity and exclusions

`tools/candidate_manifest.py` inventories all public checkout files, including
dirty tracked edits, untracked additions, unstaged deletions, configuration,
tests, documentation and previous committed reports. An arbitrary `.gitignore`
rule cannot hide a newly added header or CUDA source from this inventory.
Absolute checkout paths, timestamps and root commit IDs are not identity inputs;
the same dirty candidate may be deployed into a different existing checkout.

Each record contains a portable relative path, kind, exact byte length and
SHA-256. The canonical payload has sorted file and submodule lists and ASCII
JSON with sorted keys and no insignificant whitespace. Its SHA-256 is the
candidate identity. The same staged Python implementation serializes and
verifies this format on Linux and Windows; PowerShell does not reimplement JSON
canonicalization. CRLF is not silently normalized: deploy the exact source
bytes, using the project's Git attributes and `core.autocrlf=false` for a new
checkout. A mismatch produces a failure and a bounded list of differing paths.

The fixed excluded output roots are `.git`, `.venv`, `build`, `artifacts`, `runs`,
`_work`, `start` and `dist`; Python bytecode/cache directories are excluded too.
Private local profiles, `.env` settings, key formats and credential filenames
are never read into the manifest. In particular `configs/local.json` and
`configs/*.local.json` remain outside public source identity. Force-tracking a
private or generated input is rejected. Put generated output in the documented
output roots, rather than creating an ignored source beside a real input.

This is public source identity. It does not identify private owner settings,
environment variables, installed compilers/drivers, Cargo caches, binary output
or physical hardware. Those have their separate configuration/provenance and
build/runtime checks. The manifest is not a sandbox for arbitrary hostile code.

## Pinned upstream sources

The root Git index's gitlinks must agree exactly with imported entries in
`upstream/sources.lock.json`. Every pinned checkout must have its expected HEAD
and no modified, staged, untracked or ignored extra files. The verifier reads
each committed file and compares its actual Git-blob hash with the pinned tree;
`assume-unchanged` cannot conceal a modified source byte. SHA-256 identities of
all these files also enter the complete manifest.

Root symlinks, directory links/reparse points, case collisions and unsupported
paths are rejected. The pinned BLAKE3 license symlinks have a narrowly checked
portable representation: an actual link on Linux and the exact committed
link-text file produced by Git for Windows represent the same source. The link
target must resolve inside its own pinned module. It cannot escape the checkout.

The bounded profile allows at most 4,096 files, 32 MiB per file, 256 MiB total
and a 2 MiB manifest. Git metadata subprocesses and Windows verifier processes
also have time/output limits. The implementation uses exact public source;
it does not decide that an uninspected file is irrelevant to a candidate.

## Commands and build gates

```bash
python tools/candidate_manifest.py fingerprint --root .
python tools/candidate_manifest.py create --root . --output artifacts/candidate.json
python tools/candidate_manifest.py verify --root . --manifest artifacts/candidate.json --expected EXPECTED_SHA256
```

The `fingerprint` result is `neuro.candidate.fingerprint.v1`. `create` writes a
`neuro.candidate.manifest.v1` record. The output must be outside public candidate
inputs so the manifest does not try to contain its own hash. `verify` requires
the expected identity and compares the whole reconstructed manifest, including
additional and removed files. Success is `passed:true` and exit 0.

The fixed Windows action uses complete candidate identity for its source
fingerprint. Build and CPU reports must match that identity and their binary
hashes before later gates may start. Editing documentation or adding a committed
report also creates a new complete candidate. Historical evidence should record
the implementation commit/digest it actually tested; it cannot contain its own
final full-tree identity without changing that tree.

## Remote protocol version 2

The private connection profile remains schema version 1. Its recommended
`run-cuda` outer timeout is now 900 seconds to leave room for bounded probes,
replay and the external miner handoff. A shorter owner-selected timeout can stop
the action early; the GPU ownership recovery mechanism must still restore the
owner's miner after the test processes are gone.

For a non-inventory request, the controller creates one candidate manifest and
stages it with the fixed worker, runner, standalone verifier and structured v2
request. The request binds all staged-code hashes, the manifest file hash and
the expected candidate identity. This does not deploy source files. The complete
checkout and pinned sources must already have been copied into place. A stale
or partial deployment fails before its action starts.

The worker checks complete source before launching the bounded action, again
inside the action process before the entrypoint, and after termination. The
last check also runs after failure, timeout or output overflow. A changed or
unverifiable final source invalidates even an otherwise successful child exit.
The v2 report binds the expected identity and observed pre/post identities to
the request. The controller rejects missing or mismatched identities and keeps
transport uncertainty distinct from a confirmed worker outcome. Inventory can
still diagnose a missing checkout and makes no candidate-validation claim.

Keep a checkout fixed while its action runs. Pre/post checks detect ordinary
concurrent edits but do not provide an atomic filesystem snapshot against an
adversarial edit-and-restore race. A source deployment or new experiment must
wait for the current owned action to finish.

## Evidence boundary

Python tests use real temporary Git repositories for source changes, relocation,
ignored additions, private-file exclusion, pinned mutation, path collisions,
symlinks and canonical serialization. Controller tests use synthetic SSH
transports. The Windows whole-worker fixture uses a real small Git checkout and
actual Python verification, but its fixed action is explicitly synthetic. It
tests pre-launch mismatches and edits made during both successful and failing
actions. None of these tests claims a connection to the owner's laptop, actual
source transfer, physical GPU execution or live pool acceptance.
