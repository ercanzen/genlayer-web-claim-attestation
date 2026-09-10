"""Direct-mode tests for the WebClaimAttestation Intelligent Contract.

Run with:  pytest tests/ -v
"""

import json
from datetime import datetime, timedelta, timezone

import pytest

CONTRACT = "contracts/web_claim_attestation.py"

SOURCE_URL = "https://example.org/reports/2026/energy"
COUNTER_URL = "https://example.org/reports/2026/energy-correction"

PRIMARY_HTML = """
<html><head><title>Grid report</title><style>.x{color:red}</style></head>
<body>
  <h1>2026 grid report</h1>
  <p>In 2026 the national grid produced 41.2 terawatt hours of electricity,
     of which renewable sources accounted for slightly more than half.</p>
  <script>console.log("ignore me")</script>
  <p>The figure was confirmed by the independent auditor in March.</p>
</body></html>
"""

COUNTER_HTML = """
<html><body>
  <h1>Correction notice</h1>
  <p>The previously published figure was wrong. Renewable sources accounted for
     only 38 percent of national electricity production in 2026, not a majority.</p>
</body></html>
"""

CLAIM = "In 2026 more than half of national electricity came from renewable sources."

SUPPORTED_ANSWER = json.dumps(
    {
        "verdict": "SUPPORTED",
        "quote": "renewable sources accounted for slightly more than half",
        "rationale": "The report states renewables were slightly above half of production.",
    }
)

CONTRADICTED_ANSWER = json.dumps(
    {
        "verdict": "CONTRADICTED",
        "quote": "Renewable sources accounted for only 38 percent of national electricity production in 2026",
        "rationale": "The correction notice puts renewables well below half.",
    }
)

INSUFFICIENT_ANSWER = json.dumps(
    {
        "verdict": "INSUFFICIENT_EVIDENCE",
        "quote": "",
        "rationale": "The page never breaks production down by source.",
    }
)

FABRICATED_ANSWER = json.dumps(
    {
        "verdict": "SUPPORTED",
        "quote": "the ministry certified that renewables reached seventy one percent of supply",
        "rationale": "Quote invented by the model; it appears nowhere on the page.",
    }
)

PROMPT_PATTERN = r"adjudicate a factual claim"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _mock_primary(vm):
    vm.mock_web(r"energy$", {"status": 200, "body": PRIMARY_HTML})


def _mock_counter(vm):
    vm.mock_web(r"energy-correction$", {"status": 200, "body": COUNTER_HTML})


def _deploy(direct_deploy, window_seconds=86400):
    return direct_deploy(CONTRACT, window_seconds)


def _iso_after(seconds):
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


# ---------------------------------------------------------------------------
# storage / configuration
# ---------------------------------------------------------------------------


def test_config_is_stored_on_deploy(direct_deploy):
    contract = _deploy(direct_deploy, 3600)
    config = contract.config()
    assert config["challenge_window_seconds"] == 3600
    assert config["verdicts"] == ["SUPPORTED", "CONTRADICTED", "INSUFFICIENT_EVIDENCE"]
    assert contract.total() == 0


def test_rejects_absurd_challenge_window(direct_vm, direct_deploy):
    with direct_vm.expect_revert("challenge_window_seconds"):
        _deploy(direct_deploy, 60 * 60 * 24 * 400)


# ---------------------------------------------------------------------------
# happy path
# ---------------------------------------------------------------------------


def test_supported_claim_is_attested(direct_vm, direct_deploy):
    _mock_primary(direct_vm)
    direct_vm.mock_llm(PROMPT_PATTERN, SUPPORTED_ANSWER)

    contract = _deploy(direct_deploy)
    attestation_id = contract.submit_claim(CLAIM, SOURCE_URL)
    assert attestation_id == 0

    record = contract.get_attestation(0)
    assert record["verdict"] == "SUPPORTED"
    assert record["status"] == "OPEN"
    assert "more than half" in record["quote"]
    assert record["source_url"] == SOURCE_URL
    assert record["counter_source_url"] == ""
    assert record["revisions"] == 0
    assert record["finalized_at"] == 0
    assert record["challenge_deadline"] == record["created_at"] + 86400
    assert contract.total() == 1


