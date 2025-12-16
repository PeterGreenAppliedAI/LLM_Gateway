AI System & Agent Design Principles
A concise reference for building secure, stable, and maintainable AI-driven systems. Designed for daily
use as a foundational guide.
1. Secure by Design
Systems and agents must be built with safety and predictability as defaults.
Core Rules
• Least Privilege: Give agents only the exact capabilities they require.
• Explicit Boundaries: Every tool has defined inputs, outputs, and side effects.
• No Implicit Trust: Validate all incoming and outgoing data.
• Auditability: Log tool usage, errors, and state changes.
• Idempotent Actions: Retrying an operation must not duplicate or corrupt data.
• Fixed Capability Set: Agents cannot create, modify, or extend their capabilities.
2. SOLID Principles (Applied to AI Systems)
Ensure components are predictable, modular, and interchangeable.
S — Single Responsibility
Each tool or module handles one task only.
O — Open/Closed
Extend functionality through new components instead of modifying existing ones.
L — Liskov Substitution
You must be able to replace implementations (LLMs, vector stores, agents, tools) without breaking
consumers.
I — Interface Segregation
Provide minimal interfaces tailored to specific tasks. Avoid large, multi-purpose surfaces.
D — Dependency Inversion
Depend on stable contracts, not concrete implementations.
3. DRY (Don't Repeat Yourself)
Avoid duplicating logic, validation, error handling, or schema processing.
Benefits
• More reliable behavior across the system
• Fewer security holes
• Easier debugging and maintenance
4. YAGNI (You Aren’t Gonna Need It)
Do not build capabilities based on speculation.
Guidelines
• Add tools only when a real use case appears.
• Avoid general-purpose super-tools.
• Keep agent capability scope small and intentional.
5. KISS / Law of Simplicity
Simple systems fail in simple, understandable ways.
Keep Systems Simple By
• Keeping data flow obvious and explicit
• Reducing branching and hidden conditions
• Avoiding nested orchestration logic
• Keeping tools small and focused
6. Replaceability & Modularity
Agents and tools must be built so they can be swapped without refactoring the entire system.
Replaceable Components
• LLM backends
• Vector storage engines
• Memory layers
• Tool implementations
• Planning modules
Guarantee this through clear contracts and consistent I/O structures.
7. Contracts Everywhere
Use structured, enforced schemas at every stage.
Contract Goals
• Define what data looks like
• Reduce improvisation by the model
• Standardize tool usage and agent behavior
• Increase predictability and safety
8. Practical Application Checklist
A quick reference before implementing any new agent feature:
• Does this tool do exactly one thing?
• Does it follow a clear input/output contract?
• Is the capability absolutely necessary (YAGNI)?
• Does it follow least privilege?
• Is the logic duplicated anywhere else (DRY)?
• Can I swap the implementation without breaking consumers?
• Does the agent remain simple, debuggable, and auditable?
• Does the system fail predictably if something goes wrong?
9. Domain-Driven Design (DDD)
Model AI systems around explicit domains, not tools or technologies.
DDD Principles for AI Systems
• Define clear bounded contexts for agents and services
• Use ubiquitous language shared between engineers and domain experts
• Separate domain logic from orchestration and infrastructure
• Expose domain behaviors through explicit contracts
• Prevent domain leakage across agent boundaries
Agents should represent domain intent, not implementation detail.
10. Core Mindset
Design AI systems like they are adversarial until proven otherwise. Keep tools small, safe, predictable,
and modular. Build systems where components can be replaced, audited, and tested independently