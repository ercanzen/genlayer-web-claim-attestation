# WebClaimAttestation

**A source-grounded claim attestation primitive built with GenLayer Intelligent Contracts.**

Register a natural-language claim together with a public source URL. GenLayer
validators independently fetch that source, independently re-derive a verdict,
and additionally verify that the evidence quote about to be written on-chain
really occurs in the page *they* fetched. The agreed verdict is stored behind a
challenge window and can be finalized once that window elapses.

```
SUPPORTED · CONTRADICTED · INSUFFICIENT_EVIDENCE
```

---

## The problem

Agents, prediction markets, escrow contracts, and dispute-resolution systems all
need the same thing before they can act: *did this source actually say that?*

Today that question is answered off-chain. Somebody calls a model, gets a
sentence back, and everyone downstream has to trust both the caller and the
model. Two failure modes dominate:

1. **Unverifiable adjudication** — one party decides, everybody else trusts.
2. **Fabricated citations** — the model returns a confident verdict with a quote
   that does not exist in the source. The verdict looks grounded; it is not.

A conventional blockchain cannot fix this: it cannot read a web page, and it
cannot interpret a sentence. An oracle only moves the trust problem to the
oracle operator.

## Why GenLayer

GenLayer is the only environment where the *judgment itself* is what the
network reaches consensus on. This contract uses four GenLayer capabilities
that have no equivalent on a deterministic chain:

| Capability | Used for |
|---|---|
| **Web access from inside a contract** | Every validator fetches the cited source itself — no oracle, no relayer |
| **LLM execution inside the VM** | Deciding whether a page supports, contradicts, or fails to settle a claim |
| **Equivalence Principle** | Turning many independent, non-identical judgments into one agreed verdict |
| **Deterministic transaction clock & storage** | Challenge windows and finalization that every validator computes identically |

## What this is not

It is deliberately **not** a "let the AI decide" demo:

- the verdict space is closed and enumerated, not free text;
- the validator never inspects only the leader's formatting — it re-derives the
  decision from source data it fetched itself;
- a decisive verdict is only accepted when its evidence quote is provably
  present in that independently fetched source;
- every failure mode is classified so that transient noise, remote errors and
  model errors are handled differently by consensus.

---

## Consensus design

The whole contract turns on one question: *what would make it impossible for a
single node to write a false attestation on-chain?* The answer implemented here
is a two-gate validator built on `gl.vm.run_nondet_unsafe`.

```
LEADER                                  VALIDATOR (each, independently)
──────────────────────────────────      ────────────────────────────────────────
fetch source(s)                         fetch the same source(s) itself
strip HTML → text                       strip HTML → text
LLM: verdict + verbatim quote           LLM: its own verdict + quote
self-check: quote in own corpus?        ── gate 1 ──  verdict fields must match
   no → downgrade to INSUFFICIENT       ── gate 2 ──  leader's quote must occur
propose {verdict, quote, rationale}                   in the validator's own text
                                        accept only if both gates pass
```

**Gate 1 — independent re-derivation.** The validator runs the identical
leader function and compares the `verdict` field exactly. `rationale` is never
compared: it is free prose and two models will always word it differently.
This is partial field matching, the pattern GenLayer recommends when a result
mixes an objective decision with subjective text.

**Gate 2 — evidence groundedness.** This is the part that matters. Even if two
models happen to agree on a verdict, the *citation* the leader wants persisted
must survive an independent existence check against text the validator
downloaded itself. Comparison is whitespace-normalized and case-insensitive,
with a contiguous 8-word shingle accepted as a match so that harmless rendering
differences between nodes do not cause false rejections — while a fabricated
quote cannot pass at any tolerance.

**Leader self-check.** Before proposing anything, the leader applies gate 2 to
its own corpus. A decisive verdict whose quote is not present is downgraded to
`INSUFFICIENT_EVIDENCE` with an empty quote rather than proposed and rejected.
Every node applies this same deterministic rule, so it does not itself create
divergence — it just stops hallucinations from ever entering the consensus
round.

**Error classification.** Leader failures are not all equal, so validators do
not treat them equally:

