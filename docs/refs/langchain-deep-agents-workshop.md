# Deep Agents Workshop — Interrupt 2026 (LangChain)

Reorganized notes from Kevin Frank's (Deployed Engineer, LangChain) 35-minute
Deep Agents workshop. Timestamps, fillers, and speaker labels stripped; ASR
errors corrected; content regrouped by topic rather than talk order. Every
technical claim, number, tool name, and named reference is preserved.

- **Video:** https://share.descript.com/view/VXHGQrxCRzq
- **Gated page:** https://info.langchain.com/interrupt-2026/download/workshop-deep-agents
- **Companion repo:** https://github.com/langchain-samples/interrupt26-deepagents (`deep_agent.ipynb`)
- **Verbatim transcript + source video:** `~/Videos/langchain-deep-agents-workshop/`

Editorial flags are marked `[ed: ...]`. Model version numbers are transcribed
as spoken and postdate common training cutoffs — treat them as heard, not
verified.

---

## 1. Why Deep Agents exists

### 1.1 The library lineage

LangChain's three agent-building libraries are generations of one idea, not
competitors:

1. **LangChain (original)** — chaining together disparate data with LLM calls.
2. **LangGraph** — the ReAct pattern (model calling tools in a loop) was
   promising, but models weren't yet good enough to follow instructions and
   pick the right tools. LangGraph constrained where the LLM could go: less
   free rein, only permitted paths. Effectively custom workflow building.
3. **LangChain v1 `create_agent`** — models improved, so the constraints could
   relax. Just a model node and a tool node; the LLM decides whether to call a
   tool or return to the user. This was the goal all along.
4. **Deep Agents** — some tools and some context-engineering steps should
   *always* be in the loop. That standing set is what the library packages.

Positioned on the build/test/deploy/monitor lifecycle, this workshop is
entirely about **build**.

### 1.2 The harness

Around July of last year things started working: Claude Code was getting
traction, and people began asking what *non-coding* work it was being used
for. Manus made the same point as a general agent. That prompted a library
following similar principles — usable for coding and non-coding agents alike.

The enabling trend: as LLMs improved they got better at **long-running
tasks**. A chart of task duration against model release date shows Deep Agents
arriving exactly when models became trustworthy for complicated, multi-step
work. Today the harness field is crowded — OpenClaw had its moment, and people
run some mix of Claude Code, Codex, the deepagents CLI, Windsurf, or Manus.

LangChain's open-source team's definition, which the talk leans on throughout:

> **agent = model + harness**

The harness supplies built-in tools and built-in context engineering — the
things you should not be re-implementing in every agent you build, because
they're substantially the same every time.

### 1.3 How the three stack

```
Deep Agents        planning tool, file system tools, subagents,
                   context-engineering middleware  ("batteries included")
      ↓ built on
LangChain          create_agent = model + tools + surrounding context
                   (system prompt, skills, middleware)
      ↓ built on
LangGraph          file system backends, human-in-the-loop, streaming,
                   persistence (resume where you left off after a failure)
```

Deep Agents is the opinionated assembly of the layers below it — a claim the
talk later verifies by opening the source (§6.3).

---

## 2. Desired agent behavior → what the harness adds

The design is derived backwards from behaviors agents need:

| Desired behavior | What the harness provides |
|---|---|
| Work with real data, durably | Virtual file system support |
| Write and execute code | `execute` tool + sandboxes |
| Do it safely | Sandboxes, file system permissions, middleware hooks, guardrails |
| Access and remember knowledge | Memory, skills, MCP connections |
| Complete long-horizon work | Built-in context management (eviction, compaction) |
| Get the most out of each model | Harness profiles |

The recurring justification for the file-system emphasis: **LLMs are trained
and RL'd on enormous volumes of code**, which makes them inherently good at
navigating file systems and inclined to write code. Give them a file system
and an execution surface and you're working with the grain of the training
distribution rather than against it.

**Context management**, concretely, means two things:

- **Eviction** — a large tool result gets written to the file system instead
  of into the context window; the agent then uses `glob`/`grep` to retrieve
  only the part it needs.
- **Compaction/summarization** — triggered at a threshold percentage of the
  model's context window. Familiar from coding agents.

---

## 3. Middleware

Middleware are hooks for customizing behavior inside the agent loop. An agent
is "a model running in a loop with tools," but useful agents need more than
that. Available hook points:

- **Before the agent runs** — guardrail on incoming user input
- **Before the model call** — inspect/modify what's being sent
- **After the model returns** — inspect/modify what came back
- **On tool call output** — inspect it, or evict it before it reaches context

This is the same pre/post-hook concept found in Claude Code and similar
coding agents. Deep Agents ships a default middleware stack automatically via
`create_deep_agent`; you can add your own on top.

