"""Read-only Amazon Bedrock check for RepoDoctor. Prints no secrets.

  python scripts/check_bedrock.py                       # list Claude/Nova inference profiles you can see (free)
  python scripts/check_bedrock.py --quotas              # show 'tokens per day' quotas (free, read-only)
  python scripts/check_bedrock.py --ping MODEL_ID       # ONE tiny request (~20 tokens): small but NOT free

Requires: pip install -r requirements-aws.txt
"""

from __future__ import annotations

import argparse
import os
import sys
import time

NEEDLES = ("anthropic", "amazon.nova")


def filter_profiles(
    summaries: list[dict], needles: tuple[str, ...] = NEEDLES
) -> list[tuple[str, str]]:
    """Pure helper: (profile id, status) for profiles whose id mentions one of the needles, sorted."""
    rows = [
        (s.get("inferenceProfileId", ""), s.get("status", "?"))
        for s in summaries
        if any(n in s.get("inferenceProfileId", "") for n in needles)
    ]
    return sorted(rows)


def describe_auth(session) -> list[str]:
    lines = []
    if os.environ.get("AWS_BEARER_TOKEN_BEDROCK"):
        lines.append(
            "Bedrock API key: AWS_BEARER_TOKEN_BEDROCK is set (used for Bedrock calls)."
        )
    else:
        lines.append(
            "Bedrock API key: AWS_BEARER_TOKEN_BEDROCK is NOT set in this shell."
        )
    creds = session.get_credentials()
    if creds is None:
        lines.append("IAM credentials: none found.")
    else:
        lines.append(
            f"IAM credentials: found (access key ending ...{(creds.access_key or '')[-4:]})."
        )
    return lines


def explain(code: str, message: str = "") -> str:
    text = message.lower()
    if code == "ThrottlingException" and "tokens per day" in text:
        return (
            "Your account's daily token quota for this model is probably 0 (common on new accounts), "
            "so waiting will not help. Run with --quotas to check, then request an increase in the "
            "Service Quotas console or open an AWS Support case."
        )
    if code == "AccessDeniedException" and "not available for this account" in text:
        return "This model is not enabled for your account. Check the model catalog in the Bedrock console."
    hints = {
        "AccessDeniedException": "No permission for this model or action. Check the model access / use-case form "
        "in the Bedrock console and the IAM policy of this identity.",
        "ValidationException": "Often means the bare model id cannot be called on demand. Use an inference "
        "profile id with a 'us.' or 'global.' prefix.",
        "ResourceNotFoundException": "Model or profile not found in this region.",
        "ThrottlingException": "Rate limited. Wait a minute and retry.",
        "UnrecognizedClientException": "The credentials were rejected (expired or wrong key).",
        "ExpiredTokenException": "The key or session token has expired.",
    }
    return hints.get(code, "See the error message above.")


DEFAULT_QUOTA_WORDS = ("haiku", "nova", "sonnet")


def filter_quotas(
    quotas: list[dict], words: tuple[str, ...] = DEFAULT_QUOTA_WORDS
) -> list[tuple[str, float, bool]]:
    """Pure helper: (name, value, adjustable) for 'tokens per day' quotas mentioning one of the words."""
    rows = []
    for q in quotas:
        name = q.get("QuotaName", "")
        low = name.lower()
        if "tokens per day" in low and any(w in low for w in words):
            rows.append(
                (name, float(q.get("Value", 0)), bool(q.get("Adjustable", False)))
            )
    return sorted(rows)