def test_insufficient_evidence_stores_no_quote(direct_vm, direct_deploy):
    _mock_primary(direct_vm)
    direct_vm.mock_llm(PROMPT_PATTERN, INSUFFICIENT_ANSWER)

    contract = _deploy(direct_deploy)
    contract.submit_claim(CLAIM, SOURCE_URL)

    record = contract.get_attestation(0)
    assert record["verdict"] == "INSUFFICIENT_EVIDENCE"
    assert record["quote"] == ""


def test_per_submitter_counter(direct_vm, direct_deploy, direct_alice, direct_bob):
    _mock_primary(direct_vm)
    direct_vm.mock_llm(PROMPT_PATTERN, SUPPORTED_ANSWER)
    contract = _deploy(direct_deploy)

    with direct_vm.prank(direct_alice):
        contract.submit_claim(CLAIM, SOURCE_URL)
        contract.submit_claim(CLAIM, SOURCE_URL)
    with direct_vm.prank(direct_bob):
        contract.submit_claim(CLAIM, SOURCE_URL)

    assert contract.submissions_of(direct_alice) == 2
    assert contract.submissions_of(direct_bob) == 1


# ---------------------------------------------------------------------------
# anti-hallucination guard
# ---------------------------------------------------------------------------


def test_fabricated_quote_is_downgraded_not_trusted(direct_vm, direct_deploy):
    _mock_primary(direct_vm)
    direct_vm.mock_llm(PROMPT_PATTERN, FABRICATED_ANSWER)

    contract = _deploy(direct_deploy)
    contract.submit_claim(CLAIM, SOURCE_URL)

    record = contract.get_attestation(0)
    assert record["verdict"] == "INSUFFICIENT_EVIDENCE"
    assert record["quote"] == ""
    assert "Downgraded" in record["rationale"]


def test_malformed_model_output_reverts(direct_vm, direct_deploy):
    _mock_primary(direct_vm)
    direct_vm.mock_llm(PROMPT_PATTERN, "I think it is probably true.")

    contract = _deploy(direct_deploy)
    with direct_vm.expect_revert("[LLM]"):
        contract.submit_claim(CLAIM, SOURCE_URL)


def test_unknown_verdict_reverts(direct_vm, direct_deploy):
    _mock_primary(direct_vm)
    direct_vm.mock_llm(PROMPT_PATTERN, json.dumps({"verdict": "MAYBE", "quote": "x", "rationale": "y"}))

    contract = _deploy(direct_deploy)
    with direct_vm.expect_revert("unknown verdict"):
        contract.submit_claim(CLAIM, SOURCE_URL)


# ---------------------------------------------------------------------------
# input validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "claim,url,expected",
    [
        ("short", SOURCE_URL, "at least"),
        ("x" * 501, SOURCE_URL, "at most"),
        (CLAIM, "http://example.org/a", "https://"),
        (CLAIM, "https://localhost/a", "public host"),
        (CLAIM, "https://127.0.0.1/a", "public host"),
        (CLAIM, "https://192.168.1.9/a", "public host"),
        (CLAIM, "https://nodots/a", "valid host"),
        (CLAIM, "https://example.org/a b", "illegal character"),
        (CLAIM, "", "required"),
    ],
)
def test_invalid_input_is_rejected_before_any_web_call(
    direct_vm, direct_deploy, claim, url, expected
):
    contract = _deploy(direct_deploy)
    with direct_vm.expect_revert(expected):
        contract.submit_claim(claim, url)
    assert contract.total() == 0


def test_unknown_attestation_id_reverts(direct_vm, direct_deploy):
    contract = _deploy(direct_deploy)
    with direct_vm.expect_revert("does not exist"):
        contract.get_attestation(7)


def test_http_error_from_source_reverts(direct_vm, direct_deploy):
    direct_vm.mock_web(r"energy$", {"status": 404, "body": "not found"})
    contract = _deploy(direct_deploy)
    with direct_vm.expect_revert("[EXTERNAL]"):
        contract.submit_claim(CLAIM, SOURCE_URL)


