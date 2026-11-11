# GitHub suspension: local mitigation, 12 November 2026

This is incident remediation after preparation iteration 7, not iteration 8.
Dates in this note use Europe/Moscow.

## Observed facts

- On 11 November, GitHub's API rejected publication with HTTP 403 and
  `Sorry. Your account was suspended`. Iteration-7 tree creation failed before
  a remote commit or branch was published; no iteration-7 Actions run started.
- On 12 November, the owner reported a suspended-account message when signing
  in and said that no explanatory suspension email had arrived.
- GitHub Support subsequently denied reinstatement in ticket #4990916 on
  11 November at 23:03 UTC (12 November at 02:03 Moscow time), citing use of
  GitHub Actions inconsistent with its terms. The reply gave general examples
  involving third-party sites, incentivized activities and general computing;
  it did not identify a specific workflow, step or applicable category. Do not
  describe it as a confirmed finding of cryptomining or all three examples.
  The reply states that restrictions will not be removed and gives no expiry.
  Restoration must not be assumed when planning further work.
- The owner replied with a follow-up and received a service notice that ticket
  #4990916 is closed, cannot be reopened, and replies will not be seen by the
  team. Do not suggest replying to that email thread again or count the notice
  as a second review of the additional arguments. GitHub directs further
  contact to a new ticket through its support portal; this does not guarantee
  reconsideration, and no new ticket has been submitted by the assistant.
- Earlier published workflows used GitHub-hosted Linux and Windows runners.
  Besides compiling code, they executed real CPU matrix/jackpot/proof code and
  a synthetic loopback TCP peer. The saved evidence distinguishes this from
  external-pool share acceptance, GPU execution or operation of the owner's
  SRBMiner. Do not describe those jobs as compilation-only.

The last observed published main was
`c864e1eaf513ccefcf786f412d0b0ac151d2b983`. Review that commit's workflows and
`reports/iteration-06-validation.json` for the prior executed version.
Iteration-7's validation report records its local checks and unrun Windows
changes separately. No earlier success proves that the new Windows code ran.

## Local containment

Both current workflow definitions were moved byte-for-byte from
`.github/workflows/` to `ci/disabled/`, with `.yml.disabled` extensions. No
recognized workflow YAML remains in the active directory. These archived files
include unpublished iteration-7 changes; use the published commit above when
explaining what ran before suspension.

All project builds, arithmetic, proofs, search and GPU checks now belong in the
local development environment or directly on the owner's VM/Windows laptop.
Do not schedule them through GitHub Actions, including self-hosted Actions.
The owner still authorizes bounded local development and owner-hardware checks;
no extra confirmation process is introduced for those already authorized tasks.

This mitigation exists locally. The blocked remote account, remote workflow
settings and published repository have not been modified. Reinstatement does
not automatically authorize restoring the removed triggers. If an older
checkpoint is restored, apply this containment before publishing it.

## Owner instruction after the refusal

The owner explicitly asked that this mistake not be repeated. The execution
policy follows neuro across any account, remote or repository changes and
must be included in the iteration-10 archive and DSH starting directive.
Before selecting a third-party execution service, assess the actual workload
against that service's terms. A test label, CPU-only execution, loopback peer
or free compute allowance is not sufficient evidence of permission.
Keep authorized local development moving; this lesson does not add an approval
procedure or prohibit ordinary CI in unrelated projects.

## Recovery and policy boundary

Use GitHub's official appeal/reinstatement process to request the specific
policy and run details, human review, and the necessary remediation. Do not
claim that a synthetic test is automatically exempt or that an automated
classifier is proven to have caused the suspension. Do not use another account
or alternate credentials to bypass the restriction.

GitHub's Additional Product Terms list cryptomining among prohibited Actions
uses, explicitly including self-hosted runners. This explains why moving the
same Actions job onto the owner's machine is not the remedy. It does not by
itself establish how GitHub classified the particular tests.

- [GitHub Additional Product Terms — Actions](https://docs.github.com/en/site-policy/github-terms/github-terms-for-additional-products-and-features#actions)
- [GitHub appeal and reinstatement policy](https://docs.github.com/en/site-policy/acceptable-use-policies/github-appeal-and-reinstatement)
- [Appeal/reinstatement form](https://support.github.com/contact/reinstatement)
- [Support route when sign-in is unavailable](https://support.github.com/contact/cannot_sign_in)
