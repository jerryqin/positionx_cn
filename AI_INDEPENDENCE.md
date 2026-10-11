# AI Independence Contract

PositionX-CN must remain operational when ChatGPT or any other external AI assistant is unavailable.

Without AI assistance the system must be able to start, run scheduled jobs, update/validate data,
run forward/research pipelines, reconcile broker state, enforce risk/governance gates, expose
human-review queues, restore governed state, and diagnose common local failures.

External AI APIs are not permitted to be hidden hard dependencies of core PositionX-CN runtime.

The P0B planned tracked baseline reported zero hard OpenAI/Anthropic runtime-term hits after removal
of the user's standalone test_openai.py.

AI may remain an optional development, research, or audit assistant.