def test_server_error_is_classified_transient(direct_vm, direct_deploy):
    direct_vm.mock_web(r"energy$", {"status": 503, "body": "busy"})
    contract = _deploy(direct_deploy)
    with direct_vm.expect_revert("[TRANSIENT]"):
        contract.submit_claim(CLAIM, SOURCE_URL)


# ---------------------------------------------------------------------------
# challenge
# ---------------------------------------------------------------------------


def test_challenge_re_adjudicates_against_counter_source(direct_vm, direct_deploy, direct_bob):
    _mock_primary(direct_vm)
    direct_vm.mock_llm(PROMPT_PATTERN, SUPPORTED_ANSWER)
    contract = _deploy(direct_deploy)
    contract.submit_claim(CLAIM, SOURCE_URL)
    assert contract.get_verdict(0) == "SUPPORTED"

    direct_vm.clear_mocks()
    _mock_primary(direct_vm)
    _mock_counter(direct_vm)
    direct_vm.mock_llm(PROMPT_PATTERN, CONTRADICTED_ANSWER)

    with direct_vm.prank(direct_bob):
        contract.challenge(0, COUNTER_URL)

    record = contract.get_attestation(0)
    assert record["verdict"] == "CONTRADICTED"
    assert record["status"] == "CHALLENGED"
    assert record["counter_source_url"] == COUNTER_URL
    assert record["revisions"] == 1
    assert "38 percent" in record["quote"]


def test_second_challenge_is_rejected(direct_vm, direct_deploy):
    _mock_primary(direct_vm)
    _mock_counter(direct_vm)
    direct_vm.mock_llm(PROMPT_PATTERN, SUPPORTED_ANSWER)
    contract = _deploy(direct_deploy)
    contract.submit_claim(CLAIM, SOURCE_URL)

    direct_vm.clear_mocks()
    _mock_primary(direct_vm)
    _mock_counter(direct_vm)
    direct_vm.mock_llm(PROMPT_PATTERN, CONTRADICTED_ANSWER)
    contract.challenge(0, COUNTER_URL)

    with direct_vm.expect_revert("not open for challenge"):
        contract.challenge(0, COUNTER_URL)


def test_challenge_requires_a_different_source(direct_vm, direct_deploy):
    _mock_primary(direct_vm)
    direct_vm.mock_llm(PROMPT_PATTERN, SUPPORTED_ANSWER)
    contract = _deploy(direct_deploy)
    contract.submit_claim(CLAIM, SOURCE_URL)

    with direct_vm.expect_revert("must differ"):
        contract.challenge(0, SOURCE_URL)


def test_challenge_after_window_is_rejected(direct_vm, direct_deploy):
    _mock_primary(direct_vm)
    direct_vm.mock_llm(PROMPT_PATTERN, SUPPORTED_ANSWER)
    contract = _deploy(direct_deploy, 60)
    contract.submit_claim(CLAIM, SOURCE_URL)

    direct_vm.warp(_iso_after(3600))
    with direct_vm.expect_revert("window"):
        contract.challenge(0, COUNTER_URL)


# ---------------------------------------------------------------------------
# finalization
# ---------------------------------------------------------------------------


def test_finalize_before_deadline_is_rejected(direct_vm, direct_deploy):
    _mock_primary(direct_vm)
    direct_vm.mock_llm(PROMPT_PATTERN, SUPPORTED_ANSWER)
    contract = _deploy(direct_deploy, 86400)
    contract.submit_claim(CLAIM, SOURCE_URL)

    with direct_vm.expect_revert("still open"):
        contract.finalize(0)
    assert contract.get_status(0) == "OPEN"