---

## 4. Harness profiles

**The claim: one harness cannot fit every model, and harness choices move
benchmarks meaningfully.**

Two supporting data points from the talk:

- The Deep Agents harness running Opus 4.6 **beats Claude Code** — the
  state of the art — on **Terminal Bench 2**.
- From a recent LangChain blog post, on a sample of Terminal Bench 2:

  | Harness | GPT-5.3 Codex | Claude Opus 4.7 |
  |---|---|---|
  | Base Deep Agents harness | 33% | 43% |
  | With a custom profile | ~53% | ~53% |

What a custom profile actually does: OpenAI trains its models on particular
native tool call conventions (file reading, for instance); Anthropic uses
different naming conventions. A profile matches the harness's tool surface to
whatever the model was trained or RL'd on.

The strategic reason this is a first-class feature: LangChain is open source
and model-agnostic, and **builders shouldn't have to fork Deep Agents** just
because they want a particular model. The goal is a best-in-class harness for
each supported model, not one harness that's best for a single vendor.

---

## 5. Deep Agents in production

- **OpenSWE** — LangChain's own coding agent, used internally to create first
  drafts of PRs. The talk shows a Slack thread where colleagues discuss product
  feedback, tag the deep agent, and get a PR out of it.
- **NVIDIA partnership** — a blueprint for building deep research agents on
  Deep Agents that reached **#1 on the Deep Research Bench leaderboard**, at
  lower cost by using open models.

The NVIDIA result illustrates the broader shift the talk expects: **a frontier
model as the main agent, cheaper open-source models as subagents.** That mix
is why model agnosticism matters.

---

## 6. The build: a research agent

The use case is deliberately simple. The point is to expose Deep Agents'
components one at a time.

### 6.1 Setup

- Main agent: **GPT-5.4**
- Subagent: **GPT-5.4-mini**
- Both configurable in `models.py`

### 6.2 What `create_deep_agent` gives you out of the box

- **File system tools** — `ls`, `read_file`, `write_file`, `edit_file`,
  `glob`, `grep`. Deliberately close to coding-agent conventions, with minor
  naming differences.
- **Planning tool** — writes to-dos for task tracking; states are pending /
  in progress / finished.
- **Subagent delegation** — a `task` tool. Multiple subagents can be kicked
  off simultaneously.
- **Context management** — eviction of large tool results to the file system
  (retrieved later via `glob`/`grep`), plus compaction/summarization at a
  context-window capacity threshold.
  `[ed: the eviction threshold is spoken as "greater than twenty tokens";
  given the surrounding argument this is almost certainly 20k, but the
  transcript does not say so — verify against the notebook.]`
- **A default middleware stack**, plus the ability to pass your own.

An out-of-the-box **system prompt** is included too, so a first agent needs no
assistant prompt of its own.

### 6.3 Under the hood

Opening `create_deep_agent` shows it returns a LangChain `create_agent`,
supplied with a model, a predefined system prompt, predefined tools, and
predefined middleware. Opening `create_agent` in turn shows **just two nodes:
a model node and a tool node.** For anyone used to building LangGraph graphs
with many nodes and conditional edges, that collapse is the point — the agent
decides whether to call a tool or return to the user, and that simple
architecture is what won out. Deep Agents follows the same principle.

### 6.4 Demo — file system

With no custom tools, the agent is asked to write a file containing "hello
from Deep Agents" and read it back. Result: `notes.md` created and read back,
with the file present in the virtual file system. **Takeaway: file system
capability is in the harness, not something you build.**

### 6.5 Demo — a custom tool

Adding a **Tavily search** tool (a research agent needs the internet) and
asking "in two sentences, what is LangGraph?", constrained to one search. The
answer comes back correct. In **LangSmith**, the trace confirms the Tavily
tool was called with a LangGraph overview query — and, importantly, that the
one-search instruction was honored. The pattern the talk keeps modeling:
*don't trust the output, read the trace.*

---

## 7. Backends

Files matter because LLMs manage context well with a file system. Deep Agents
offers several, which plug and play:

| Backend | Visibility scope | Typical use |
|---|---|---|
| **State** | One thread only | Scratch pad, large tool output eviction |
| **Filesystem** | Anything with access to that directory | Working with real files, like a coding agent |
| **Store** | Any thread in the same namespace | Memories across conversations and users |
| **Composite** | Mix and match | Store for `/memories` + state for eviction |

**State backend demo.** Thread 1 writes `research_notes.md`. A second thread
sees none of those files — the state backend is scoped to the thread.

