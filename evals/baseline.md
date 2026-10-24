# Evaluation report: baseline (replay)

24 golden requirements; 25 extracted.

| Metric | Value | Baseline | Meaning |
|---|---|---|---|
| Extraction recall | 100.0% |  | share of the true requirements that were found |
| Extraction precision | 96.0% |  | share of extracted items that are true requirements |
| Retrieval hit rate | 100.0% |  | the expected source was among the retrieved documents |
| Retrieval MRR | 100.0% |  | how high the expected source ranked (1.0 = first) |
| Answer rate | 100.0% |  | requirements for which the model produced a usable answer |
| Citation support | 100.0% |  | citations whose quote is really in the cited document |
| Unsupported figures (model) | 0.0% |  | answers where the model wrote a figure not in its sources |
| Unsupported figures (delivered) | 0.0% |  | the same, after the app removed them (should be 0) |
| Abstention correctness | 100.0% |  | declined when the knowledge base has nothing, answered when it has |
| State correctness | 100.0% |  | answers that ended in the right state (answered, or a placeholder for a person) |
| False answers | 0.0% |  | unsupported requirements that were answered anyway |
| Answer correctness | 100.0% |  | answers that state the expected fact and not the stale one |
| Compliance correctness | 100.0% |  | Yes/Partial/No matches what the sources support |
| Claim support | 87.8% |  | the writer's own claims that the verifier found supported by their evidence |
| Confidence separation | 0.0% |  | edit rate of the Low band minus the High band's, on answers that are answers (positive = the band ranks answers correctly) |
| High-band answers | 9 |  | answers given the High band (placeholders are not counted) |
| Medium-band answers | 2 |  | answers given the Medium band |
| Low-band answers | 6 |  | answers given the Low band |
| High band: needs a change | 0.0% |  | share of High-band answers a person would have to change (wrong fact, wrong compliance, invented figure) |
| Low band: needs a change | 0.0% |  | the same for the Low band |
| Injected errors caught | 100.0% |  | deliberately wrong claims the verifier did not accept (target 80%+) |
| Correct claims rejected | 0.0% |  | correct claims the verifier did not accept (target 10% or less) |
| Caught without a model | 100.0% |  | fabricated quotes, uncited claims and wrong figures caught by rules alone |
| Latency per requirement (mean) | 14.11 s | | recorded from the live run |
| Latency per requirement (p95) | 19.55 s | | recorded from the live run |
| Input tokens per requirement | 2320 | | recorded from the live run |
| Output tokens per requirement | 370 | | recorded from the live run |
| Verification latency per requirement (mean) | 11.86 s | | recorded from the live run |
| Verification input tokens per call | 632 | | recorded from the live run |
| Verification output tokens per call | 98 | | recorded from the live run |

## Cases

| Case | File | Expected | Extracted | Matched | Missed | Extra |
|---|---|---|---|---|---|---|
| clinic-pdf | lakeside_health_rfp.pdf | 6 | 6 | 6 | - | 0 |
| council-docx | harbourview_council_rfp.docx | 8 | 9 | 8 | - | 1 |
| retail-xlsx | northshore_retail_requirements.xlsx | 10 | 10 | 10 | - | 0 |

Extracted but not in the golden list:

- council-docx: shall be no longer than 200 words

## Injected claims (32; 0 not handled as expected)

All handled as expected.

## Problems (2 of 24 requirements)

- **clinic-pdf / C2** The provider must enforce multi-factor authentication for administrators.: 1 claim(s) the evidence does not support: Multi-factor authentication uses Microsoft Entra ID and supports proto
- **retail-xlsx / RT-03** Provide online training for administrators and end users.: 1 claim(s) the evidence does not support: Online training sessions are conducted live.
