# Manus on Context Engineering — Webinar Notes

Heavily edited notes from the LangChain × Manus webinar on context engineering.
Timestamps removed; transcription errors corrected; fillers stripped; speaker
labels dropped. Every technical lesson, example, number, and named reference
from the original is preserved.

---

## 1. Why context engineering exists

The term "context engineering" emerged around May 2025, riding the wave of
"the year of agents." Karpathy coined it on Twitter, framing it as the
delicate art and science of filling the context window with just the right
information needed for the next step. By contrast, "prompt engineering"
emerged in late 2022 with ChatGPT and was about prompting chat models.

Why agents made this a discipline of its own: an agent is an LLM bound to
some number of tools, calling them autonomously in a loop. Every tool call
produces a tool observation that's appended to the message history. Messages
grow without bound as the agent runs. Manus's own data point: typical tasks
require around 50 tool calls. Anthropic has noted that production agents can
span hundreds of turns. Chroma published a "context rot" report showing that
performance drops as context grows. So agents structurally accumulate
context (because they call tools freely), and their performance structurally
degrades when they do — that paradox is what context engineering is trying
to solve.

## 2. Don't reach for fine-tuning too early

Fine-tuning and post-training have become much more accessible (e.g. the
Tinker API from the Thinking Machines team has a nice design). But context
engineering is still the right boundary for application companies.

Manus's chief scientist spent 10+ years in NLP. His previous startup trained
its own language model from scratch for open-domain information extraction,
knowledge-graph building, and semantic search. The product's iteration speed
was completely capped by the model's iteration speed: a single training +
evaluation cycle took one to two weeks, and they were optimizing benchmarks
before reaching PMF — benchmarks that might not even matter for the product.

Lesson: instead of building specialized models too early, lean on general
models and context engineering for as long as possible.

When the product matures and open-source base models get stronger, it's
tempting to fine-tune on your own data. They tried it. It's another trap.
To make RL work well, you usually fix an action space, design a reward
around your current product behavior, and generate tons of on-policy
rollouts and feedback. But this is dangerous in early-days AI: everything
can shift overnight. The classic example for them was the launch of MCP,
which completely changed Manus's design from a compact static action space
to something infinitely extensible. Open-domain action spaces are extremely
hard to optimize. You could pour massive effort into post-training that
ensures generalization — but at that point you're rebuilding the layer the
model providers have already built. That's duplication.

Be firm about where you draw the line. Right now, context engineering is the
clearest, most practical boundary between application and model.

## 3. The five dimensions

Five themes recur across Manus, deep agents, open deep research, Claude
code, Cursor, Cognition's Devin, and Anthropic's research agents:

1. **Offload** — push context outside the message history into external
   state (file system, agent state, etc.) and reference it back in.
2. **Reduce** — summarize or compact context that's already in the message
   history.
3. **Retrieve** — pull context back in on demand (file search like glob/grep,
   or semantic search over an index).
4. **Isolate** — split context across multiple agents/sub-agents so each has
   its own window.
5. **Cache** — keep prefixes stable so the provider's KV cache can be reused.

These dimensions are not independent. Offload + retrieve enable more
efficient reduction. Stable retrieval makes isolation safe. Isolation slows
context growth and reduces the frequency of reduction. But more isolation
and more reduction hurt cache efficiency and output quality. Context
engineering is balancing conflicting objectives — it's hard.

## 4. Offloading

The central idea: not all context needs to live in the agent's message
history. Take heavy tool outputs (e.g. a web-search payload), dump them to
the file system, and send the agent only minimal information so it can
reference the full context if it needs to. The full payload doesn't get
spammed into the window in perpetuity.

This pattern is everywhere: Manus uses it. Deep agents uses the file
system. Open Deep Research uses agent state (LangGraph state) as the
file-system equivalent. Claude code uses it extensively. Long-running agents
generally do.

A key Manus observation: almost every agent action is naturally reversible
because there's a unique identifier you can use as the offload handle —
file path for file ops, URL for browser ops, query for search ops. The
identifier is "already there."

### Open Deep Research as a worked example

Open Deep Research has three phases:

- **Scoping** — produce a research brief.
- **Research** — multi-agent fan-out.
- **Writing** — final one-shot.

