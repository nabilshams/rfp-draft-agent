# Evaluation report: baseline (replay)

22 golden requirements; 23 extracted.

| Metric | Value | Baseline | Meaning |
|---|---|---|---|
| Extraction recall | 100.0% |  | share of the true requirements that were found |
| Extraction precision | 95.7% |  | share of extracted items that are true requirements |
| Retrieval hit rate | 100.0% |  | the expected source was among the retrieved documents |
| Retrieval MRR | 100.0% |  | how high the expected source ranked (1.0 = first) |
| Answer rate | 100.0% |  | requirements for which the model produced a usable answer |
| Citation support | 100.0% |  | citations whose quote is really in the cited document |
| Unsupported figures (model) | 0.0% |  | answers where the model wrote a figure not in its sources |
| Unsupported figures (delivered) | 0.0% |  | the same, after the app removed them (should be 0) |
| Abstention correctness | 86.4% |  | declined when the knowledge base has nothing, answered when it has |
| False answers | 60.0% |  | unsupported requirements that were answered anyway |
| Answer correctness | 100.0% |  | answers that state the expected fact and not the stale one |
| Compliance correctness | 100.0% |  | Yes/Partial/No matches what the sources support |
| Latency per requirement (mean) | 10.03 s | | recorded from the live run |
| Latency per requirement (p95) | 13.09 s | | recorded from the live run |
| Input tokens per requirement | 1621 | | recorded from the live run |
| Output tokens per requirement | 366 | | recorded from the live run |

## Cases

| Case | File | Expected | Extracted | Matched | Missed | Extra |
|---|---|---|---|---|---|---|
| clinic-pdf | lakeside_health_rfp.pdf | 6 | 6 | 6 | - | 0 |
| council-docx | harbourview_council_rfp.docx | 8 | 9 | 8 | - | 1 |
| retail-xlsx | northshore_retail_requirements.xlsx | 8 | 8 | 8 | - | 0 |

Extracted but not in the golden list:

- council-docx: shall be no longer than 200 words

## Problems (3 of 22 requirements)

- **council-docx / SV-4** The vendor must supply three customer references, including contact details.: answered an unsupported requirement
- **retail-xlsx / RT-06** Does your solution include an on-premises hardware appliance?: answered an unsupported requirement
- **retail-xlsx / RT-07** Provide measurable business outcomes you achieved for government clients.: answered an unsupported requirement
