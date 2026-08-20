# ClawBytes Editorial Scope

The anchor against drift. Both curator and supervisor reference this file in every Claude call. Edit when shifting editorial direction; both immediately respect the new boundaries.

## In scope

- **Claws / OpenClaw and its derivatives** — OpenClaw, Hermes, PicoClaw, NanoClaw, IronClaw, Moltis, Codex-style agent runtimes built on or around them
- **Coding harnesses and agent CLIs, open or closed source** — Claude Code, Codex CLI, Cursor, Devin Desktop (ex-Windsurf), GitHub Copilot (agent/CLI modes), Google Antigravity, Aider, Cline, Roo Code, Goose, OpenHands, Zed (agentic editing), Amp, Devin, Warp, Replit Agent, and credible new entrants. Releases, changelog moves, pricing/limit changes that hit operators, capability shifts, and post-mortems. The harness space is wide and fast-moving — bias toward covering a real move from a smaller harness over silence.
- **AI agent runtimes, frameworks, SDKs** — Claude Agent SDK, MCP (Model Context Protocol), ACP (Agent Client Protocol), LangChain, LangGraph, AutoGen, CrewAI, MetaGPT, OpenHands, Smolagents, AGNO, Mastra, similar
- **LLM releases that materially change agent capabilities** — new Claude/GPT/Gemini/Llama/DeepSeek/Qwen/Mistral models when they meaningfully shift what agents can do (longer context, tool use quality, speed/cost, code reasoning). Not every release.
- **Security, safety, supply-chain concerns** for any of the above — CVEs, malicious packages, jailbreaks that affect agent deployments, sandbox escapes
- **Tools agent builders rely on** — vector databases (Chroma, Qdrant, Pinecone, Weaviate, Milvus), eval frameworks (Inspect AI, Phoenix, LangSmith), observability (Helicone, LangFuse), browser automation (Playwright, browser-use), code execution sandboxes (E2B, Modal, Daytona)
- **People building or shaping the above** — notable engineers, researchers, founders publishing about agent infrastructure, agent capability, or operational experience
- **Substantive research on agents, harnesses, and coding agents** (the Read lane) — papers and technical reports that advance how agents are built, evaluated, or operated: agent architectures, tool-use / function-calling methods, context engineering, coding-agent benchmarks and eval frameworks, memory/retrieval for agents, multi-agent orchestration, agent security/robustness findings. These belong in **Read**, not Watch. A paper does **not** need a shipped product to qualify — if it would change how a thoughtful operator thinks about building or running agents, it's in scope. (Still exclude generic ML/vision/image-gen papers with no agent angle.)

## Out of scope

- Generic AI/ML news that doesn't shift agent capability or deployment — most image/video gen releases, generic ML/vision papers with no agent angle, most fine-tuning announcements. (Agent/harness/coding-agent research is **in** scope for Read — see the Read bullet above.)
- Application-layer chatbot products (ChatGPT consumer features, Claude consumer features, character.ai, etc.) — unless they expose new agent-relevant primitives
- AI ethics meta-discourse not tied to a specific agent capability or deployment risk
- VC funding / acquisition news not tied to a specific Claws-ecosystem move (e.g. "X raised Series B" without context)
- Tutorials and explainers — ClawBytes covers what's *new* and what *moved*, not pedagogy
- Hot takes without substance, Twitter drama, personality conflicts

## Tone and editorial standard

- **Opinionated, declarative.** "X happened. Here's why it matters."
- **No hype amplification.** If a release is incremental, say so. "Latest official OpenClaw release; scan the changelog for operator-facing runtime changes" is honest; "BREAKING: OpenClaw drops massive update" is not.
- **Operator-centric.** What changes for someone building or running agents.
- **Brevity is a feature.** One line of context. Two if the item genuinely needs it. Almost never three.
- **Specific over generic.** "Adds streaming tool calls" beats "improves performance." "Security advisory in langflow auth flow" beats "security advisory found."
- **Source the claim.** Every item links to the primary source — release notes, repo, advisory page, blog post. Not a third-party summary.

## Examples that *would* pass the scope gate

- "Claude Agent SDK v0.3 — adds streaming tool calls, removes the `MessageStream` shim. Migration: change `with sdk.messages.stream()` to direct iteration."
- "OpenClaw 2026.5.19 — runtime now batches sub-agent calls when the parent is sleeping. ~30% latency drop for parallel workflows."
- "GHSA-87cc — auth bypass in langflow's `/login` route. Fixed in 1.2.4. Anyone running langflow exposed beyond localhost should update today."
- "Anthropic ships prompt caching for the API — 90% cost reduction on cached tokens. Material for any long-context agent."
- "Cursor 1.3 changelog — background agents can now run shell commands in CI. Closed-source, but operators live in it; worth one line."

## Examples that would *not* pass

- "OpenAI announces new ChatGPT voice mode" — application-layer, not agent infrastructure
- "Researchers publish a paper on diffusion image quality" — generic ML, no agent angle. (But a paper on agent architectures, coding-agent benchmarks, tool-use methods, or agent security IS in scope for Read.)
- "Drama on Twitter about agent benchmarks" — not signal
- "Tutorial: how to build your first MCP server" — pedagogy, not news
- "Stable Diffusion 4 released" — not agent-relevant unless it gates a new agent capability

## When to edit this file

- The channel starts feeling generic — tighten "in scope"
- You're missing a class of signal that matters — add to "in scope"
- A category is producing too much noise — move it to "out of scope" or add a quality bar
- Editorial voice has drifted — update "tone and editorial standard"

Every edit is a curator/supervisor prompt change without touching code.