| Prefix | Meaning | Validator behaviour |
|---|---|---|
| `[EXPECTED]` | business-logic error (bad input, wrong state) | must match the leader's message exactly |
| `[EXTERNAL]` | deterministic remote error (4xx, unreadable page) | must match exactly |
| `[TRANSIENT]` | timeout, 429, 5xx | both nodes transient → agree, no state change |
| `[LLM]` | malformed model output | always disagree → rotate to a new leader |

The effect is that node-local noise never locks bad state in, and model noise
forces a retry instead of a coin flip.

---

## State model

State is written **only after** consensus, in deterministic context — never
inside a non-deterministic block.

```python
@allow_storage
@dataclass
class Attestation:
    claim: str                  # normalized claim text
    source_url: str             # https:// URL the verdict was derived from
    submitter: Address
    status: str                 # OPEN → CHALLENGED → FINALIZED
    verdict: str                # SUPPORTED | CONTRADICTED | INSUFFICIENT_EVIDENCE
    quote: str                  # verbatim, consensus-verified evidence ("" if insufficient)
    rationale: str              # model's short reasoning (stored, never compared)
    created_at: u256            # transaction timestamp, unix seconds
    challenge_deadline: u256    # created_at + challenge_window
    challenger: Address         # zero address until challenged
    counter_source_url: str     # "" until challenged
    finalized_at: u256
    revisions: u32
```

Contract storage:

```python
attestations:   DynArray[Attestation]   # id == index, so ids are stable and enumerable
submissions_by: TreeMap[str, u32]       # address hex → submission count
challenge_window: u256                  # seconds, fixed at deploy, max 30 days
owner: Address
```

Addresses are keyed by canonical lowercase hex rather than by object so keys
stay stable across runtime boundaries.

### Lifecycle

```
submit_claim ──► OPEN ──challenge()──► CHALLENGED
                  │                        │
                  └────── finalize() ──────┴──► FINALIZED (immutable)
```

- `challenge()` requires status `OPEN`, a still-open window, and a
  `counter_source_url` different from the original. It re-adjudicates against
  **both** sources through the same two-gate consensus and bumps `revisions`.
- `finalize()` requires `now >= challenge_deadline` and is idempotent-safe:
  a second call reverts.
- A finalized record can never be challenged or re-verdicted.

---

## API

| Method | Kind | Description |
|---|---|---|
| `submit_claim(claim: str, source_url: str) -> u256` | write | Adjudicate a claim against its source; returns the attestation id |
| `challenge(attestation_id: u256, counter_source_url: str)` | write | Re-adjudicate an open attestation against an extra source |
| `finalize(attestation_id: u256)` | write | Lock a record once its challenge window has elapsed |
| `get_attestation(attestation_id: u256) -> dict` | view | Full record |
| `get_verdict(attestation_id: u256) -> str` | view | Verdict only |
| `get_status(attestation_id: u256) -> str` | view | Status only |
| `total() -> u256` | view | Number of attestations |
| `submissions_of(who: Address) -> u32` | view | Submissions by an address |
| `config() -> dict` | view | Owner, window, verdict vocabulary, limits |

### Input validation

All validation happens **before** any web or LLM call, so malformed input costs
nothing and can never reach a non-deterministic block:

- claim normalized, 8–500 characters;
- URLs must be `https://`, at most 500 characters, free of whitespace, quotes
  and angle brackets;
- host must be public — `localhost`, `*.local`, `*.internal`, `127.*`, `10.*`,
  `192.168.*`, `169.254.*` and `0.*` are rejected, so a submitter cannot point
  validators at their own network;
- `challenge_window_seconds` is bounded to 30 days at deploy time;
- unknown attestation ids revert rather than returning empty records.

Page text is capped at 20 000 characters and quotes at 300, bounding both prompt
size and on-chain storage.

---

## Usage

