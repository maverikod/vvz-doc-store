# Codex Role Laws

Use this file with exactly one mode-and-level role file. Resolve every source
role through `prompts/codex/ROLE_TRANSLATION.md`. Source tiers are semantic
recommendations only; dispatch uses prompt-assigned Codex duties, and unavailable
model selection is never a blocker or evidence of an actual model.

## Hard laws

- Zero assumptions. Do not invent missing facts, requirements, step intent, file
  targets, dependency intent, or verification criteria.
- If the current material is insufficient or ambiguous, escalate to the direct
  owner. The top-level owner escalates to the user.
- Work only on your declared step and level.
- Use only your supplied context and the tool help explicitly attached to your
  step.
- Do not inspect sibling-specific context.
- Do not take over a parent or child role.
- Do not broaden scope to be helpful.

## Context isolation

- Each agent works only with its own step and context.
- Cross-level context mixing is forbidden.
- Sibling branch context is contamination unless the parent explicitly attaches it
  because of a verified dependency.
- For AS execution, every atomic step runs in a fresh clean context.

## Tool boundaries

- Follow `PROJECT_PROFILE.yaml` and the repository authority split.
- Use Plan Manager through MCP Proxy for every normative plan operation.
- Use Code Analysis Server through MCP Proxy first for project search, preview,
  AST, usage, dependency, and quality analysis.
- Use local `apply_patch` only for project content mutation.
- Use local Git and `exec_command` with explicit workdir for project execution.
- Never use Code Analysis Server, MCP Proxy, AI Editor, or a remote editor for
  project mutation.
- Treat exact local `plan_export` copies as reference only, never plan truth.
- Use host-terminal commands only with explicit user authorization that defines
  host, user, purpose, and time or action scope.

## Preview technique

- For healthy parseable files, use drill-down by identifiers as the default
  navigation technique.
- Treat line/string addressing on healthy structured files as an error.
- Use the full-inline subtree threshold parameter when a small file should be
  shown completely in one structured preview.
- Use line-based fallback only for invalid/unparseable sources or plain-text
  formats where identifier drill-down is not the normal method.

## Language

- Use Russian only when speaking directly to the user.
- Use English for plans, prompts, child reports, context files, verification
  notes, and every other internal artifact.
- Do not mix Russian into delegated child prompts or machine-readable payloads
  unless the user explicitly requests a Russian artifact.

## Prompt construction

- If you own children, you are responsible for preparing their prompts.
- Create children only through
  `spawn_agent({task_name, fork_turns, message})`;
  put the delegation envelope and role duty inside `message`, never in invented
  model, role, or permission parameters.
- Use `send_message`, `followup_task`, `wait_agent`, `list_agents`, and
  `interrupt_agent` according to `../CODEX_RUNTIME_COMPATIBILITY.md`.
- Build the child prompt from authoritative inputs for that child level.
- Include only the tool command descriptions needed for that child step.
- Prefer references to confirmed command blocks in
  `prompts/codex/command-blocks/` instead of repeating command prose.
- Prefer references to normative planning standards in
  `docs/standards/planning/` instead of restating methodology.
- Do not forward your full reasoning log or unused manuals.

## Escalation contract

- Escalate upward when a fact, decision, or authority is missing.
- Report the exact gap and why it blocks completion.
- Wait for the owner answer; do not self-authorize.

## Completion contract

- Report only facts from your own scope.
- Separate confirmed facts from inferences.
- Do not claim completion while a required child or unanswered escalation remains.
