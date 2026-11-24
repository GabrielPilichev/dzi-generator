# Question Bank Assistant — Design

An operator tool that lets an admin ask questions about the LearnPilot question bank in plain
language ("which grade 9 topics have fewer than 5 questions?", "does question 512 have the
right answer marked?") and lets a Claude agent investigate with tools. The agent can **read**
the bank freely. It can only **propose** changes, and every proposal needs a human "yes"
first. It never writes to the database.

It follows the same rule as the Ollama pipelines: **AI stays out of the request path and
out of the database**. Proposals go to a JSONL review file, the same kind of reviewed
hand-off that `docs/question_batch_review.md` uses.

## 1. Scope

In scope (this PR):

- A **skill library**: named skills, each with instructions, tool bindings and a trust level
  for each tool.
- A **router** that picks one skill per request using structured outputs, with a confidence
  threshold. Below the threshold it asks the operator instead of guessing.
- An **agent loop** on the Anthropic Messages API (manual loop) that offers only the tools
  bound to the chosen skill, enforces trust levels, and asks for confirmation before any
  proposal is written.
- A **CLI**: `python -m src.assistant --db data/questions.db "<request>"`.
- A JSONL **audit log** of every step, and **tests** (scripted fake client, no network) that
  check decision quality: routing, trust enforcement, the grade rule and the step cap.

Out of scope: web routes, schema migrations, applying proposals to the database (a
separate, reviewed step), and any student-facing use.

Why a manual loop instead of the SDK's tool runner or Managed Agents: the confirmation
checkpoint, the per-skill tool filter and the audit log sit between "model asked for a tool"
and "tool ran". The loop is short and owning it keeps those checks explicit and testable.

## 2. Package layout

```
src/assistant/
  __init__.py
  __main__.py      # python -m src.assistant  -> cli.main()
  trust.py         # TrustLevel enum                                (Agent A)
  tools.py         # tool implementations + schemas, read-only DB   (Agent A)
  skills.py        # Skill / ToolBinding, SKILL_LIBRARY, validation (Agent A)
  router.py        # route_request() via structured outputs         (Agent B)
  agent.py         # run_agent() manual loop, confirmation, audit   (Agent B)
  cli.py           # argparse entry point                           (Agent B)
tests/
  test_assistant_tools.py    # (Agent C)
  test_assistant_skills.py   # (Agent C)
  test_assistant_agent.py    # (Agent C) scripted fake client
  assistant_fakes.py         # (Agent C) FakeClient + response builders
```

## 3. Contracts (all agents build against these exactly)

### 3.1 `trust.py`

```python
class TrustLevel(str, enum.Enum):
    READ = "read"          # runs without asking; must not change anything
    CONFIRM = "confirm"    # writes a proposal; runs only after the operator says yes
    FORBIDDEN = "forbidden"  # never offered to the model; a call is refused and logged
```

### 3.2 `tools.py`

```python
@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict          # JSON Schema, "additionalProperties": False, "required" lists every property
    handler: Callable[["ToolContext", dict], dict]   # returns a JSON-serialisable dict

@dataclass
class ToolContext:
    conn: sqlite3.Connection    # opened read-only: file:<db>?mode=ro
    proposals_path: Path        # JSONL file proposals are appended to
    now: Callable[[], str]      # ISO timestamp provider (tests inject a fixed clock)

class ToolError(Exception): ...  # bad input / not found; becomes an is_error tool_result

def open_read_only(db_path: Path) -> sqlite3.Connection   # URI mode=ro, row_factory=sqlite3.Row
TOOLS: dict[str, ToolSpec]                                # registry keyed by name
```

Tools (`strict: True` is added by the agent, so every schema must satisfy strict rules):

| name | trust (in skills) | input | output |
|---|---|---|---|
| `search_questions` | READ | `query` (str), `grade` (int 8–12 or null), `limit` (int 1–25) | `{"results": [{"id", "prompt", "question_type", "grade", "topic_slug"}]}`; text search over `questions.prompt`, case-insensitive with `str.casefold()` in Python (SQLite `LIKE` only folds ASCII, and the bank is Cyrillic); grade = `curriculum_sections.class` via `curriculum_topics.section_id` |
| `get_question` | READ | `question_id` (int) | `{"id", "prompt", "question_type", "grade", "topic_slug", "topic_title", "options": [{"letter", "text", "is_correct"}], "is_ai_generated"}`; ToolError if missing |
| `topic_coverage` | READ | `grade` (int 8–12 or null), `max_questions` (int ≥ 0) | `{"topics": [{"topic_slug", "title", "grade", "question_count"}]}` for topics with `question_count <= max_questions`, sorted ascending by count |
| `find_similar_questions` | READ | `question_id` (int), `limit` (int 1–10) | `{"similar": [{"id", "prompt", "score"}]}`; token Jaccard on lower-cased word sets of `prompt`, same grade only, score ≥ 0.5, best first |
| `flag_question` | CONFIRM | `question_id` (int), `reason` (str, 10–500 chars) | appends `{"kind": "flag", "question_id", "reason", "created_at"}` to proposals; returns `{"status": "proposed"}` |
| `propose_topic_change` | CONFIRM | `question_id` (int), `topic_slug` (str), `rationale` (str, 10–500 chars) | validates the topic exists **and** the grade rule (below); appends `{"kind": "topic_change", "question_id", "from_topic", "to_topic", "rationale", "created_at"}`; returns `{"status": "proposed"}` |

**Grade rule** (from the curriculum mapping rules): a question may move to a topic of the
**same or a higher** grade, never to a lower one. Violations raise `ToolError` with a clear
message, so the model sees why and the proposal is not written.

Tool handlers never write to the database. `open_read_only` makes that structural, not a
convention: a write raises `sqlite3.OperationalError`.

### 3.3 `skills.py`

```python
@dataclass(frozen=True)
class ToolBinding:
    tool: str            # key in tools.TOOLS
    trust: TrustLevel

@dataclass(frozen=True)
class Skill:
    name: str            # snake_case, unique
    description: str     # one line; the router sees this
    instructions: str    # appended to the system prompt when the skill runs
    bindings: tuple[ToolBinding, ...]

    def offered_tools(self) -> list[str]   # bindings with trust != FORBIDDEN, in order
    def trust_for(self, tool: str) -> TrustLevel   # FORBIDDEN if unbound

SKILL_LIBRARY: dict[str, Skill]
def validate_library(library: dict[str, Skill]) -> None   # raises ValueError on: unknown tool,
    # duplicate binding, a READ binding on a tool in WRITE_TOOLS, empty offered tools
WRITE_TOOLS: frozenset[str] = {"flag_question", "propose_topic_change"}
```

Skills in the library:

| skill | what it is for | bindings |
|---|---|---|
| `coverage_audit` | Find thin or empty topics | `topic_coverage` READ, `search_questions` READ |
| `answer_check` | Check whether a question's marked answer looks right; flag doubtful ones | `get_question` READ, `search_questions` READ, `flag_question` CONFIRM |
| `duplicate_hunt` | Find near-duplicate questions and flag them | `search_questions` READ, `get_question` READ, `find_similar_questions` READ, `flag_question` CONFIRM |
| `topic_review` | Check a question's topic and propose a better one | `get_question` READ, `search_questions` READ, `topic_coverage` READ, `propose_topic_change` CONFIRM, `flag_question` FORBIDDEN |

`validate_library(SKILL_LIBRARY)` runs at import time.

### 3.4 `router.py`

```python
@dataclass(frozen=True)
class RouteDecision:
    skill: str | None      # None => unclear, ask the operator
    confidence: float      # 0..1
    reason: str

ROUTER_THRESHOLD = 0.6
def route_request(client, request: str, library: dict[str, Skill], *, model: str) -> RouteDecision
```

One `client.beta.messages.create` call with `output_config={"format": {"type": "json_schema",
"schema": ...}}`. The schema is `{"skill": enum[skill names + "none"], "confidence": number,
"reason": string}`, all required, `additionalProperties: false`. The system prompt lists each
skill's name and description. `skill == "none"` or `confidence < ROUTER_THRESHOLD` →
`skill=None`. A refusal → `skill=None` with the refusal as the reason.

### 3.5 `agent.py`

```python
MODEL = "claude-opus-5-5"
MAX_STEPS = 12            # model calls per run; the loop stops with status "step_limit"

@dataclass
class AgentResult:
    status: str           # "done" | "needs_clarification" | "step_limit" | "refused"
    skill: str | None
    answer: str           # final text for the operator
    proposals: int        # proposals written this run
    steps: int

Confirm = Callable[[str, dict], bool]   # (tool name, input) -> operator said yes?

def run_agent(request: str, *, client, ctx: ToolContext, confirm: Confirm,
              audit_path: Path, library=SKILL_LIBRARY, model=MODEL,
              max_steps=MAX_STEPS) -> AgentResult
```

The loop:

1. `route_request`. If `skill is None`, return `needs_clarification` with the router's reason
   and the list of skills.
2. Build `tools=[{name, description, input_schema, "strict": True}]` for
   `skill.offered_tools()` only. The system prompt is the base prompt plus
   `skill.instructions`.
3. Call `client.beta.messages.create(model=..., max_tokens=16000,
   thinking={"type": "adaptive"}, output_config={"effort": "medium"},
   betas=["server-side-fallback-2026-07-01"], fallbacks="default", system=..., tools=...,
   messages=...)`.
4. `stop_reason`: `"refusal"` → status `refused`; `"end_turn"` → status `done`, answer =
   the joined text blocks; `"pause_turn"` → append `response.content` and continue;
   `"max_tokens"` → status `done` with the partial text plus a note. Otherwise, for each
   `tool_use` block (all results go back in **one** user message, in order):
   - trust `FORBIDDEN` (or unknown tool) → `is_error` result "tool not available for this
     skill", logged; the tool does not run.
   - trust `CONFIRM` → call `confirm(name, input)`. On no → result
     `{"status": "declined_by_operator"}` (not an error), logged; the tool does not run.
   - otherwise run the handler. `ToolError` → `is_error` result with its message.
   - results are `json.dumps(output, ensure_ascii=False)`.
5. Always append `response.content` unchanged as the assistant turn (it may contain
   `thinking` and `fallback` blocks, which must go back as they are).
6. Stop after `max_steps` model calls → status `step_limit`.

Audit log: one JSON line per event, appended to `audit_path`:
`{"ts", "event", ...}` with events `route`, `model_call` (`stop_reason`, `usage` input/output
tokens), `tool_call` (`tool`, `trust`, `input`), `tool_result` (`tool`, `ok`,
`declined`), `finish` (`status`, `proposals`, `steps`).

### 3.6 `cli.py`

`python -m src.assistant --db PATH [--proposals PATH] [--audit PATH] [--yes-to-nothing] REQUEST`

- Defaults: proposals `data/assistant/proposals.jsonl`, audit `data/assistant/audit.jsonl`
  (`data/assistant/` is git-ignored).
- Confirmation is an interactive `y/N` prompt showing the tool and its input as JSON.
  `--yes-to-nothing` declines every proposal (dry run).
- Builds `anthropic.Anthropic()` (credentials from the environment or `ant auth login`).
  Missing `anthropic` package → a clear message pointing at `requirements-dev.txt`.
- Prints the answer, the skill used, and how many proposals were written.

## 4. Testing (no network, no real database)

Tests use the repo's temporary-database support (`tests/support.py`) or a small fixture DB
built in the test, plus `assistant_fakes.FakeClient`, which returns a scripted list of
responses from `client.beta.messages.create` and records every request.

Decision-quality checks:

- the router maps clear requests to the expected skill and returns `None` below the threshold;
- a skill's request offers exactly its non-forbidden tools, all `strict`;
- a `CONFIRM` tool never runs when the operator declines, and the proposals file stays empty;
- a `FORBIDDEN` or unbound tool call is refused and never runs;
- the grade rule rejects moving a grade 11 question to a grade 9 topic;
- the database stays byte-for-byte unchanged after any run;
- the loop stops at `max_steps`; refusals end the run with status `refused`;
- every tool result goes back in one user message, matched by `tool_use_id`.

## 5. Dependencies

`anthropic` is added to `requirements-dev.txt` (optional tooling, like `python-docx`). The web
app does not import `src.assistant`.
