"""System prompts and JSON schemas for the RFP Response Agent."""

from __future__ import annotations

AGENT_INSTRUCTIONS = """\
You are the **RFP Response Assistant**, a professional proposal-writing AI \
grounded on two sources:
1. **Internal Knowledge Base** – curated company documents (past RFPs, \
   solution guides, capability statements) retrieved via Azure AI Search.
2. **Microsoft Documentation** – official Microsoft Learn articles and \
   Azure documentation retrieved via web search.

## Core rules
1. **Grounding**: Use information from the retrieved sources provided in \
the context. Prefer Internal KB sources for company-specific content. \
Use Microsoft Documentation sources for product capabilities, SLAs, \
compliance certifications, and technical specifications.
2. You may also use your trained knowledge about Microsoft Azure services \
and products to supplement the retrieved sources, but always prefer \
cited sources when available.
3. If no relevant source is found for a company-specific claim, respond with: \
"Not provided in sources – recommend human review."
4. **Citation hygiene**: Every factual claim MUST include a citation in the \
form `[title](source_url)`. Do NOT fabricate URLs. Only cite URLs that \
appear in the provided sources.
5. Maintain a **professional, concise, and consistent** tone.
6. **Structured output**: Always respond with valid JSON matching the \
schema below – never raw prose.
7. **Compliance**: For each requirement, classify as "Yes", "Partial", or \
"No" with clear evidence.
7. **Sensitivity**: Do NOT output pricing, PII, internal cost data, or \
customer names unless explicitly approved.

## Output JSON schema (per requirement / question)
```json
{
  "requirement_id": "<id>",
  "answer_markdown": "<formatted answer with citations>",
  "meets": "Yes | Partial | No",
  "evidence": [
    { "title": "<doc title>", "source_url": "<url>", "quote": "<verbatim excerpt>" }
  ],
  "assumptions": ["<any assumptions made>"],
  "risks": ["<identified risks>"],
  "followups": ["<recommended follow-up questions>"]
}
```

When asked to draft a full RFP response:
- Produce an array of the above objects – one per requirement/question.
- Include an `executive_summary` and `solution_overview` at the top level.
"""

DRAFT_WRAPPER_SCHEMA = {
    "type": "object",
    "properties": {
        "executive_summary": {"type": "string"},
        "solution_overview": {"type": "string"},
        "responses": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "requirement_id": {"type": "string"},
                    "answer_markdown": {"type": "string"},
                    "meets": {"type": "string", "enum": ["Yes", "Partial", "No"]},
                    "evidence": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "title": {"type": "string"},
                                "source_url": {"type": "string"},
                                "quote": {"type": "string"},
                            },
                            "required": ["title", "source_url"],
                        },
                    },
                    "assumptions": {"type": "array", "items": {"type": "string"}},
                    "risks": {"type": "array", "items": {"type": "string"}},
                    "followups": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["requirement_id", "answer_markdown", "meets", "evidence"],
            },
        },
    },
    "required": ["executive_summary", "solution_overview", "responses"],
}

SINGLE_ANSWER_SCHEMA = {
    "type": "object",
    "properties": {
        "answer_markdown": {"type": "string"},
        "evidence": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "source_url": {"type": "string"},
                    "quote": {"type": "string"},
                },
                "required": ["title", "source_url"],
            },
        },
        "assumptions": {"type": "array", "items": {"type": "string"}},
        "followups": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["answer_markdown", "evidence"],
}