Offloading: the research brief is saved outside the context window
(LangGraph state, equivalent to a file). The window will get peppered with
other things during research; the brief stays accessible and gets pulled
back in on demand at the end of the message list before the writing phase.

Reduction: summarize observations from token-heavy search tool calls inside
the research phase.

Isolation: research uses sub-agents.

The repo performs on par with the best implementations and currently sits
top-10 on Deep Research Bench.

## 5. Reduction: compaction vs. summarization

Manus draws a sharp line between two reduction operations.

### Compaction (reversible)

Every tool call and tool result has two formats: a full one and a compact
one. The compact format strips any field that can be reconstructed from the
file system or external state. Example: a write-file tool has `path` and
`content` fields. After it returns, the file exists in the environment, so
the compact format keeps `path` and drops `content`. If the agent is smart
enough, when it needs to read that file again, it retrieves it via the
path. No information is lost — it's externalized.

This reversibility is crucial because agents do chained predictions on
previous actions and observations, and you cannot predict which past action
will suddenly become important 10 steps later.

### Summarization (irreversible)

Compaction only goes so far. Eventually context still grows and you hit a
ceiling. That's when summarization kicks in.

### Threshold and trigger

You have your model's hard context limit (e.g. 1M tokens), but in reality
most models start degrading much earlier — typically around 200K. You see
"context rot": repetitions, slower inference, degraded quality. Run
evaluations to identify your **pre-rot threshold** (typically 128K–200K)
and use it as the trigger for context reduction.

When the trigger fires, **start with compaction, not summarization.** And
compaction doesn't mean compressing the entire history: compact the oldest
50% of tool calls while keeping the newer ones in full detail, so the model
still has fresh few-shot examples of how to use tools properly. Otherwise
the model imitates the compact format and outputs incomplete tool calls
with missing fields — wrong.

After compaction, check how much free context you actually gained. After
multiple rounds the gain can be tiny because even compact tool calls still
use context. That's when you go to summarization.

When summarizing, **use the full version of the data, not the compact
one.** And keep the last few tool calls and tool results in full detail
(not summary) so the model knows where it left off and continues smoothly.
Otherwise after summarization the model can change its style and tone.
Keeping a few full tool-call/tool-result examples really helps.

### Prompting for summarization

Don't use a free-form prompt that asks the AI to generate a paragraph.
Define a **schema** — a form with fields the AI fills in: files modified,
user goal, where you left off, etc. Output is stable, you can iterate on
it. Schemas as contracts shows up repeatedly in Manus's design.

### What other systems do

- Claude 4.5 has built-in pruning of old tool calls / tool messages in
  recent SDK releases.
- Claude code's compaction feature triggers at a percentage of the
  overall context window.
- Cognition summarizes / compacts at agent-to-agent handoffs.
- Open Deep Research summarizes token-heavy search tool outputs inside
  research.

## 6. Retrieval: indexing vs. file-system search

There's an active debate about the right approach.

