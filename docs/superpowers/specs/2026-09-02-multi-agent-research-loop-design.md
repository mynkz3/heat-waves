# Multi-Agent Research Loop Design

**Date:** 2026-09-02

**Project:** Smart India Hackathon 2026 — Industrial Thermal-Source Intelligence

**Target problem statement:** SIH26162 — AI-Based Detection and Classification of Industrial Fires and Persistent Thermal Sources Using NASA FIRMS, OSM & Satellite Data

## Purpose

Create a repeatable, visible research workflow for every substantive user query. The workflow must reduce silent assumptions, separate research from independent review, iterate when quality checks fail, and report what each role contributed.

The objective is competitive excellence through scientific validity, strong evidence, explicit uncertainty, faithful execution, and a demonstrable solution. The workflow must not rely on exaggeration, invented evidence, plagiarism, or rule-gaming.

## Platform Constraint

The environment supports four agents concurrently, including the primary agent. Five requested functions therefore cannot run as five simultaneous agents. Loop control and reporting are combined into one independent role so that no requested function is removed.

Only the primary agent can send the final interface response to the user. The Reporter authors an attributed report, and the primary agent relays it transparently.

## Roles and Authority

### 1. Intent and Assumption Auditor

Responsibilities:

- Convert the user's message into an explicit objective, constraints, requested deliverables, and success criteria.
- Maintain an assumption ledger using `CONFIRMED`, `INFERRED`, and `UNKNOWN` labels.
- Identify ambiguities that could materially change the result.
- Formulate concise clarification questions when required.

Authority:

- Stop state-changing or interpretation-sensitive work when a high-impact ambiguity is unresolved.
- Permit reversible research on unaffected parts of the query while clarification is pending.

### 2. Main Researcher

The primary agent acts as the main researcher.

Responsibilities:

- Perform scientific research, technical reasoning, experiments, implementation, and verification.
- Prefer official sources and primary research.
- Distinguish facts, hypotheses, recommendations, and unverified ideas.
- Record the source or verification method for consequential claims.
- Never invent datasets, citations, metrics, experimental results, or execution outcomes.

Authority:

- Produce and revise candidate solutions.
- Cannot approve its own output.

### 3. Coach and Protocol Judge

Responsibilities:

- Review the researcher's proposed answer or action independently.
- Test intent fidelity, assumption handling, scientific validity, evidence quality, feasibility, novelty, reproducibility, instruction compliance, ethics, and SIH competitiveness.
- Detect overclaiming, unsupported conclusions, missing failure modes, and protocol violations.

Authority:

- Return exactly one verdict: `PASS`, `REVISE`, or `BLOCKED`.
- Attach concrete reasons and required corrections to non-passing verdicts.
- Reject the researcher's work; the researcher cannot override the verdict silently.

### 4. Loop Controller and Reporter

Responsibilities:

- Track the query, role outputs, audit verdicts, revisions, and unresolved issues.
- Compare the intent audit, research result, and coach verdict for contradictions.
- Reopen the loop when material inconsistencies or unsupported claims remain.
- Produce an attributed summary of every role's work and an overall assessment.

Authority:

- Release a report only when the stopping conditions are satisfied.
- Request another cycle or declare that user clarification or unavailable evidence blocks completion.

## Assumption Protocol

Every material assumption must be visible in an assumption ledger:

- `CONFIRMED`: explicitly supplied by the user or verified with authoritative evidence.
- `INFERRED`: supported by context but not directly confirmed.
- `UNKNOWN`: information is missing or conflicting.

Each entry also records its impact (`LOW` or `HIGH`) and the evidence or reason behind the label.

Rules:

1. No material inferred or unknown item may be presented as fact.
2. A high-impact `UNKNOWN` requires user clarification before interpretation-sensitive or irreversible action.
3. A high-impact `INFERRED` must be confirmed by the user or independently verified.
4. Low-impact assumptions may be used only when explicitly disclosed and safely reversible.
5. Uncertainty must be preserved in the final answer rather than hidden through confident wording.

The system cannot guarantee that every assumption is correct. It guarantees that material assumptions are surfaced, classified, challenged, and not silently promoted to facts.

## Query Lifecycle

Every user question or instruction that requests reasoning, research, judgment, planning, or action activates the loop. Pure acknowledgements do not activate a full cycle unless they also authorize or request work.

### Stage 1: Intake and Intent Gate

