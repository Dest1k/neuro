# Flat offline source handoff

`tools/package_handoff.py` builds the source checkout that the owner will unpack
directly into `/home/jericho/neuro/start/`. Iteration 9 rehearses this layout;
iteration 10 supplies the final archive and DSH starting directive. A successful
package check establishes source identity and portability of the checkout. It
does not establish a working GPU miner, current Windows execution, a connected
DSH billing adapter, or live pool acceptance.

## Layout and retained identity

There is **no enclosing directory in the ZIP**. `AGENTS.md`, the empty
`README.md`, `state/`, `native/`, `tools/`, and both initialized `upstream/`
checkouts appear immediately in the extraction destination. Python 3.11+ and
Git are sufficient to verify the extracted source. No clone, initialization,
credential, provider API, GPU, or network request is needed for that step.

The root includes a newly created `.git` directory with the selected commit and
its reachable history. Unmerged branches, remote-tracking references, unrelated
root objects, local Git configuration, hooks, reflogs, alternate object stores,
worktree pointers, and caches are not copied from the developer's checkout.
Each upstream includes its own embedded `.git` directory. The exact locked
commits and the available shallow history boundaries are preserved. Earlier
upstream history beyond those boundaries is not promised.

Git configuration contains only the portable core profile: LF checkout,
`autocrlf=false`, `symlinks=false`, `filemode=false`, and disabled reflog updates.
There are no active Git remotes or credential helpers in these configurations.
The original, suspended-account URL
`https://github.com/alinescafs3mp-afk/neuro.git` is retained as provenance in the
manifest with `origin_usable:false`. It is not replaced with a different account
or the Friday repository. Upstream URLs remain in the committed source lock and
`.gitmodules`; packaging and verification never fetch from them.

ZIP payloads are regular files, including link-text representations of pinned
BLAKE3 license symlinks. Git's `core.symlinks=false` and the existing candidate
verifier preserve the identity of those committed symlink blobs. Windows
developer mode or symlink privilege is therefore not needed just to extract and
verify the checkout. The ZIP does not preserve executable permission bits;
invoke Python, shell, and PowerShell scripts through the appropriate interpreter.
`filemode=false` prevents that transport detail from making the checkout dirty.
These choices are exercised through real Git extraction tests on Linux; they do
not substitute for an observed Windows run.

All tracked project documentation, licenses, third-party source notices, pinned
source bytes, and historical disabled CI definitions are retained. No active
root `.github/workflows/*.yml` or `*.yaml` is allowed. The no-Actions execution
policy is checked in project state and travels with the package, including for
self-hosted runners. Historical commits and the unchanged vendor trees may
contain historical workflows; their presence does not authorize running them.

## Metadata and exclusions

The only generated files shipped are these four records:

| File | Purpose |
| --- | --- |
| `_work/handoff/MANIFEST.json` | Schema `neuro.handoff.package.v1`; source commit, candidate fingerprint, pins, shallow boundaries, and size/SHA-256 for every payload file |
| `_work/handoff/MANIFEST.sha256` | SHA-256 of the manifest itself |
| `_work/handoff/candidate.json` | Full `neuro.candidate.manifest.v1` source inventory |
| `_work/handoff/verify.py` | Standalone copy of the committed packaging tool, usable without installing a Python package |

The manifest includes the verifier and candidate record. Its own checksum is
stored separately to avoid self-reference. `_work/` is already excluded from
candidate identity, so adding package metadata does not change the source
fingerprint. The external ZIP SHA-256 must be compared with the independently
delivered value: internal checks detect damage but cannot authenticate a ZIP
whose manifest and verifier were both replaced.

