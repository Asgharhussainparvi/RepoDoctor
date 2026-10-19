# Using Amazon Bedrock with RepoDoctor

## What is used

- **Amazon Bedrock** (Converse API) for the model, reached through the **Strands Agents SDK**:
  `BedrockModel(model_id, region_name, max_tokens)` inside a Strands `Agent` with three read-only tools and structured outputs
  (`structured_output_model=`). Investigation is a tool-using agent; patching is one tool-less call returning a small edit that RepoDoctor
  turns into a unified diff.
- Verified against the official Strands and AWS documentation (October 2026): model ids for current Claude models are inference
  profiles with a `global.` or `us.` prefix; Bedrock API keys use `AWS_BEARER_TOKEN_BEDROCK`.

## Choosing a model

List what your account can see, then test one cheaply:

```powershell
python scripts/check_bedrock.py --quiet-profiles                     # free, read-only
python scripts/check_bedrock.py --quiet-profiles --ping <profile-id> # ONE tiny billable call (about 20 tokens)
```

Prefer a small, inexpensive model. `global.` profiles avoid the regional premium; `us.` profiles keep requests in the US
(see docs/aws-costs.md). Never use top-tier or restricted model families here: they cost far more and are not needed.

## Authentication

RepoDoctor reads credentials from the environment, never from files in the repository: `AWS_BEARER_TOKEN_BEDROCK` (a Bedrock API key) or
the normal AWS credential chain. Give long-term API keys an expiry date and delete them after the hackathon.

## Troubleshooting

| Error | Meaning | What to do |
|---|---|---|
| `AccessDeniedException ... is not available for this account` | That model is not enabled for your account | Bedrock console: model catalog, request access or complete the use-case form; try another model |
| `ThrottlingException: Too many tokens per day` | The daily token quota for that model is probably 0 (common on new accounts) | Service Quotas > Amazon Bedrock > search "tokens per day": request an increase, or ask AWS Support or the hackathon organisers. Waiting does not help. |
| `ValidationException` | Often a bare model id | Use an inference profile id with `us.` or `global.` |
| `ExpiredTokenException` / `UnrecognizedClientException` | Key expired or wrong | Create a new key |

RepoDoctor reports all of these to the user as "AI service unavailable. Check AWS configuration." and never as a fake result.
The details are in the server log (`model_unavailable type=... code=...`).

## Cost controls

Per-call output cap, 12 model calls while investigating and 6 while patching, a token budget per session (default 200,000), clipped tool
results, at most 3 patch attempts, and real token counts per session (`BedrockEngine.usage_for`; the CLI prints them).
Set `REPODOCTOR_PRICE_INPUT_PER_MTOK` and `REPODOCTOR_PRICE_OUTPUT_PER_MTOK` to see a cost estimate. Details: docs/aws-costs.md.

## Verified vs not yet verified

Verified: the Strands wiring (tool calls, structured output, usage, limits, error mapping) with a real Strands agent and a scripted model;
credentials and profile listing on a real account. **Not yet verified: a successful real model invocation**, because the daily token quota
was zero when tested. Run `python -m pytest -m aws -v -s` once the quota is fixed.
