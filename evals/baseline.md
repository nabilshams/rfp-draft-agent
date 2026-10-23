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
| Answer correctness | 94.1% |  | answers that state the expected fact and not the stale one |
| Compliance correctness | 100.0% |  | Yes/Partial/No matches what the sources support |
| Latency per requirement (mean) | 6.17 s | | recorded from the live run |
| Latency per requirement (p95) | 10.61 s | | recorded from the live run |
| Input tokens per requirement | 2070 | | recorded from the live run |
| Output tokens per requirement | 292 | | recorded from the live run |

## Cases

| Case | File | Expected | Extracted | Matched | Missed | Extra |
|---|---|---|---|---|---|---|
| clinic-pdf | lakeside_health_rfp.pdf | 6 | 6 | 6 | - | 0 |
| council-docx | harbourview_council_rfp.docx | 8 | 9 | 8 | - | 1 |
| retail-xlsx | northshore_retail_requirements.xlsx | 10 | 10 | 10 | - | 0 |

Extracted but not in the golden list:

- council-docx: shall be no longer than 200 words

## Problems (1 of 24 requirements)

- **retail-xlsx / RT-08** The service must provide at least 99.99% availability.: contains forbidden: 99.5%