- **Cursor** uses indexing + semantic search alongside simpler file-based
  search like glob and grep (per Lee Robinson's OpenAI demo day talk).
- **Claude code** uses only the file system + simple search tools (notably
  glob and grep).
- **Manus** does not use index databases. Every Manus session's sandbox is
  fresh, and the user wants fast interaction — there's no time to build an
  index on the fly. Manus is more like Claude code: relies on grep and
  glob.

For long-term memory or enterprise knowledge bases, external vector
indexes are still appropriate — that's about the *amount* of information
you can access. Manus operates in a sandbox, so it's a different scale.

## 7. Isolation: communicating vs. sharing memory

Cognition has warned in their blog against multi-agent setups because
syncing information between multiple agents becomes a nightmare. Manus
agrees. But this is not a new problem — multi-process / multi-thread
coordination has been a classic challenge since the early days of computer
programming, and we can borrow from there.

There's a famous Go community quote:

> Do not communicate by sharing memory; instead, share memory by
> communicating.

It's not directly about agents (and is sometimes wrong for agents), but it
highlights two distinct patterns. Translate "memory" → "context" and the
parallel is clear.

### By communicating (classic sub-agent)

The main agent writes a prompt; the prompt is sent to a sub-agent; the
sub-agent's entire context consists only of that instruction. Use this when
the task has a short, clear instruction and only the final output matters
— for example, searching a codebase for a specific snippet. The main agent
doesn't care how the sub-agent finds the code; it only needs the result.
Claude code's task tool works this way.

### By sharing memory (sharing context)

The sub-agent can see the entire previous context — all tool-use history —
but has its **own** system prompt and **own** action space. Use for more
complex scenarios: deep research where the final report depends on lots of
intermediate searches and notes. Yes, you could save all those notes to
files and have the sub-agent read everything again, but that wastes
latency and possibly even more tokens than just sharing.

Trade-off: sharing context is **expensive**. Each sub-agent has a larger
input to prefill (more input tokens), and because the system prompt and
action space differ, you cannot reuse the KV cache — you pay the full
price.

### Manus's multi-agent structure

Manus is multi-agent but does **not** divide by role (designer agent,
programmer agent, manager agent, etc.). Role-based decomposition mimics
how human companies organize themselves — but that's a consequence of
limited human context, not LLM constraints. Forcing a human org chart onto
agents is anthropomorphizing them.

Manus has only a handful of agents:

- A large general executor
- A planner
- A knowledge manager (reviews user–agent conversations and decides what
  to save to long-term memory)
- A data/API registration agent

Manus is very cautious about adding more sub-agents because communication
is hard. Most "sub-agents" are implemented as **agent-as-tool**.

### Agent-to-agent communication: wide research / agentic map-reduce

Manus shipped a feature called **wide research**, internally called
"agentic map-reduce." Because each Manus session has a full virtual
machine behind it, passing information from main agent to sub-agent is
done by **sharing the same sandbox** — the file system is there, you just
pass paths.

Sending information *to* sub-agents is the easy part. The hard part is
getting correct output back. The trick: every time the main agent spawns a
new sub-agent (or 10), it must define the **output schema**. From the
sub-agent's perspective there is a special tool `submit_result`, and
**constraint decoding** ensures what the sub-agent submits matches the
main-agent-defined schema. The map-reduce result resembles a spreadsheet
constrained by that schema.

Same idea as summarization — schemas as contracts between agents and
between tools and agents.

## 8. Layered action space (the new offloading frontier)

When people say "offload" they usually mean moving working context into
external files. But as the system grows — especially when you integrate
MCP — **the tools themselves take up a lot of context**, and having too
many tools causes "context confusion": the model calls the wrong ones or
even non-existing ones. So tools also need to be offloaded.

A common approach is **dynamic RAG on tool descriptions**: load tools on
demand based on current task or status. But this causes two problems:

1. Tool definitions sit at the front of the context, so the **KV cache
   resets every time** they change.
2. The model's past calls to now-removed tools are still in the context.
   That can fool the model into calling invalid tools or using invalid
   parameters.

Manus's solution is a **layered action space** with three abstraction
levels.

### Level 1 — Function calling

Schema-safe (constraint decoding). The classic pattern. Downsides: changes
break the cache, too many tools cause confusion. Manus uses a **fixed**
small set of atomic functions: read/write files, exec shell commands,
search files / search internet, browser operations. These atomic functions
have very clear boundaries and compose into more complex workflows.

Rule of thumb on tool count: try not to include more than ~30 tools (rough
number, depends on the model). If you're building a general AI agent like
Manus, keep native functions super atomic — Manus has only 10–20 — and
push everything else to lower levels.

### Level 2 — Sandbox utilities

Each Manus session runs inside a full virtual machine sandbox on a
customized Linux. Manus uses shell commands to run pre-installed utilities
that Manus develops. Examples: format converters, speech-recognition
utilities, and a special one called **MCP CLI** — Manus does **not**
inject MCP tools into the function-calling space; it accesses them via CLI
inside the sandbox.

Utilities are great because:

- New capabilities can be added without touching the model's
  function-calling space — they're just commands pre-installed on the
  computer.
- The agent finds them the way any Linux user would: it knows there's a
  directory of utilities and can run `<command> --help` to figure out
  usage.
- Large outputs can write to files or return paginated; Linux tools like
  grep / cat / less / more let the agent process results on the fly.

Trade-off: super good for large outputs, **not** good for low-latency
back-and-forth interactions with the front end (where you want to
visualize each step of the agent for the user).

### Level 3 — Packages and APIs

Manus writes Python scripts to call pre-authorized APIs or custom
packages. Examples: a 3D-design library for modeling; a financial API for
market data. Manus pre-purchases API access and pays for these on the
user's behalf — included in the subscription. API keys are pre-installed
in the sandbox.

Best for tasks requiring lots of computation or memory but no need to push
all the data through the model's context. Example: analyzing a stock's
entire year of price data — don't feed the model all the numbers; let the
script compute and put only the summary back into context.

Code and APIs are highly composable. You can chain `get_city_names →
get_city_id → get_weather` in one Python script. Same idea as the CodeAct
paper. Downside: not schema-safe — very hard to do constraint decoding on
generated code. So pick the right scenario: anything that fits inside a
compiler/interpreter runtime, do it in code; otherwise use sandbox
utilities or function calls.

### How the LLM finds Level-2 utilities

System prompt has a hint: pre-installed command-line utilities live in a
specific folder (e.g. `/usr/sbin`). The most frequently used ones are
listed compactly in the system prompt — names only, no usage docs. The
prompt also tells the agent that `--help` is safe to use because all
utilities are developed by the Manus team with a consistent format.

### Why the layered approach preserves model ergonomics

From the model's point of view, all three levels still go through the
**standard function calls**: Level 2 is accessed via the shell function;
Level 3 is accessed by writing files and executing them via the shell
function. The interface stays simple, cache-friendly, and orthogonal
across functions. The model is using primitives it's already trained on
and familiar with — no new schemas to learn.

## 9. Hybrid code / tool execution

Manus tried using CodeAct exclusively but ran into the constraint-decoding
problem — without schema enforcement, things go wrong. Pure-code agents
generate a script for every action, run it inside a sandbox, and extract
the result.

Manus's hybrid: tool calling for some actions (~10 directly callable
tools), plus the option for the model to write a script and run it in the
sandbox. The general "write script + run" tool covers an enormous action
space without needing one tool per script.