The Intent and Assumption Auditor receives the exact query plus only the established project context needed to interpret it. It returns:

- Objective
- Requested deliverable
- Constraints
- Success criteria
- Assumption ledger
- Clarification requirement

If a high-impact ambiguity exists, the primary agent relays the auditor's question to the user. Safe, reversible research may continue on unambiguous parts.

### Stage 2: Research and Construction

The Main Researcher independently builds the candidate result using the approved intent contract. Work should be parallelized only where subtasks are genuinely independent and do not share mutable state.

Evidence priority:

1. Official problem-statement owner and government sources
2. Standards, authoritative technical documentation, and primary research
3. Reputable datasets and reproducible experiments
4. Secondary sources for context only

Time-sensitive claims must be rechecked. Numerical claims about the project must come from executed analysis or be clearly labelled as external results.

### Stage 3: Coach Gate

The Coach reviews the candidate result against this checklist:

1. Does it answer the user's confirmed intention?
2. Are all material assumptions classified and handled correctly?
3. Are consequential factual claims grounded?
4. Is the scientific or technical reasoning valid and reproducible?
5. Were user, repository, safety, and tool protocols followed?
6. Is the proposal feasible within SIH constraints?
7. Is the novelty real rather than cosmetic?
8. Are limitations, failure modes, Indian operating conditions, privacy, and ethics addressed where relevant?
9. Does the result materially strengthen the team's competitive position?

Verdicts:

- `PASS`: no material correction is required.
- `REVISE`: specific correctable issues remain.
- `BLOCKED`: missing user input, evidence, authority, or external state prevents responsible completion.

### Stage 4: Revision Loop

A `REVISE` verdict returns concrete corrections to the Main Researcher. The revised result goes through the Coach Gate again. The loop has no artificial one-pass acceptance.

If the same unresolved condition survives repeated cycles because additional reasoning cannot resolve it, the Loop Controller stops recycling the answer and asks the user for the missing decision or reports the evidence gap.

### Stage 5: Reporter Gate

After a coach `PASS`, the Reporter checks consistency across all role outputs. A contradiction, unsupported claim, instruction mismatch, or unresolved high-impact assumption reopens the appropriate stage.

The workflow terminates only in one of these states:

- `PASSED`: intent, evidence, protocol, and consistency checks pass.
- `NEEDS_USER_INPUT`: a material choice or clarification is required.
- `EVIDENCE_BLOCKED`: the required authoritative evidence or data is unavailable.
- `AUTHORITY_BLOCKED`: completing the request needs permission outside the user's current authorization.

## Per-Query Report

The final response should remain readable while exposing accountability. It includes:

1. **Overall status:** terminal state and number of review cycles.
2. **Understood intention:** concise objective and requested outcome.
3. **Assumption ledger:** material confirmed, inferred, and unknown items.
4. **Agent activity:** brief attributed summary of each role's work.
5. **Coach verdict:** verdict, decisive checks, and revisions completed.
6. **Integrated answer:** the result the user actually requested.
7. **Remaining uncertainty and next action:** only when relevant.

Internal chain-of-thought is not exposed. Reports contain conclusions, evidence, decisions, unresolved questions, and audit outcomes.

## Parallelism and Coordination

- The Intent Auditor and preliminary research may work concurrently, but the researcher must not commit to an interpretation until the intent gate clears.
- The Coach may identify query-specific risks in parallel, then receives the candidate result for the formal verdict.
- The Reporter receives structured summaries after the dependent stages finish.
- Role agents are reused through focused follow-up tasks rather than recreated for every revision.
- The primary agent remains responsible for tool use, integration, user communication, and preservation of project state.

## Failure Handling

- Conflicting agents: the Reporter identifies the precise disagreement; authoritative evidence or user intent resolves it.
- Weak evidence: downgrade the claim, seek better evidence, or report the gap.
- Failed experiment: report the actual failure and revise; never substitute expected output.
- Ambiguous instruction: ask one focused question that unlocks the largest amount of work.
- Reviewer deadlock: surface the contested decision and evidence to the user rather than looping indefinitely.
- Agent unavailable: the primary agent reports degraded review coverage and does not pretend the missing audit occurred.

## Persistence

This document is the operating specification for the SIH project. Role prompts must implement it without expanding authority or changing user-approved requirements. Any material alteration to roles, gates, stopping conditions, or reporting requires user approval.
