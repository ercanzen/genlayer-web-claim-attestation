# { "Depends": "py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6" }
"""WebClaimAttestation — a source-grounded claim attestation primitive for GenLayer.

Anyone can register a natural-language claim together with a public source URL.
GenLayer validators independently fetch that source, independently re-derive a
verdict, and additionally verify that the evidence quote stored on-chain really
occurs in the page they fetched themselves. The agreed verdict is written to
deterministic storage behind a challenge window, after which it is finalized.

The contract is deliberately *not* an "AI decides X" wrapper: the verdict space
is closed, the consensus rule is explicit, and the validator never trusts the
leader's answer — it re-derives the decision and cross-checks the leader's
evidence against source data it fetched itself.
"""

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone

from genlayer import *

# ---------------------------------------------------------------------------
# Domain constants
# ---------------------------------------------------------------------------

VERDICT_SUPPORTED = "SUPPORTED"
VERDICT_CONTRADICTED = "CONTRADICTED"
VERDICT_INSUFFICIENT = "INSUFFICIENT_EVIDENCE"
ALLOWED_VERDICTS = (VERDICT_SUPPORTED, VERDICT_CONTRADICTED, VERDICT_INSUFFICIENT)

STATUS_OPEN = "OPEN"
STATUS_CHALLENGED = "CHALLENGED"
STATUS_FINALIZED = "FINALIZED"

# Error classes. The prefix is what the validator uses to decide whether a
# leader failure is reproducible (agree) or node-local noise (disagree).
ERR_EXPECTED = "[EXPECTED]"    # deterministic business-logic error
ERR_EXTERNAL = "[EXTERNAL]"    # deterministic remote error (4xx, empty page)
ERR_TRANSIENT = "[TRANSIENT]"  # timeouts, 5xx, rate limits
ERR_LLM = "[LLM]"              # malformed model output — force leader rotation

MIN_CLAIM_LEN = 8
MAX_CLAIM_LEN = 500
MAX_URL_LEN = 500
MAX_PAGE_CHARS = 20000
MAX_QUOTE_LEN = 300
MIN_QUOTE_LEN = 12
MAX_RATIONALE_LEN = 600
SHINGLE_WORDS = 8
MAX_CHALLENGE_WINDOW = 60 * 60 * 24 * 30  # 30 days