def test_finalize_after_deadline_locks_the_record(direct_vm, direct_deploy):
    _mock_primary(direct_vm)
    direct_vm.mock_llm(PROMPT_PATTERN, SUPPORTED_ANSWER)
    contract = _deploy(direct_deploy, 3600)
    contract.submit_claim(CLAIM, SOURCE_URL)

    direct_vm.warp(_iso_after(7200))
    contract.finalize(0)

    record = contract.get_attestation(0)
    assert record["status"] == "FINALIZED"
    assert record["finalized_at"] >= record["challenge_deadline"]

    with direct_vm.expect_revert("already finalized"):
        contract.finalize(0)


def test_finalized_record_cannot_be_challenged(direct_vm, direct_deploy):
    _mock_primary(direct_vm)
    direct_vm.mock_llm(PROMPT_PATTERN, SUPPORTED_ANSWER)
    contract = _deploy(direct_deploy, 0)
    contract.submit_claim(CLAIM, SOURCE_URL)
    contract.finalize(0)

    with direct_vm.expect_revert("not open for challenge"):
        contract.challenge(0, COUNTER_URL)


# ---------------------------------------------------------------------------
# consensus / equivalence principle
# ---------------------------------------------------------------------------


def test_validator_agrees_when_it_derives_the_same_verdict(direct_vm, direct_deploy):
    _mock_primary(direct_vm)
    direct_vm.mock_llm(PROMPT_PATTERN, SUPPORTED_ANSWER)
    contract = _deploy(direct_deploy)
    contract.submit_claim(CLAIM, SOURCE_URL)

    assert direct_vm.run_validator() is True


def test_validator_disagrees_when_its_own_verdict_differs(direct_vm, direct_deploy):
    _mock_primary(direct_vm)
    direct_vm.mock_llm(PROMPT_PATTERN, SUPPORTED_ANSWER)
    contract = _deploy(direct_deploy)
    contract.submit_claim(CLAIM, SOURCE_URL)

    direct_vm.clear_mocks()
    _mock_primary(direct_vm)
    direct_vm.mock_llm(PROMPT_PATTERN, INSUFFICIENT_ANSWER)

    assert direct_vm.run_validator() is False


def test_validator_rejects_a_leader_quote_absent_from_the_source(direct_vm, direct_deploy):
    _mock_primary(direct_vm)
    direct_vm.mock_llm(PROMPT_PATTERN, SUPPORTED_ANSWER)
    contract = _deploy(direct_deploy)
    contract.submit_claim(CLAIM, SOURCE_URL)

    forged = {
        "verdict": "SUPPORTED",
        "quote": "the ministry certified that renewables reached seventy one percent of supply",
        "rationale": "forged evidence",
    }
    assert direct_vm.run_validator(leader_result=forged) is False


def test_validator_rejects_malformed_leader_payload(direct_vm, direct_deploy):
    _mock_primary(direct_vm)
    direct_vm.mock_llm(PROMPT_PATTERN, SUPPORTED_ANSWER)
    contract = _deploy(direct_deploy)
    contract.submit_claim(CLAIM, SOURCE_URL)

    assert direct_vm.run_validator(leader_result={"verdict": "SUPPORTED"}) is False
    assert direct_vm.run_validator(leader_result="not a dict") is False
    assert (
        direct_vm.run_validator(
            leader_result={"verdict": "INSUFFICIENT_EVIDENCE", "quote": "x", "rationale": "y"}
        )
        is False
    )


def test_validator_disagrees_when_leader_failed_but_it_succeeds(direct_vm, direct_deploy):
    _mock_primary(direct_vm)
    direct_vm.mock_llm(PROMPT_PATTERN, SUPPORTED_ANSWER)
    contract = _deploy(direct_deploy)
    contract.submit_claim(CLAIM, SOURCE_URL)

    assert direct_vm.run_validator(leader_error=Exception("[TRANSIENT] leader timed out")) is False


def test_state_is_isolated_by_snapshot(direct_vm, direct_deploy):
    _mock_primary(direct_vm)
    direct_vm.mock_llm(PROMPT_PATTERN, SUPPORTED_ANSWER)
    contract = _deploy(direct_deploy)

    snapshot = direct_vm.snapshot()
    contract.submit_claim(CLAIM, SOURCE_URL)
    assert contract.total() == 1

    direct_vm.revert(snapshot)
    assert contract.total() == 0
