# Multi-Agent Research Loop Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Persist, activate, and validate the approved four-seat research loop for every substantive SIH26162 project query.

**Architecture:** A project-level `AGENTS.md` activates the protocol and points to three focused role contracts. The primary agent remains the Main Researcher and interface owner; isolated subagents act as Intent and Assumption Auditor, Coach and Protocol Judge, and Loop Controller and Reporter. Standard-library tests enforce the durable contract, while live calibration verifies that the agents challenge silent assumptions and unsupported work.

**Tech Stack:** Markdown role contracts, Codex collaboration agents, Python 3 standard-library `unittest`

**Spec:** `docs/superpowers/specs/2026-09-02-multi-agent-research-loop-design.md`

## Global Constraints

- The target is `SIH26162 — AI-Based Detection and Classification of Industrial Fires and Persistent Thermal Sources Using NASA FIRMS, OSM & Satellite Data`.
- Four concurrent seats include the primary agent; only three subagents may run at once.
- Only the primary agent sends interface responses; the Reporter authors an attributed report that the primary relays transparently.
- No material inferred or unknown item may be presented as fact.
- High-impact unknowns require user clarification before interpretation-sensitive or irreversible action.
- The Main Researcher cannot approve its own work.
- The Coach returns exactly one verdict: `PASS`, `REVISE`, or `BLOCKED`.
- The Reporter releases only `PASSED`, `NEEDS_USER_INPUT`, `EVIDENCE_BLOCKED`, or `AUTHORITY_BLOCKED`.
- Internal chain-of-thought is never requested or exposed; agents report conclusions, evidence, decisions, and unresolved questions.
- No GitHub push occurs without an explicit user request.

## File Structure

- `AGENTS.md`: Project entry point that activates the loop and defines orchestration rules.
- `.agents/intent-assumption-auditor.md`: Exact contract for intent extraction and the assumption ledger.
- `.agents/coach-protocol-judge.md`: Exact independent review contract and verdict rubric.
- `.agents/loop-controller-reporter.md`: Exact iteration-control and attributed-report contract.
- `tests/test_agent_entrypoint.py`: Verifies that the project entry point activates and constrains the loop.
- `tests/test_role_contracts.py`: Verifies required role outputs, states, and cross-role boundaries.

---

### Task 1: Project Protocol Entrypoint

**Files:**

- Create: `AGENTS.md`
- Create: `tests/test_agent_entrypoint.py`

**Interfaces:**

- Consumes: Approved design at `docs/superpowers/specs/2026-09-02-multi-agent-research-loop-design.md`
- Produces: Project-wide activation instructions used by the primary agent for every substantive query

- [ ] **Step 1: Write the failing entrypoint test**

Create `tests/test_agent_entrypoint.py`:

```python
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class AgentEntrypointTests(unittest.TestCase):
    def test_entrypoint_activates_approved_loop(self):
        text = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
        required = (
            "SIH26162",
            "every substantive user query",
            "Intent and Assumption Auditor",
            "Main Researcher",
            "Coach and Protocol Judge",
            "Loop Controller and Reporter",
            "docs/superpowers/specs/2026-09-02-multi-agent-research-loop-design.md",
        )
        for phrase in required:
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, text)

    def test_entrypoint_forbids_silent_assumptions_and_self_approval(self):
        text = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
        self.assertIn("No material assumption may remain silent", text)
        self.assertIn("The Main Researcher cannot approve its own work", text)
        self.assertIn("Only the primary agent sends the final response", text)

    def test_entrypoint_requires_all_role_contracts(self):
        text = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
        for role_file in (
            ".agents/intent-assumption-auditor.md",
            ".agents/coach-protocol-judge.md",
            ".agents/loop-controller-reporter.md",
        ):
            with self.subTest(role_file=role_file):
                self.assertIn(role_file, text)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the test and verify that it fails**

Run:

```powershell
python -m unittest tests.test_agent_entrypoint -v
```

Expected: `ERROR` with `FileNotFoundError` for `AGENTS.md`.

- [ ] **Step 3: Create the minimal project entrypoint**

Create `AGENTS.md` with this content:

```markdown
# SIH26162 Multi-Agent Research Protocol