A computer is Turing-complete, so theoretically an agent with shell + text
editor can do anything a junior intern can do at a computer — that's why
Manus calls itself a general agent.

## 10. Long-term memory across sessions

Manus has a feature called **knowledge** — explicit memory.

User says "remember X." Manus does **not** automatically insert it into
memory. It pops a dialogue: *Here's what I learned from our previous
conversation. Accept or reject?* — explicit confirmation.

Exploring more automatic ways: an interesting property of agents (vs.
chatbots) is that users correct the agent more often. A common Manus
mistake is data-visualization font issues with Chinese / Japanese / Korean
text — many users tell it to use a non-CJK font. Different users repeat
the same correction. The opportunity is to leverage **collective feedback**
— a "self-improving agent with online learning, in a parameter-free way."

## 11. File formats

Prefer **line-based** formats. They let the model use grep or read a range
of lines.

Markdown can cause trouble. Some models (no names) are trained too well on
markdown and will output excessive bullet points if the context is heavy
in markdown. Manus uses plain text more often.

## 12. Compacting search results specifically

Two cases:

- **Complex search** — multiple queries, gather important info and drop
  the rest. Use sub-agents (agent-as-tool). From the model's perspective
  it's a function (e.g. `advanced_search`), but what it triggers is
  another sub-agent — actually an agentic workflow with a fixed output
  schema. The schema-constrained result returns to the main agent.
- **Simple search** — e.g. a single Google query. Just append the full
  detail to context and rely on compaction.

In both cases, instruct the model to **write down intermediate insights
and key findings to files**, in case compaction kicks in earlier than the
model expected. Done well, compaction loses very little information,
because old tool calls are usually irrelevant after time anyway.

## 13. Planning

Early Manus used the `todo.md` paradigm — generate a to-do list, mark
tasks done. It worked, but wasted a lot of turns. Looking at logs from
March / April, roughly **one-third of actions were updates to the to-do
list**. That's a lot of tokens.

The current Manus uses a **structured planner**. There's a planner at the
bottom of the system, internally implemented as a separate planning agent
in the agent-as-tool pattern. The latest version no longer uses
`todo.md`. `todo.md` still works and gives good results, but if you want
to save tokens, use a planner agent.

A separate planning agent has a different perspective and can do external
reviews. You can also use a different model for planning — e.g. Grok can
generate interesting insights.

## 14. Model choice: no open-source models

Manus does not use open-source models. The reason is not quality — it's
**cost**.