**Filesystem backend demo.** Used with `virtual mode = true` so the agent
edits a virtual overlay rather than the real file system. Writing
`notes.txt` containing "Hello from file system backend" succeeds.
`[ed: the talk then opens the file and says it does in fact exist on disk,
which sits awkwardly with the stated purpose of virtual mode — worth
checking the notebook's actual semantics.]`

The composite backend is called out as the common production shape: memories
in the store backend, ephemeral eviction traffic in the state backend.

---

## 8. Subagents

Subagents exist for **context isolation**. The main agent delegates; the
subagent may run a smaller, cheaper model; it keeps all its working context
internally and returns a clean summary or report. The main agent then merges
several such reports.

The mechanism matters: the main agent calls subagents through the `task` tool
and **sees only the result — none of the subagent's tool calls.** That's what
keeps the main context window clean.

**Demo.** A research subagent on GPT-5.4-mini with a short system prompt, asked
to research what LangGraph and LangChain are and give one paragraph on each —
phrased that way specifically to see whether the main agent spawns two
subagents. The **LangSmith waterfall view** confirms it: two research agents
running concurrently, one per topic.

---

## 9. Human-in-the-loop

Once an agent can touch a file system, some tools need approval. HITL comes
from LangGraph and is configured with **`interrupt_on`**, listing the risky
tools — write, edit, delete.

**Demo.** Writing a file triggers the interrupt. The prompt surfaces the tool
name (`write_file`) and the proposed file contents, with three choices:
**approve, edit, or reject.** On approval the run resumes and `test.md` appears
in state — it exists precisely because it was approved. In a real UI this
would be considerably cleaner than a notebook prompt.

---

## 10. Long-term memory

Memory is the composite backend in practice: a **state backend** for ephemeral
per-thread data and a **store backend** mounted at **`/memories`** for anything
that should survive across conversations.

**Demo.** Thread 1 saves a memories file. Checks: the state backend is empty in
a different thread; the store backend contains the item and its contents. A
brand-new thread then reads `/memories/findings.md` and returns the same
content — same file, different conversation.

The analogy drawn: this is how coding agents let you resume in a new session
and still know your preferences — no emojis, PRs in a particular format.

---

## 11. AGENTS.md and skills

The talk frames this as where instruction-giving is heading: away from
hand-tuned system prompts and prompt engineering, toward **standardized
`AGENTS.md` and skill files**.

| | Loading | Editable | Purpose |
|---|---|---|---|
| **AGENTS.md** | Always, like a system prompt | By the agent, unless permissions prevent it | Standing instructions |
| **Skills** | On demand, only when needed | — | Task-specific templates |

The distinction is a context-budget argument. A skill should **not** sit in the
context window at all times — the agent should pull it in only when the task
calls for it.

**Demo.** A simple `AGENTS.md` plus a **LinkedIn skill**. Prompt: "Briefly
research what LangChain is, then write a short LinkedIn post about them." The
LangSmith trace shows three distinct layers:

1. The built-in system prompt ("you're a deep agent") — how to use skills, how
   to use file system tools for large tool results, how to use subagents.
2. `AGENTS.md`, loaded into context and always present.
3. The skills file — **read by the agent's own decision**, mid-run, because it
   determined it needed it.

Why this is the right design: not every research run ends up on LinkedIn.
Another might go to LangChain's blog, or out as an email. Each destination is
a skill, and only the relevant one should ever occupy context.

---

## 12. Wrap-up

The notebook's arc, in order: basic `create_deep_agent` → file system →
custom tools → backends → context isolation with subagents → human oversight →
long-term memory at `/memories` → `AGENTS.md` and skills.

**Where this is going:** standardization on `AGENTS.md` and skill files, and
**self-improving agents** — which require somewhere durable for the agent to
record what makes a use case or a user specific.

**The headline recommendation:** most agents should use this architecture, so
Deep Agents should be the *first* thing you reach for when building an agent.

### Things to try next

- **`langgraph dev`** — run the agent server and drive it from **LangSmith
  Studio**. Instead of running in a notebook and switching to LangSmith to
  confirm tool calls and skill reads, you get both in one view.
- **Write more skills** — an email skill, a LangChain-blog skill — then confirm
  the agent pulls the right one rather than the LinkedIn skill.
- **Namespace the store backend by user ID**, so memories are per-user instead
  of shared across the whole agent.
- **Try specialized subagents**, and use a cheaper open model for them to cut
  token spend.

### Resources

- Deep Agents documentation and repo
- LangChain Academy
- LangChain's own guide to when to use LangChain vs. LangGraph vs. Deep Agents

### The two things to remember

1. Deep Agents should be the first place you try to solve an agentic use case.
2. Deep Agents is built on LangChain's `create_agent`, which is built on
   LangGraph — a model node and a tool node.
