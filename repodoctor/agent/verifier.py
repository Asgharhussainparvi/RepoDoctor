"""The Verifier: the ONLY source of a trustworthy `verified=True`.

A VerificationResult is sealed with an HMAC computed from a per-process secret.
The state machine refuses to enter SUCCESS unless the seal checks out, so no other
code path (or accidental edit of a result) can produce a success.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets

from pydantic import BaseModel, ConfigDict, Field

from repodoctor.tools.models import Counts, RunTestsOutput

_SECRET = secrets.token_bytes(32)

NOT_VERIFIED_MESSAGE = "The proposed fix did not pass verification. RepoDoctor will not claim success."
_MESSAGES = {
    "verified": "Verification passed.",
    "baseline_not_reproduced": "The original failure could not be reproduced, so a fix cannot be verified.",
    "tests_timed_out": "Test execution exceeded the allowed time. " + NOT_VERIFIED_MESSAGE,
    "test_run_failed": "The tests could not be run after the patch. " + NOT_VERIFIED_MESSAGE,
    "test_output_unusable": "The test output could not be interpreted. " + NOT_VERIFIED_MESSAGE,
    "tests_still_failing": NOT_VERIFIED_MESSAGE,
    "new_failures": "The patch introduced new test failures. " + NOT_VERIFIED_MESSAGE,
    "test_count_decreased": "Fewer tests ran after the patch than before. " + NOT_VERIFIED_MESSAGE,
}


def _total(c: Counts) -> int:
    return c.passed + c.failed + c.errors + c.skipped + c.xfailed + c.xpassed


def _seal(payload: dict) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hmac.new(_SECRET, canonical.encode("utf-8"), hashlib.sha256).hexdigest()


class VerificationResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    verified: bool
    reason_code: str
    message: str
    before: Counts
    after: Counts
    previously_failing: list[str] = Field(default_factory=list)
    still_failing: list[str] = Field(default_factory=list)
    new_failures: list[str] = Field(default_factory=list)
    files_changed: list[str] = Field(default_factory=list)
    patch_sha256: str = ""
    seal: str = ""

    def authentic(self) -> bool:
        """True only if this exact result was produced by Verifier.verify in this process."""
        return bool(self.seal) and hmac.compare_digest(self.seal, _seal(self.model_dump(exclude={"seal"})))


class Verifier:
    @staticmethod
    def verify(*, before: RunTestsOutput, after: RunTestsOutput, patch_sha256: str,
               files_changed: list[str]) -> VerificationResult:
        before_ids = [f.test_id for f in before.failed_tests]
        after_ids = [f.test_id for f in after.failed_tests]
        still = [t for t in after_ids if t in before_ids]
        new = [t for t in after_ids if t not in before_ids]

        if not before.ok or before.status != "failed":
            reason = "baseline_not_reproduced"
        elif not after.ok:
            reason = "test_run_failed"
        elif after.status == "timeout":
            reason = "tests_timed_out"
        elif after.status in ("unparseable", "error", "no_tests"):
            reason = "test_output_unusable"
        elif after.status == "failed":
            reason = "new_failures" if new and not still else "tests_still_failing"
        elif after.status == "passed" and (
            _total(after.counts) < _total(before.counts) or after.counts.passed < before.counts.passed
        ):
            reason = "test_count_decreased"
        elif after.status == "passed" and after.counts.failed == 0 and after.counts.errors == 0:
            reason = "verified"
        else:  # pragma: no cover - defensive: any unclassified state is not a success
            reason = "test_output_unusable"

        fields = {
            "verified": reason == "verified",
            "reason_code": reason,
            "message": _MESSAGES[reason],
            "before": before.counts.model_dump(),
            "after": after.counts.model_dump(),
            "previously_failing": before_ids,
            "still_failing": still,
            "new_failures": new,
            "files_changed": list(files_changed),
            "patch_sha256": patch_sha256,
        }
        return VerificationResult(**fields, seal=_seal(fields))