```python
# deploy with a 24-hour challenge window
contract = WebClaimAttestation(challenge_window_seconds=86400)

attestation_id = contract.submit_claim(
    "In 2026 more than half of national electricity came from renewable sources.",
    "https://example.org/reports/2026/energy",
)

contract.get_attestation(attestation_id)
# {'verdict': 'SUPPORTED',
#  'quote': 'renewable sources accounted for slightly more than half',
#  'status': 'OPEN', 'challenge_deadline': 1789137600, ...}

# somebody disagrees and brings a second source
contract.challenge(attestation_id, "https://example.org/reports/2026/energy-correction")
# → verdict becomes CONTRADICTED, status CHALLENGED, revisions 1

# after the window closes
contract.finalize(attestation_id)
```

### Who this primitive is for

- **Escrow / dispute contracts** — settle "was the deliverable published?" against a URL.
- **Prediction markets** — resolve an outcome against a named source with an auditable citation.
- **Agent-to-agent commerce** — an agent can point at an attestation id instead of asking another agent to trust its screenshot.
- **Reputation and moderation systems** — a claim about a listing or account, adjudicated against the page itself.

Other contracts can read `get_verdict` / `get_attestation` and act on
`status == "FINALIZED"`.

---

## Testing

```bash
pip install genlayer-test        # Python 3.12+
pytest tests/ -v
```

33 direct-mode tests, ~1 second, no Docker and no network:

- **Storage & config** — deploy parameters, bounded challenge window, counters per submitter.
- **Happy path** — supported verdict stored with a grounded quote, deadline arithmetic, `INSUFFICIENT_EVIDENCE` stores no quote.
- **Anti-hallucination** — a fabricated quote is downgraded, not trusted; malformed and out-of-vocabulary model output reverts with `[LLM]`.
- **Input validation** — 9 parametrized rejection cases, each asserting that no attestation was created.
- **Remote failures** — 4xx classified `[EXTERNAL]`, 5xx classified `[TRANSIENT]`.
- **Challenge** — re-adjudication flips the verdict, double challenge rejected, identical counter-source rejected, expired window rejected (via `direct_vm.warp`).
- **Finalization** — rejected before the deadline, succeeds after, second call rejected, finalized records cannot be challenged.
- **Consensus** — validator agrees on a matching verdict; disagrees when its own verdict differs; **rejects a leader payload whose quote does not exist in the source**; rejects malformed leader payloads; disagrees when the leader errored but the validator succeeded.
- **Isolation** — snapshot/revert round-trip.

Web and LLM calls are mocked with `direct_vm.mock_web` / `direct_vm.mock_llm`,
and consensus behaviour is exercised with `direct_vm.run_validator()` after
swapping the mocks — which is how a dissenting validator is simulated.

Before deploying to a testnet, run the official linter as well:

```bash
genvm-lint check contracts/web_claim_attestation.py
```

---

## Limitations

Stated plainly, because a primitive that hides them is not reusable:

1. **A source can change after finalization.** The attestation records what the
   page said at adjudication time; it does not archive the page. Content
   addressing or an on-chain excerpt hash would be the next step.
2. **Groundedness is not truthfulness.** Gate 2 proves the quote exists in the
   cited source. It does not prove the source is right. Garbage in, attested
   garbage out — by design; the contract attests to sources, not to reality.
3. **One challenge round.** The current lifecycle allows a single challenge
   before finalization. Multi-round appeals with staking would need economic
   weight behind challenges.
4. **No stake or fee.** Challenges are free, so spam prevention is out of scope
   here. In production the challenge should be bonded.
5. **Rendering differences.** Heavily client-rendered pages may yield little
   text via `web.request`; `gl.nondet.web.render(url, mode='text')` is the
   better fetcher for those and is a natural extension.
6. **Prompt-injection surface.** A hostile page can address the model directly.
   Gate 2 blunts the worst outcome — an injected verdict still needs a real
   quote — but adversarial hardening of the prompt is future work.
7. **Language coverage** depends on the validator LLM configuration; no explicit
   multilingual normalization is done.

---

## Layout

```
contracts/web_claim_attestation.py   Intelligent Contract
tests/test_web_claim_attestation.py  33 direct-mode tests
README.md
```

Built against GenVM runner `py-genlayer` v0.2.12 (`genlayer-test` 0.29.x).

## License

MIT