People often think open-source models lower cost, but at Manus's scale,
for a real agent where input is much longer than output, the **KV cache**
is critical. Distributed KV cache is very hard to implement on
open-source serving stacks. Frontier providers have solid distributed
caching globally. If you do the math, with proper cache use, flagship
models can be **cheaper** than open-source alternatives.

Multi-provider strategy:

- **Anthropic** (Claude) is the best choice for agentic tasks.
- **Gemini** for multimodal.
- **OpenAI** for complex math and reasoning.

The frontier labs are diverging in directions, not converging.
Application companies have an advantage: they're not tied to one model.
Manus does **task-level** routing and even **subtask / step-level**
routing where KV-cache invalidation can be reasoned about. A lot of
internal evaluation goes into picking the right model for each subtask.

The KV-cache feature being used is provider input caching (e.g. Anthropic
input caching).

## 15. RL with verifiable rewards

Background: Manus's chief scientist did pretraining, post-training, and
RL for many years before Manus. So this is informed.

If you have sufficient resources you can try. But MCP fundamentally
changed things — you no longer have a fixed action space. Without a fixed
action space:

- Designing a good reward is very hard.
- Rollouts and feedback are unbalanced.
- To support MCP via RL, you're literally building a foundation model
  yourself — which all the model companies are already doing for you.

Don't spend much time on RL right now.

What Manus is exploring instead: personalization / online learning, in a
parameter-free way (collective feedback as above).

### Mimicking provider tool names for free RL benefit

Anthropic has done RL with verifiable rewards on a specific tool set
inside Claude code (Glob, grep, file-manipulation tools). Could you
unlock the same capability by mocking the same tool names + descriptions
in your harness?

Manus deliberately does **not** use the same names. If you design your
own function, requirements and input arguments are likely different from
the model's post-training tools. Reusing names would confuse a model
trained on different internal tools with the same name.

## 16. Architecture refactoring as models improve

Manus has refactored its architecture **five times** between launch
(March 2025) and the time of this talk (October 2025). Models are not
just improving — their **behavior** is changing.

Heuristic for designing future-proof architecture: **fix the
architecture, swap models.** If the architecture gains a lot when you
switch from a weaker to a stronger model, it's future-proof — the weaker
model tomorrow may be as good as the stronger model today.

Practical version: re-evaluate architecture every 1–2 months. Use
open-source models internally and early access to proprietary models to
prepare the next release before the next model lands.

## 17. Guardrails and safety

A sandbox connected to the internet is dangerous by default. Effort areas:

- **Outgoing-traffic checks** to ensure no tokens / secrets leave the
  sandbox if the agent is prompt-injected.
- **Redaction** when the user asks Manus to print something out of the
  sandbox.
- **Browser** is the hardest case. If the user lets Manus persist login
  state, the content of any visited page can be malicious (prompt
  injection from page content). This is somewhat out of scope for an
  application company — Manus works closely with computer-use model
  providers (Anthropic, Google) who are adding guardrails at that layer.
- **Manual confirmation** for sensitive operations (in the browser or the
  sandbox): the user must accept, otherwise take over and finish manually.

It's progressive: more user takeover today; less needed as model-level
guardrails mature.

## 18. Evals

Started with public academic benchmarks like **GAIA**. After launching to
the public, Manus found GAIA to be **misaligned** with user satisfaction
— models scoring high on GAIA were not the ones users liked.

Three eval methods now:

1. **User feedback** — every completed Manus session prompts the user for
   a 1–5 star rating. Average user rating is the gold standard.
2. **Automated tests with verifiable results** — Manus's own dataset with
   clear answers; some public academic benchmarks; **custom
   execution / transactional** test sets. Most public benchmarks are
   read-only. The sandbox makes resetting the test environment trivial,
   so Manus can run real execution tasks repeatedly.
3. **Human interns** — needed for taste-based outputs like website
   generation and data visualization, where it's very hard to design a
   reward model for "visually appealing."

## 19. Closing principle: avoid context **over**-engineering

Looking back at the six-to-seven months since Manus launched, the biggest
leaps did **not** come from adding more context-management layers or
clever retrieval hacks. They came from **simplifying** — removing
unnecessary tricks and trusting the model a little more. Every time the
architecture simplified, the system got faster, more stable, smarter.

The goal of context engineering is to make the model's job **simpler**,
not harder.

> Build less. Understand more.
