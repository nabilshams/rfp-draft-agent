# Evaluation report: baseline (replay)

25 golden requirements; 25 extracted.

| Metric | Value | Baseline | Meaning |
|---|---|---|---|
| Extraction recall | 100.0% |  | share of the true requirements that were found |
| Extraction precision | 100.0% |  | share of extracted items that are true requirements |
| Extraction recall (PDF) | 100.0% |  | recall on the PDF RFPs |
| Extraction precision (PDF) | 100.0% |  | precision on the PDF RFPs |
| Extraction recall (Word) | 100.0% |  | recall on the Word RFPs |
| Extraction precision (Word) | 100.0% |  | precision on the Word RFPs |
| Extraction recall (spreadsheet) | 100.0% |  | recall on the spreadsheet RFPs |
| Extraction precision (spreadsheet) | 100.0% |  | precision on the spreadsheet RFPs |
| Extraction recall (model) | 100.0% |  | the model-based extractor: true requirements found |
| Extraction precision (model) | 100.0% |  | the model-based extractor: share of its items that are true requirements |
| Typed fields (model) | 85.7% |  | the model-based extractor: kind, response type and evidence flag as labelled |
| Typed fields | 100.0% |  | kind, response type and evidence flag as labelled |
| Duplicates grouped | 100.0% |  | a repeated question points at the first, and nothing else is grouped |
| Instructions, dates, rules | 100.0% |  | instructions, deadlines and submission rules found and typed (not drafted) |
| Deadlines | 100.0% |  | closing dates read from the RFP |
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
| Claim support | 86.4% |  | the writer's own claims that the verifier found supported by their evidence |
| Confidence separation | 0.0% |  | edit rate of the Low band minus the High band's, on answers that are answers (positive = the band ranks answers correctly) |
| Injected set: High band | 16 |  | answers made of one checked claim that got the High band |
| Injected set: Low band | 16 |  | the same for the Low band |
| High band: wrong | 0.0% |  | share of High-band injected answers that are deliberately wrong (should be 0%) |
| Low band: wrong | 100.0% |  | the same for the Low band (should be high) |
| Injected confidence separation | 100.0% |  | wrong-rate of the Low band minus the High band's, on answers whose truth was decided in advance |
| High-band answers | 9 |  | answers given the High band (placeholders are not counted) |
| Medium-band answers | 2 |  | answers given the Medium band |
| Low-band answers | 7 |  | answers given the Low band |
| High band: needs a change | 0.0% |  | share of High-band answers a person would have to change (wrong fact, wrong compliance, answering what it should not) |
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
| council-docx | harbourview_council_rfp.docx | 8 | 8 | 8 | - | 0 |
| retail-xlsx | northshore_retail_requirements.xlsx | 11 | 11 | 11 | - | 0 |

## Injected claims (32; 0 not handled as expected)

All handled as expected.

## Problems (3 of 25 requirements)

- **clinic-pdf / C2** The provider must enforce multi-factor authentication for administrators.: 1 claim(s) the evidence does not support: Multi-factor authentication uses Microsoft Entra ID and supports proto
- **retail-xlsx / RT-03** Provide online training for administrators and end users.: 1 claim(s) the evidence does not support: Online training sessions are conducted live.
- **retail-xlsx / RT-11** Describe the online training you provide for administrators and end users.: 1 claim(s) the evidence does not support: Online training sessions are conducted live.