_TAG_RE = re.compile(r"<[^>]+>")
_SCRIPT_RE = re.compile(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>")
_WS_RE = re.compile(r"\s+")
_PRIVATE_HOST_PREFIXES = ("127.", "10.", "192.168.", "169.254.", "0.")


# ---------------------------------------------------------------------------
# Pure helpers (module level so they stay picklable inside nondet closures)
# ---------------------------------------------------------------------------


def _now() -> int:
    """Transaction timestamp in unix seconds — identical on every validator."""
    return int(datetime.now(timezone.utc).timestamp())


def _zero_address() -> Address:
    return Address(b"\x00" * 20)


def _addr_hex(value) -> str:
    """Canonical lowercase 0x hex for an address-like value.

    Addresses are keyed by hex string rather than by object so that storage
    keys stay stable across SDK/runtime boundaries.
    """
    if isinstance(value, (bytes, bytearray)):
        return "0x" + bytes(value).hex()
    as_hex = getattr(value, "as_hex", None)
    if isinstance(as_hex, str):
        return as_hex.lower()
    text = str(value).strip().lower()
    if not text.startswith("0x"):
        raise gl.vm.UserError(f"{ERR_EXPECTED} invalid address")
    return text


def _normalize(text: str) -> str:
    return _WS_RE.sub(" ", str(text)).strip().lower()


def _strip_html(raw: str) -> str:
    without_scripts = _SCRIPT_RE.sub(" ", raw)
    text = _TAG_RE.sub(" ", without_scripts)
    text = (
        text.replace("&nbsp;", " ")
        .replace("&amp;", "&")
        .replace("&quot;", '"')
        .replace("&#39;", "'")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
    )
    return _WS_RE.sub(" ", text).strip()


def _clamp(text: str, limit: int) -> str:
    value = _WS_RE.sub(" ", str(text)).strip()
    return value[:limit]


def _require_claim(claim: str) -> str:
    value = _WS_RE.sub(" ", str(claim)).strip()
    if len(value) < MIN_CLAIM_LEN:
        raise gl.vm.UserError(f"{ERR_EXPECTED} claim must be at least {MIN_CLAIM_LEN} characters")
    if len(value) > MAX_CLAIM_LEN:
        raise gl.vm.UserError(f"{ERR_EXPECTED} claim must be at most {MAX_CLAIM_LEN} characters")
    return value


def _require_url(url: str, field: str) -> str:
    value = str(url).strip()
    if not value:
        raise gl.vm.UserError(f"{ERR_EXPECTED} {field} is required")
    if len(value) > MAX_URL_LEN:
        raise gl.vm.UserError(f"{ERR_EXPECTED} {field} is too long")
    if not value.startswith("https://"):
        raise gl.vm.UserError(f"{ERR_EXPECTED} {field} must be an https:// URL")
    for bad in (" ", "\t", "\n", "\r", '"', "'", "<", ">", "\\"):
        if bad in value:
            raise gl.vm.UserError(f"{ERR_EXPECTED} {field} contains an illegal character")
    host = value[len("https://") :].split("/")[0].split("@")[-1].split(":")[0].lower()
    if host == "localhost" or host.endswith(".local") or host.endswith(".internal"):
        raise gl.vm.UserError(f"{ERR_EXPECTED} {field} must be a public host")
    for prefix in _PRIVATE_HOST_PREFIXES:
        if host.startswith(prefix):
            raise gl.vm.UserError(f"{ERR_EXPECTED} {field} must be a public host")
    if not host or "." not in host or host.endswith("."):
        raise gl.vm.UserError(f"{ERR_EXPECTED} {field} has no valid host")
    return value


def _extract_json(raw) -> dict:
    # Depending on the runtime, `exec_prompt` hands back either the raw model
    # string or an already-decoded object. Accept both.
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, (bytes, bytearray)):
        raw = bytes(raw).decode("utf-8", errors="replace")
    text = str(raw).strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise gl.vm.UserError(f"{ERR_LLM} model did not return a JSON object")
    try:
        parsed = json.loads(text[start : end + 1])
    except Exception:
        raise gl.vm.UserError(f"{ERR_LLM} model returned malformed JSON")
    if not isinstance(parsed, dict):
        raise gl.vm.UserError(f"{ERR_LLM} model did not return a JSON object")
    return parsed


def _grounded(quote: str, corpus: str) -> bool:
    """True when `quote` demonstrably comes from `corpus`.

    Exact containment is tried first. Because two nodes can fetch slightly
    different renderings of the same page, a contiguous 8-word shingle is
    accepted as well — tolerant to edge truncation, still impossible to satisfy
    with a fabricated quote.
    """
    needle = _normalize(quote)
    haystack = _normalize(corpus)
    if len(needle) < MIN_QUOTE_LEN or not haystack:
        return False
    if needle in haystack:
        return True
    words = needle.split()
    if len(words) < SHINGLE_WORDS:
        return False
    for i in range(len(words) - SHINGLE_WORDS + 1):
        if " ".join(words[i : i + SHINGLE_WORDS]) in haystack:
            return True
    return False


def _fetch_page(url: str) -> str:
    """Fetch one page inside a non-deterministic block and reduce it to text."""
    response = gl.nondet.web.request(url, method="GET")
    status = int(getattr(response, "status", getattr(response, "status_code", 200)) or 200)
    if status in (408, 425, 429) or status >= 500:
        raise gl.vm.UserError(f"{ERR_TRANSIENT} {url} returned HTTP {status}")
    if status >= 400:
        raise gl.vm.UserError(f"{ERR_EXTERNAL} {url} returned HTTP {status}")
    body = getattr(response, "body", b"")
    if isinstance(body, (bytes, bytearray)):
        raw = bytes(body).decode("utf-8", errors="replace")
    else:
        raw = str(body)
    text = _strip_html(raw)
    if len(text) < 40:
        raise gl.vm.UserError(f"{ERR_EXTERNAL} {url} returned no readable text")
    return text[:MAX_PAGE_CHARS]