Apply this protocol to every substantive user query in this project. Read and follow `docs/superpowers/specs/2026-09-02-multi-agent-research-loop-design.md` before orchestrating the roles.

Use four seats:

1. Intent and Assumption Auditor — `.agents/intent-assumption-auditor.md`
2. Main Researcher — the primary agent
3. Coach and Protocol Judge — `.agents/coach-protocol-judge.md`
4. Loop Controller and Reporter — `.agents/loop-controller-reporter.md`

Spawn or reuse the three subagents with focused context. Parallelize only independent work. Respect dependencies: intent must clear before committing to an interpretation; the Coach audits a candidate result; the Reporter integrates completed role summaries.

No material assumption may remain silent. High-impact uncertainty must be verified with the user or authoritative evidence. The Main Researcher cannot approve its own work. Only the primary agent sends the final response and must attribute the Reporter's summary honestly.

Do not claim that an agent ran when it did not. If a role is unavailable, report degraded review coverage. Do not expose internal chain-of-thought; provide conclusions, evidence, decisions, verdicts, and unresolved questions.
```

- [ ] **Step 4: Run the entrypoint test and verify that it passes**

Run:

```powershell
python -m unittest tests.test_agent_entrypoint -v
```

Expected: 3 tests pass.

- [ ] **Step 5: Commit the entrypoint**

```powershell
git add AGENTS.md tests/test_agent_entrypoint.py
git commit -m "chore: activate SIH research protocol"
```

---

### Task 2: Specialized Role Contracts

**Files:**

- Create: `.agents/intent-assumption-auditor.md`
- Create: `.agents/coach-protocol-judge.md`
- Create: `.agents/loop-controller-reporter.md`
- Create: `tests/test_role_contracts.py`

**Interfaces:**

- Consumes: Exact user query, confirmed project context, candidate research result, coach verdict, and structured role summaries as appropriate to each role
- Produces: `CLEAR` or `NEEDS_CLARIFICATION` intent audit; `PASS`, `REVISE`, or `BLOCKED` coach verdict; and one valid terminal report state

- [ ] **Step 1: Write failing contract tests**

Create `tests/test_role_contracts.py`:

```python
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
ROLES = ROOT / ".agents"


class RoleContractTests(unittest.TestCase):
    def read(self, name: str) -> str:
        return (ROLES / name).read_text(encoding="utf-8")

    def test_intent_auditor_contract(self):
        text = self.read("intent-assumption-auditor.md")
        for phrase in (
            "CLEAR",
            "NEEDS_CLARIFICATION",
            "CONFIRMED",
            "INFERRED",
            "UNKNOWN",
            "HIGH",
            "LOW",
            "Do not answer the domain question",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, text)

    def test_coach_contract(self):
        text = self.read("coach-protocol-judge.md")
        for phrase in (
            "VERDICT: PASS",
            "VERDICT: REVISE",
            "VERDICT: BLOCKED",
            "intent fidelity",
            "scientific validity",
            "instruction compliance",
            "SIH competitiveness",
            "Do not rewrite the candidate answer",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, text)

    def test_reporter_contract(self):
        text = self.read("loop-controller-reporter.md")
        for phrase in (
            "PASSED",
            "NEEDS_USER_INPUT",
            "EVIDENCE_BLOCKED",
            "AUTHORITY_BLOCKED",
            "Agent Activity",
            "Coach Verdict",
            "Assumption Ledger",
            "Review Cycles",
            "Never convert an unresolved assumption into a fact",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, text)

    def test_roles_forbid_private_reasoning_disclosure(self):
        for name in (
            "intent-assumption-auditor.md",
            "coach-protocol-judge.md",
            "loop-controller-reporter.md",
        ):
            with self.subTest(name=name):
                self.assertIn("Do not provide private chain-of-thought", self.read(name))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the tests and verify that they fail**

Run:

```powershell
python -m unittest tests.test_role_contracts -v
```

Expected: 4 tests error because the three role files do not exist.

- [ ] **Step 3: Create the Intent and Assumption Auditor contract**

Create `.agents/intent-assumption-auditor.md`:

```markdown
# Intent and Assumption Auditor

You are independent of the Main Researcher. Receive the user's exact query and the minimum confirmed project context. Do not answer the domain question and do not perform the requested implementation.

Return a concise audit with these fields:

- `STATUS`: `CLEAR` or `NEEDS_CLARIFICATION`
- `Objective`
- `Requested Deliverable`
- `Constraints`
- `Success Criteria`
- `Assumption Ledger`: each material item labelled `CONFIRMED`, `INFERRED`, or `UNKNOWN`; impact labelled `HIGH` or `LOW`; include its evidence or reason
- `Clarification Question`: exactly one question when status is `NEEDS_CLARIFICATION`, otherwise `None`

Rules:

- Never promote an `INFERRED` or `UNKNOWN` item to fact.
- Any `HIGH`-impact `UNKNOWN` requires `NEEDS_CLARIFICATION`.
- Any `HIGH`-impact `INFERRED` must be verified by the user or authoritative evidence.
- Low-impact defaults must remain explicit and reversible.
- Flag conflicts between the current query and confirmed prior instructions.
- Do not provide private chain-of-thought. Report only conclusions, classifications, evidence, and the clarification needed.
```

- [ ] **Step 4: Create the Coach and Protocol Judge contract**

Create `.agents/coach-protocol-judge.md`:

```markdown
# Coach and Protocol Judge

You independently audit a candidate result from the Main Researcher. You guide and judge; you do not take over the research or implementation. Do not rewrite the candidate answer.

Check:

1. intent fidelity
2. assumption handling
3. factual grounding and source quality
4. scientific validity and reproducibility
5. feasibility and failure modes
6. novelty versus cosmetic complexity
7. instruction compliance and authorization boundaries
8. privacy, ethics, safety, and Indian operating conditions when relevant
9. SIH competitiveness

Return:

- `VERDICT: PASS`, `VERDICT: REVISE`, or `VERDICT: BLOCKED`
- `Decisive Evidence`
- `Protocol Findings`
- `Required Corrections`: concrete and testable; `None` only for `PASS`
- `Residual Risks`

Use `REVISE` for correctable defects. Use `BLOCKED` only when missing input, evidence, authority, or external state prevents responsible completion. Never pass unsupported numerical, experimental, or comparative claims.

Do not provide private chain-of-thought. Report only the verdict, evidence, detected defects, required corrections, and residual risks.
```

- [ ] **Step 5: Create the Loop Controller and Reporter contract**

Create `.agents/loop-controller-reporter.md`:

```markdown
# Loop Controller and Reporter

You receive the exact user query, the Intent Auditor's structured output, the Main Researcher's candidate result, and the Coach's verdict. Track consistency and decide whether the workflow may terminate. You do not independently replace the domain research.

Valid overall states:

- `PASSED`
- `NEEDS_USER_INPUT`
- `EVIDENCE_BLOCKED`
- `AUTHORITY_BLOCKED`

Never convert an unresolved assumption into a fact. Reopen the relevant stage when role outputs conflict, the Coach requests revision, evidence does not support the result, or a high-impact uncertainty remains. Do not report `PASSED` unless the Coach returned `PASS` and the outputs are mutually consistent.

Return this concise report:

- `Overall Status`
- `Review Cycles`
- `Understood Intention`
- `Assumption Ledger`
- `Agent Activity`: attributed summary for the Auditor, Main Researcher, Coach, and Reporter
- `Coach Verdict`
- `Integrated Answer`: preserve the researcher's substantiated result; do not add new factual claims
- `Remaining Uncertainty`
- `Next Action`

Only the primary agent can send the interface response. Write the report for transparent relay by that agent.

Do not provide private chain-of-thought. Report only conclusions, evidence, role contributions, verdicts, unresolved issues, and the next action.
```

- [ ] **Step 6: Run the role-contract tests and verify that they pass**

Run:

```powershell
python -m unittest tests.test_role_contracts -v
```

Expected: 4 tests pass.

- [ ] **Step 7: Run the complete protocol test suite**

Run:

```powershell
python -m unittest discover -s tests -v
```

Expected: 7 tests pass.

- [ ] **Step 8: Commit the role contracts**

```powershell
git add .agents tests/test_role_contracts.py
git commit -m "feat: define independent research agents"
```

---

### Task 3: Live Role Calibration

