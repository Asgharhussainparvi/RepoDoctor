# AWS usage and costs

RepoDoctor uses ONE AWS service: **Amazon Bedrock** (model inference), through the Strands Agents SDK.
Nothing else is created in AWS: no servers, databases, queues or storage. The sandbox, API, UI and SQLite
database all run locally.

## Pricing (verify before the demo)

Prices change. These were the figures I found in October 2026; confirm them on the Bedrock pricing page.

| Model | Input / output per 1M tokens | Source |
|---|---|---|
| Claude Haiku 5.5 | $0.10 / $0.50 (prompts up to 100K tokens; much higher above) | Anthropic pricing page |
| Claude Haiku 4.5 | $1 / $5 | Anthropic pricing page |

Regional ("us.") inference profiles carry a premium over "global." profiles for Claude 4.6 and later
(Anthropic pricing page: 1.1x). A session's cost is `input_tokens * input_price + output_tokens * output_price`.
Set `REPODOCTOR_PRICE_INPUT_PER_MTOK` / `REPODOCTOR_PRICE_OUTPUT_PER_MTOK` and RepoDoctor prints an estimate
from the real token counts Bedrock reports.

## What drives cost

1. Model calls per session: investigation (several tool-using turns) plus one call per patch attempt.
2. Tokens per call: failure excerpt, file contents and earlier-attempt feedback.
3. The model chosen.

## Safeguards built

| Safeguard | Where |
|---|---|
| At most 3 patch attempts per session | `AgentConfig.max_patch_attempts` |
| At most 25 agent tool calls per session | `ToolLimits.max_tool_calls` |
| At most 12 model calls while investigating, 6 while patching (a hook aborts runaway loops) | `BedrockSettings` |
| Token budget per session (default 200,000) checked before every model call | `REPODOCTOR_MAX_SESSION_TOKENS` |
| Output cap per model response (default 2,048 tokens) | `REPODOCTOR_MODEL_MAX_TOKENS` |
| Tool results clipped to 12,000 characters; file reads capped at 400 lines; test output excerpt 6,000 characters | tools and prompts |
| Patching is a single tool-less call: the model returns a small edit and RepoDoctor builds the diff | `BedrockEngine.propose_patch` |
| All automated tests use scripted fake models; real-AWS tests are opt-in (`REPODOCTOR_RUN_AWS_TESTS=1`) | `tests/integration` |
| Real token usage is counted per session | `BedrockEngine.usage_for` |

## Estimate (an assumption, not a measurement)

About 12 model calls of roughly 8K input and 500 output tokens each is about 96K input and 6K output tokens.
At Haiku 5.5 prices that is roughly 1-2 cents per session. Replace this with the numbers the CLI prints
after your first real runs.

## Shutting down

There is nothing to shut down in AWS: no resources are created. To stop all spending, stop calling the model.
To revoke access: Bedrock console > API keys > deactivate or delete the key. Long-term keys should have an
expiry date and be deleted after the hackathon.

## Checking what you spent

AWS Billing console > Bills / Cost Explorer, filter by service "Amazon Bedrock". Billing data lags, so rely on
the limits above for protection, not on the console.