def _fetch_pages(source_url: str, counter_source_url: str) -> list:
    pages = [(source_url, _fetch_page(source_url))]
    if counter_source_url:
        pages.append((counter_source_url, _fetch_page(counter_source_url)))
    return pages


def _build_prompt(claim: str, pages: list) -> str:
    blocks = []
    for index, (url, text) in enumerate(pages, start=1):
        label = "PRIMARY SOURCE" if index == 1 else "COUNTER SOURCE"
        blocks.append(f"[{label} {index}] {url}\n<<<\n{text}\n>>>")
    sources = "\n\n".join(blocks)
    return (
        "You adjudicate a factual claim strictly against the source text provided below.\n"
        "Use no outside knowledge. If the sources do not settle the claim, say so.\n\n"
        f"CLAIM:\n<<<\n{claim}\n>>>\n\n"
        f"{sources}\n\n"
        "Decide exactly one verdict:\n"
        '- "SUPPORTED": the sources state or directly entail the claim.\n'
        '- "CONTRADICTED": the sources state or directly entail the opposite of the claim.\n'
        '- "INSUFFICIENT_EVIDENCE": the sources neither settle nor refute the claim.\n\n'
        "Rules:\n"
        "1. `quote` MUST be copied verbatim from a source block, at most "
        f"{MAX_QUOTE_LEN} characters, and must be the single passage that decides the verdict.\n"
        '2. If the verdict is "INSUFFICIENT_EVIDENCE", `quote` MUST be an empty string.\n'
        "3. Never paraphrase inside `quote`. Never invent text that is not in a source block.\n"
        "4. Answer with a single JSON object and nothing else.\n\n"
        'Answer format: {"verdict": "...", "quote": "...", "rationale": "one or two sentences"}'
    )


def _judge_from_pages(claim: str, pages: list) -> dict:
    """Produce a normalized, self-checked verdict from already-fetched pages."""
    raw = gl.nondet.exec_prompt(_build_prompt(claim, pages))
    parsed = _extract_json(raw)

    verdict = str(parsed.get("verdict", "")).strip().upper().replace(" ", "_")
    if verdict not in ALLOWED_VERDICTS:
        raise gl.vm.UserError(f"{ERR_LLM} unknown verdict {verdict!r}")

    quote = _clamp(parsed.get("quote", ""), MAX_QUOTE_LEN)
    rationale = _clamp(parsed.get("rationale", ""), MAX_RATIONALE_LEN)

    if verdict == VERDICT_INSUFFICIENT:
        quote = ""
    else:
        corpus = " ".join(text for _url, text in pages)
        # Anti-hallucination guard: a decisive verdict must rest on a quote that
        # actually occurs in the fetched source. Otherwise it is downgraded
        # rather than trusted. Every node applies this same rule, so it does not
        # by itself create divergence.
        if not _grounded(quote, corpus):
            verdict = VERDICT_INSUFFICIENT
            quote = ""
            rationale = _clamp(
                "Downgraded: the model's decisive quote was not found in the fetched source. "
                + rationale,
                MAX_RATIONALE_LEN,
            )

    return {"verdict": verdict, "quote": quote, "rationale": rationale}


def _shape_ok(payload) -> bool:
    if not isinstance(payload, dict):
        return False
    if payload.get("verdict") not in ALLOWED_VERDICTS:
        return False
    if not isinstance(payload.get("quote", ""), str):
        return False
    if not isinstance(payload.get("rationale", ""), str):
        return False
    if payload["verdict"] == VERDICT_INSUFFICIENT and payload.get("quote"):
        return False
    if payload["verdict"] != VERDICT_INSUFFICIENT and not payload.get("quote"):
        return False
    return True


