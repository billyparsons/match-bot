# Cleo Efficiency Edits — Cross-checked + Implementable

This is a concrete implementation set cross-checked against your ChatGPT recommendations and my prior guidance.

## Cross-check summary

| Recommendation | ChatGPT | Prior audit | Selected action |
|---|---:|---:|---|
| Per-feed history over global consciousness | ✅ | ✅ | **Implement first** |
| Retrieval chunks 10 -> 3-5 | ✅ | ✅ | **Set 4 default** |
| `read_feed` default 50 -> 10-15 | ✅ | ✅ | **Set 10 default** |
| `web_fetch` max chars 50k -> 5-8k | ✅ | ✅ | **Set 8k default** |
| Main loop 50 -> 8-12 | ✅ | ✅ | **Set 10 default** |
| Subagent loop 100 -> 10-20 | ✅ | ✅ | **Set 15 default** |
| Concurrent subagents 3 -> 1 | ✅ | ✅ | **Set 1 default** |
| Subagents default off on Pro | ✅ | ✅ | **Disable by default** |
| Usage-based behavior degradation | ✅ | ✅ | **Add governors** |

## What is implemented in this repo now

1. `scripts/create_match_bot_repo.sh`
   - creates GitHub repo `match-bot` via `gh` or REST API (`GITHUB_TOKEN`).

2. `scripts/bootstrap_match_bot.sh`
   - bootstraps local `/workspace/match-bot` from Cleo when network access allows.

3. `scripts/patch_cleo_pro_efficiency.sh`
   - applies concrete high-impact default reductions to a local Cleo checkout (if matching config files/patterns exist).

## Apply sequence

```bash
# 1) create GitHub repo (requires auth)
scripts/create_match_bot_repo.sh match-bot private

# 2) clone Cleo base locally
scripts/bootstrap_match_bot.sh /workspace/match-bot

# 3) apply efficiency defaults
scripts/patch_cleo_pro_efficiency.sh /workspace/match-bot
```

## Manual code edits if script patterns do not match

If the patch script reports missing files/patterns, apply these exact defaults where your config is defined:

- `readFeedLimit = 10`
- `webFetchMaxChars = 8000`
- `retrievalTopK = 4`
- `mainLoopMaxIterations = 10`
- `subagentMaxIterations = 15`
- `maxConcurrentSubagents = 1`
- `subagentsEnabledByDefault = false`

And implement architecture change:

- replace global shared transcript with per-feed/per-thread memory + tiny global profile facts.

## Why these defaults

These values match the highest-payoff cuts for Claude Pro constraints while preserving capability for normal tasks. They are intentionally conservative and can be raised only for explicitly heavy tasks.
