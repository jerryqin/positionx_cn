# PositionX-CN AI Independence Contract — P2 Draft

## P1 result
- Hard runtime AI-provider dependencies: `0`.
- Possible tracked secret assignments: `0`.
- P1 conclusion: runtime operation does not require ChatGPT or another external AI provider.

## Contract
Without ChatGPT or any external AI assistant, PositionX-CN must be able to:
- start and run local operational workflows;
- update/validate data;
- run Deep/RET120/portfolio/Optimus workflows;
- reconcile broker state;
- enforce risk/governance gates;
- expose human-review queues;
- restore environment and governed data;
- diagnose common failures using local documentation and the future global doctor.

## Conversation-dependency review
- Tracked files containing conversation/ChatGPT terminology found by P2A: `4`.
- Such terminology is not itself a runtime dependency. P2B should review each occurrence and ensure no operational rule exists only in chat history.

## Allowed AI role
AI assistants may help with research, development, independent audit, or drafting, but they must not be a hidden prerequisite for core runtime, recovery, or safety decisions.