def _agree_on_leader_error(leader_result, claim: str, source_url: str, counter_source_url: str) -> bool:
    """Leader failed. Reproduce the work and decide whether to agree."""
    leader_message = str(getattr(leader_result, "message", "") or "")
    try:
        _judge_from_pages(claim, _fetch_pages(source_url, counter_source_url))
        return False  # leader failed where this validator succeeded — disagree
    except gl.vm.UserError as err:
        mine = str(getattr(err, "message", "") or str(err))
        if mine.startswith(ERR_EXPECTED) or mine.startswith(ERR_EXTERNAL):
            return mine == leader_message  # deterministic errors must match exactly
        if mine.startswith(ERR_TRANSIENT) and leader_message.startswith(ERR_TRANSIENT):
            return True  # both nodes hit node-local noise — agree, no state change
        return False  # LLM noise: disagree so the network rotates the leader
    except Exception:
        return False


def _adjudicate(claim: str, source_url: str, counter_source_url: str) -> dict:
    """Run one full leader/validator round and return the agreed verdict."""

    def leader_fn():
        return _judge_from_pages(claim, _fetch_pages(source_url, counter_source_url))

    def validator_fn(leader_result) -> bool:
        if not isinstance(leader_result, gl.vm.Return):
            return _agree_on_leader_error(leader_result, claim, source_url, counter_source_url)

        leader = leader_result.calldata
        if not _shape_ok(leader):
            return False

        # 1. Independent re-derivation: the validator fetches the sources itself
        #    and forms its own verdict. The decision field must match exactly.
        try:
            pages = _fetch_pages(source_url, counter_source_url)
            mine = _judge_from_pages(claim, pages)
        except gl.vm.UserError:
            return False
        except Exception:
            return False

        if leader["verdict"] != mine["verdict"]:
            return False

        # 2. Evidence groundedness: the quote the leader wants written on-chain
        #    must occur in the source text this validator fetched itself. This
        #    is what makes a fabricated citation unable to reach consensus even
        #    when two models happen to agree on the verdict.
        if leader["verdict"] != VERDICT_INSUFFICIENT:
            corpus = " ".join(text for _url, text in pages)
            if not _grounded(leader["quote"], corpus):
                return False

        # `rationale` is intentionally not compared: it is free prose and two
        # models will always word it differently.
        return True

    return gl.vm.run_nondet_unsafe(leader_fn, validator_fn)


# ---------------------------------------------------------------------------
# Storage model
# ---------------------------------------------------------------------------


@allow_storage
@dataclass
class Attestation:
    claim: str
    source_url: str
    submitter: Address
    status: str
    verdict: str
    quote: str
    rationale: str
    created_at: u256
    challenge_deadline: u256
    challenger: Address
    counter_source_url: str
    finalized_at: u256
    revisions: u32


# ---------------------------------------------------------------------------
# Contract
# ---------------------------------------------------------------------------