The builder copies committed source through fresh local Git clones, not the
developer's working directory. Ignored private profiles, `.env` files, keys,
tokens, budgets, search recovery journals, GPU leases, build outputs, virtual
environments, toolchains, dependency caches, and previous archives stay outside
the package. Force-tracked private/generated files are rejected by the existing
candidate boundary. Untracked or dirty public source is refused; even an ignored
public compiler input cannot silently disappear, because the staged candidate
must match the requested complete source fingerprint. This is not a historical
secret scanner: the retained committed history must itself be suitable for
distribution, as with the earlier offline checkpoints.

## Build a rehearsal or final archive

Commit all intended source changes first. The builder requires a clean committed
root checkout, unmodified initialized upstream sources, a full root history,
and completed iteration 9 or 10 state. It refuses an existing output, an output
inside the source checkout, a wrong commit/fingerprint, a nonempty README,
restored root Actions workflows, or changed execution policy.

Read the selected commit and fingerprint with:

```sh
git rev-parse HEAD
python3 -B -s -E tools/candidate_manifest.py fingerprint --root .
```

Then substitute those exact values in:

```sh
python3 -B -s -E tools/package_handoff.py build \
  --root . \
  --commit FULL_COMMIT_SHA1 \
  --candidate-sha256 FULL_CANDIDATE_SHA256 \
  --output /path/outside/checkout/neuro-iteration-09-handoff-rehearsal.zip
```

The build result records the ZIP SHA-256 and size, source identity, and successful
verification of the fresh staging checkout. It deliberately reports
`relocation_tested:false`; an extraction rehearsal must be observed separately.
An iteration-9 package records `iteration10_launch_archive:false`. Do not rename
that result into the final iteration-10 deliverable. Iteration-10 packaging uses
the final committed source/state and separately supplied DSH directive.

## Extract and verify before use

Use a new, empty destination; do not mix the archive with a previous checkout,
runtime state, or private files. A normal ZIP extractor can unpack the payload.
Python's standard ZIP tool also works:

```sh
python3 -m zipfile -e /path/to/neuro-handoff.zip /home/jericho/neuro/start
cd /home/jericho/neuro/start
python3 -B -s -E _work/handoff/verify.py verify --root .
```

The verifier checks the exact file inventory and all payload hashes before
running Git or project scripts. It then checks sanitized embedded Git metadata,
root history, locked source commits and shallow boundaries, `git fsck`, clean
worktrees, the real `neuroctl source-check`, complete candidate identity, README,
and the persistent execution policy. Run this immediately after extraction,
before builds, editing, adding private configuration, or ordinary Git commands
that may refresh index timestamps. Verification itself uses read-only Git
operations and does not write the checkout.

The package verifier is an arrival check, not the identity check for an evolving
development workspace. Adding a build artifact or changing `.git/index` makes
the original exact payload inventory differ even when source bytes are still
correct. Subsequent work uses the existing source and candidate checks:

```sh
python3 -B -s -E tools/neuroctl.py source-check
python3 -B -s -E tools/candidate_manifest.py fingerprint --root .
```

Read `AGENTS.md`, `state/current.json`, `docs/handoff-runbook.md`, and the final
starting directive before running the handoff preflight or owner-machine work.
The source ZIP does not include Cargo/CUDA/toolchain caches; an offline source
verification does not promise an offline dependency build. Preserve existing
owner runtime journals and budget databases in their original locations; never
reset them to satisfy a fresh-source check or make a denied action possible.

## Regression coverage

`tests/test_handoff_package.py` creates real small Git repositories with two
shallow pinned modules, including a two-boundary history and committed license
symlink. It packages those synthetic sources, extracts the same ZIP into two
unrelated paths (including spaces), and executes the real source and candidate
checks. It also runs the embedded verifier from a different working directory,
checks that verification leaves bytes unchanged, proves private/config/runtime
exclusions, and rejects corrupted or added/missing payloads, unsafe paths,
nonportable metadata, dirty/ignored public changes, wrong identities, README or
Actions regressions, and output overwrite attempts. This tests delivery
mechanics; final source extraction and a fresh native build remain separate
iteration evidence.