**Files:** None

**Interfaces:**

- Consumes: Role contracts created in Task 2 and isolated calibration packets
- Produces: Three live reusable subagents whose outputs conform to the approved protocol

- [ ] **Step 1: Spawn the three role agents with isolated context**

Use `fork_turns: "none"` for each role. Include the complete corresponding role contract and tell each agent to remain within that role. Use stable task names:

- `intent_auditor`
- `protocol_coach`
- `loop_reporter`

Expected: all three agents accept their role without editing files or answering outside their contract.

- [ ] **Step 2: Calibrate the Intent Auditor against the observed failure**

Send this packet:

```text
Confirmed context: The user cloned a repository named heat-waves and supplied the complete SIH 2026 problem-statement catalogue. The user has not supplied a problem-statement ID.
Query: "Go through our problem statement deeply."
```

Expected:

- `STATUS: NEEDS_CLARIFICATION`
- The selected problem statement is `UNKNOWN` with `HIGH` impact.
- The repository name is not treated as confirmation.
- Exactly one question asks for the PS ID or permission to shortlist the catalogue.

- [ ] **Step 3: Calibrate the Coach against an unsupported claim**

Send a candidate claiming that the heatwave statement was selected because the repository is named `heat-waves`, with no user confirmation.

Expected:

- `VERDICT: REVISE`
- Required correction identifies the unsupported high-impact assumption.
- The Coach does not rewrite the answer or perform unrelated research.

- [ ] **Step 4: Calibrate the Reporter against inconsistent inputs**

Send an Intent Auditor result with `NEEDS_CLARIFICATION`, a researcher answer that assumes heatwaves, and a Coach `REVISE` verdict.

Expected:

- Overall status is `NEEDS_USER_INPUT`, not `PASSED`.
- The contradiction and required user clarification are visible.
- No new domain claim is added.

- [ ] **Step 5: Correct any failed role contract minimally**

If a calibration expectation fails, update only the responsible `.agents/*.md` contract, add a regression assertion to `tests/test_role_contracts.py`, run the failing test first, and then repeat the affected calibration. Do not weaken an expectation merely to obtain a pass.

- [ ] **Step 6: Commit calibration-driven contract corrections if needed**

When files changed:

```powershell
git add .agents tests/test_role_contracts.py
git commit -m "fix: harden agent assumption checks"
```

If no files changed, record that the live calibration passed without creating an empty commit.

---

### Task 4: End-to-End SIH26162 Smoke Test

**Files:** None

**Interfaces:**

- Consumes: Confirmed target `SIH26162`, official problem-statement text, and all three calibrated agents
- Produces: One complete visible report that validates the loop before project strategy work begins

- [ ] **Step 1: Construct the confirmed test query**

Use:

```text
Our confirmed target is SIH26162. Restate the official deliverables without proposing a solution yet.
```

- [ ] **Step 2: Run the complete live loop**

The Intent Auditor classifies `SIH26162` as `CONFIRMED/HIGH`. The Main Researcher retrieves the official text and produces only the requested restatement. The Coach reviews it. Any `REVISE` verdict triggers another research-review cycle. The Reporter processes the final structured outputs.

- [ ] **Step 3: Verify the final report manually**

The report must include:

- Valid overall state and review-cycle count
- Understood intention
- Assumption ledger
- Attributed activity for all four roles
- Coach verdict
- Restatement limited to the two official deliverables: classification/segregation and GIS storage/visualization
- Remaining uncertainty, including the absence of an official dataset link if still true
- No solution proposal, fabricated metric, or unsupported expansion

- [ ] **Step 4: Report activation to the user**

State which agents ran, whether each calibration passed, the decisive Coach verdict, and whether review coverage is complete. Do not claim permanent background execution; explain that the role agents are invoked or resumed for substantive project queries within this thread, while `AGENTS.md` preserves the protocol for future project sessions.

---

## Completion Criteria

- All seven standard-library protocol tests pass.
- All three live calibration cases produce the expected non-passing safeguards.
- The SIH26162 smoke test terminates through the Reporter gate.
- The final response visibly attributes every role and does not hide material assumptions.
- The Git working tree contains only expected, reviewed changes.
- No remote push has occurred.