def show_quotas(session, words: tuple[str, ...]) -> int:
    from botocore.exceptions import BotoCoreError, ClientError

    print("\nBedrock 'tokens per day' quotas (Service Quotas):")
    try:
        client = session.client("service-quotas")
        quotas, token = [], None
        while True:
            kwargs = {
                "ServiceCode": "bedrock",
                "MaxResults": 100,
                **({"NextToken": token} if token else {}),
            }
            page = client.list_service_quotas(**kwargs)
            quotas += page.get("Quotas", [])
            token = page.get("NextToken")
            if not token:
                break
    except (ClientError, BotoCoreError) as exc:
        print(f"  Could not read quotas: {exc}")
        print(
            "  This identity may lack servicequotas:ListServiceQuotas. Check in the console instead:"
        )
        print(
            "  AWS Console > Service Quotas > AWS services > Amazon Bedrock > search 'tokens per day'."
        )
        return 1
    rows = filter_quotas(quotas, words)
    for name, value, adjustable in rows:
        flag = "   <-- ZERO: calls will be throttled" if value == 0 else ""
        print(f"  {value:>16,.0f}  adjustable={adjustable}  {name}{flag}")
    if not rows:
        print("  (no matching quotas listed)")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "us-east-1"))
    parser.add_argument(
        "--ping",
        metavar="MODEL_ID",
        help="send one tiny Converse request to this model/profile id",
    )
    parser.add_argument(
        "--quotas",
        action="store_true",
        help="show Bedrock 'tokens per day' quotas (free, read-only)",
    )
    parser.add_argument(
        "--quiet-profiles", action="store_true", help="do not print the profile list"
    )
    args = parser.parse_args(argv)

    try:
        import boto3
        from botocore.exceptions import BotoCoreError, ClientError
    except ImportError:
        print("boto3 is not installed. Run: pip install -r requirements-aws.txt")
        return 2

    session = boto3.Session(region_name=args.region)
    print(f"Region: {args.region}")
    print(f"boto3: {boto3.__version__}")
    for line in describe_auth(session):
        print(line)

    status = 0
    print("\nInference profiles that mention Claude or Nova:")
    try:
        client = session.client("bedrock")
        summaries, token = [], None
        while True:
            kwargs = {
                "maxResults": 100,
                "typeEquals": "SYSTEM_DEFINED",
                **({"nextToken": token} if token else {}),
            }
            page = client.list_inference_profiles(**kwargs)
            summaries += page.get("inferenceProfileSummaries", [])
            token = page.get("nextToken")
            if not token:
                break
        rows = filter_profiles(summaries)
        if args.quiet_profiles:
            print(f"  {len(rows)} profiles listed (hidden by --quiet-profiles)")
        for profile_id, state in [] if args.quiet_profiles else rows:
            print(f"  {profile_id}   [{state}]")
        if not rows:
            print("  (none listed)")
        print(
            "Listing shows what exists, not what your account is allowed to call. Use --ping to test one."
        )
    except (ClientError, BotoCoreError) as exc:
        print(f"  Could not list profiles: {exc}")
        print(
            "  This can be normal with an API key only. Try --ping with a model id instead."
        )
        status = 1

    if args.quotas:
        status = max(status, show_quotas(session, DEFAULT_QUOTA_WORDS))

    if args.ping:
        print(f"\nPing: {args.ping}")
        try:
            runtime = session.client("bedrock-runtime")
            started = time.monotonic()
            response = runtime.converse(
                modelId=args.ping,
                messages=[
                    {
                        "role": "user",
                        "content": [{"text": "Reply with the single word: ok"}],
                    }
                ],
                inferenceConfig={"maxTokens": 16, "temperature": 0},
            )
            elapsed = time.monotonic() - started
            text = response["output"]["message"]["content"][0].get("text", "").strip()
            usage = response.get("usage", {})
            print(f"  OK in {elapsed:.1f}s, reply: {text!r}")
            print(
                f"  tokens in/out: {usage.get('inputTokens')}/{usage.get('outputTokens')}"
            )
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "ClientError")
            message = exc.response.get("Error", {}).get("Message", "")
            print(f"  FAILED: {code}: {message}")
            print(f"  Hint: {explain(code, message)}")
            status = 1
        except BotoCoreError as exc:
            print(f"  FAILED: {exc}")
            status = 1
    return status


if __name__ == "__main__":
    sys.exit(main())