class WebClaimAttestation(gl.Contract):
    attestations: DynArray[Attestation]
    submissions_by: TreeMap[str, u32]
    challenge_window: u256
    owner: Address

    def __init__(self, challenge_window_seconds: u256):
        window = int(challenge_window_seconds)
        if window < 0 or window > MAX_CHALLENGE_WINDOW:
            raise gl.vm.UserError(
                f"{ERR_EXPECTED} challenge_window_seconds must be between 0 and {MAX_CHALLENGE_WINDOW}"
            )
        self.owner = gl.message.sender_address
        self.challenge_window = u256(window)

    # -- writes -------------------------------------------------------------

    @gl.public.write
    def submit_claim(self, claim: str, source_url: str) -> u256:
        """Register a claim, adjudicate it against its source, store the verdict."""
        checked_claim = _require_claim(claim)
        checked_url = _require_url(source_url, "source_url")

        result = _adjudicate(checked_claim, checked_url, "")

        now = _now()
        sender = gl.message.sender_address
        self.attestations.append(
            Attestation(
                claim=checked_claim,
                source_url=checked_url,
                submitter=sender,
                status=STATUS_OPEN,
                verdict=result["verdict"],
                quote=result["quote"],
                rationale=result["rationale"],
                created_at=u256(now),
                challenge_deadline=u256(now + int(self.challenge_window)),
                challenger=_zero_address(),
                counter_source_url="",
                finalized_at=u256(0),
                revisions=u32(0),
            )
        )
        sender_key = _addr_hex(sender)
        self.submissions_by[sender_key] = u32(int(self.submissions_by.get(sender_key, u32(0))) + 1)
        return u256(len(self.attestations) - 1)

    @gl.public.write
    def challenge(self, attestation_id: u256, counter_source_url: str):
        """Re-adjudicate an open attestation against an additional source."""
        index = self._require_index(attestation_id)
        record = self.attestations[index]

        if str(record.status) != STATUS_OPEN:
            raise gl.vm.UserError(f"{ERR_EXPECTED} attestation {index} is not open for challenge")
        if _now() > int(record.challenge_deadline):
            raise gl.vm.UserError(f"{ERR_EXPECTED} challenge window for attestation {index} has closed")

        counter_url = _require_url(counter_source_url, "counter_source_url")
        original_url = str(record.source_url)
        if counter_url == original_url:
            raise gl.vm.UserError(f"{ERR_EXPECTED} counter_source_url must differ from source_url")

        result = _adjudicate(str(record.claim), original_url, counter_url)

        self.attestations[index].verdict = result["verdict"]
        self.attestations[index].quote = result["quote"]
        self.attestations[index].rationale = result["rationale"]
        self.attestations[index].status = STATUS_CHALLENGED
        self.attestations[index].challenger = gl.message.sender_address
        self.attestations[index].counter_source_url = counter_url
        self.attestations[index].revisions = u32(int(record.revisions) + 1)

    @gl.public.write
    def finalize(self, attestation_id: u256):
        """Lock an attestation once its challenge window has elapsed."""
        index = self._require_index(attestation_id)
        record = self.attestations[index]

        if str(record.status) == STATUS_FINALIZED:
            raise gl.vm.UserError(f"{ERR_EXPECTED} attestation {index} is already finalized")
        now = _now()
        if now < int(record.challenge_deadline):
            raise gl.vm.UserError(f"{ERR_EXPECTED} challenge window for attestation {index} is still open")

        self.attestations[index].status = STATUS_FINALIZED
        self.attestations[index].finalized_at = u256(now)

    # -- views --------------------------------------------------------------

    @gl.public.view
    def get_attestation(self, attestation_id: u256) -> dict:
        index = self._require_index(attestation_id)
        record = self.attestations[index]
        return {
            "id": index,
            "claim": str(record.claim),
            "source_url": str(record.source_url),
            "counter_source_url": str(record.counter_source_url),
            "submitter": _addr_hex(record.submitter),
            "challenger": _addr_hex(record.challenger),
            "status": str(record.status),
            "verdict": str(record.verdict),
            "quote": str(record.quote),
            "rationale": str(record.rationale),
            "created_at": int(record.created_at),
            "challenge_deadline": int(record.challenge_deadline),
            "finalized_at": int(record.finalized_at),
            "revisions": int(record.revisions),
        }

    @gl.public.view
    def get_verdict(self, attestation_id: u256) -> str:
        return str(self.attestations[self._require_index(attestation_id)].verdict)

    @gl.public.view
    def get_status(self, attestation_id: u256) -> str:
        return str(self.attestations[self._require_index(attestation_id)].status)

    @gl.public.view
    def total(self) -> u256:
        return u256(len(self.attestations))

    @gl.public.view
    def submissions_of(self, who: Address) -> u32:
        return self.submissions_by.get(_addr_hex(who), u32(0))

    @gl.public.view
    def config(self) -> dict:
        return {
            "owner": _addr_hex(self.owner),
            "challenge_window_seconds": int(self.challenge_window),
            "verdicts": list(ALLOWED_VERDICTS),
            "max_quote_chars": MAX_QUOTE_LEN,
            "max_page_chars": MAX_PAGE_CHARS,
        }

    # -- internal -----------------------------------------------------------

    def _require_index(self, attestation_id: u256) -> int:
        index = int(attestation_id)
        if index < 0 or index >= len(self.attestations):
            raise gl.vm.UserError(f"{ERR_EXPECTED} attestation {index} does not exist")
        return index
