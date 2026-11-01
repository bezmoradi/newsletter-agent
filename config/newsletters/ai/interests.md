I build software with AI coding agents and want to learn how people actually build, operate and improve agentic software-development systems: coding agents, their harnesses, software factories and dark factories, and the tooling and practices that connect them. I read every item with one question: what can I learn from this that would help me build or use a more capable, reliable and autonomous software-development system?

An item is worth my time when it offers:

- working implementations, repositories, technical walkthroughs or reproducible examples;
- firsthand accounts of using coding agents on real projects, including large and brownfield codebases;
- architecture decisions, workflow patterns or useful implementation details;
- failures, limitations, postmortems, and the techniques that made systems more reliable;
- new capabilities or tooling that materially change what engineers can delegate;
- evidence about quality, review burden, cost per completed task, maintainability, or how often humans had to step in.

The topics I follow, with terms people use for them. The terms help recognize a topic; an item doesn't need to use them, and new terms are welcome when they name a concrete technique.

1. Agentic coding and autonomous development: coding agents, agentic engineering, autonomous, long-running, background and asynchronous coding agents, issue-to-PR agents, end-to-end software engineering agents.
2. Harness engineering and agent runtimes: the code, prompts, tools and checks around a model that make an agent reliable; scaffolding, the execution loop, orchestration, tool design, lifecycle management, checkpointing and recovery, durable execution such as Temporal, retry strategies and termination conditions.
3. Software factories and dark factories: pipelines where agents take work from specification to shipped code with little or no human in the loop; lights-out and unattended development, specification-driven development, spec-to-code-to-test pipelines, autonomous implementation and verification, agent-driven maintenance, continuous autonomous development, and how humans review, test and trust what comes out.
4. Context engineering, memory and skills: what goes into an agent's context; compaction and isolation; memory architecture and persistent memory; repository navigation and codebase indexing; progressive disclosure of tools; agent skills (SKILL.md), plugins and instruction files such as AGENTS.md and CLAUDE.md, and how they are written, packaged and shared for agents such as Claude Code.
5. Multi-agent engineering workflows: parallel coding agents, coordination, planner, executor and reviewer roles, task decomposition, handoffs, shared state, git worktrees, merge conflicts, integration testing, and when a single agent does better than several.
6. Verification, testing and quality gates: validating agent-generated code, test-driven development with agents, evaluation harnesses, regression testing, independent and adversarial review agents, acceptance criteria, and agents that manipulate tests, hack their rewards or report false success.
7. Real-world deployment and failure analysis: production case studies, postmortems, failure modes, long-running reliability, large codebases, refactoring and maintenance experience, human intervention rates, review overhead, and cost per completed task.
8. Execution environments, tools and safety: sandboxes, isolated and ephemeral environments, browser and terminal tools, MCP and tool design, language servers, permissions, secrets management, prompt injection, and audit trails of what an agent did.
9. Named tools in practice: Claude Code, OpenAI Codex, GitHub Copilot, Cursor, Gemini CLI and similar tools, when an item covers their harness, workflows, automation, headless or SDK use, hooks, skills, subagents, background agents, sandboxing, evaluation, case studies or limitations.
10. Go, when the item is about building agents or developer tools with it.

I care less about generic AI news, model benchmark announcements, funding news, AI policy, consumer chatbot features, generic productivity tips, beginner tutorials, promotional listicles, speculative commentary, and demos that reveal no implementation details, unless they change how coding agents are built or used.
