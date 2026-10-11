# PositionX Optimus Evidence Acquisition V0.4

Status: `RESEARCH_ONLY / FAIL_CLOSED / PROVISIONAL_FIRST`

## Purpose

Maintain the Optimus Candidate Registry without depending on a manually curated fixed list.

The acquisition layer has two jobs:

1. `Existing Candidate Surveillance`: detect new official evidence for current registry names.
2. `New Candidate Discovery`: detect new SH/SZ A-share names from official full-text disclosures.

V0.4 does **not** auto-upgrade evidence to CANONICAL. It downloads and hashes primary-source artifacts, extracts auditable context when possible, proposes structured evidence levels, and writes `PROVISIONAL` evidence into the V0.3 Registry.

## Source policy

- T1: CNINFO/Exchange official filings, Tesla official, SEC, company official IR.
- T2: exchange Q&A, earnings calls, investor-relations records.
- T3: high-quality media / supply-chain reporting.
- T4: broker research, social media, thematic lists.

Only T1/T2 are intended to support eventual Canonical evidence. T3/T4 are discovery/corroboration only.

## Evidence ladder

1. thematic/product association
2. robot customer/product commercialization
3. explicit named Tesla/Optimus relationship
4. nomination/designation
5. validation/testing
6. tooling/dedicated capacity
7. purchase order/contract
8. mass-production order / mass-production
9. recurring shipment / revenue contribution

The level produced by V0.4 is a **proposal** while review_state is PROVISIONAL.

## PIT / provenance

Every captured event preserves:

- announcement/event time
- observed_at
- source URL
- raw metadata hash
- PDF hash where available
- matched query terms
- extracted snippets where available
- stable evidence id

Raw captures are immutable; repeated identical evidence is deduplicated by stable evidence id.
