"""Dynamic LangGraph-based orchestration layer on top of the existing
CrewAI dual-agent extraction/audit pipeline (app.agents.crew).

Why LangGraph on top of CrewAI rather than replacing it: CrewAI's `Agent`/
`Task` primitives (system prompt, tools, LLM binding) are still the right
abstraction for defining WHAT an individual agent does and HOW it's
allowed to reason (see app.agents.crew's module docstring on the
extraction<->audit revision loop's accountability design) -- that logic
is unchanged here. What CrewAI's `Crew(process=Process.sequential)` does
NOT give you is DYNAMIC branching (this clause's own content deciding
which specialist agent runs next) or an explicit, inspectable state
machine with per-node checkpoints -- that is what LangGraph's
`StateGraph` adds, orchestrating the SAME CrewAI agents as its nodes.

Module map:
  state.py              AgentGraphState (the graph's shared state schema)
                         and ComplexityFlags.
  complexity_router.py  Pure regex-based clause-complexity detection
                         (Requirement 1) -- no LLM call, testable in
                         isolation, feeds the graph's conditional edges.
  state_store.py         Redis-backed persistence of every node's
                         execution -- state, token consumption, confidence
                         score (Requirement 2).
  nodes.py                The graph's node functions, each wrapping one
                         CrewAI agent invocation (or the confidence-gate/
                         fallback logic) and recording its own execution
                         via state_store.
  graph.py                Builds and compiles the StateGraph, and exposes
                         `run_graph_pipeline` as the async entrypoint
                         app.agents.pipeline dispatches to when
                         settings.agent_graph_orchestration_enabled is True.
"""
