# Devflow product context

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

Anyone building software with AI agents who wants a reliable path from a task to a reviewed change. Some are solo builders; others maintain repositories or work in small teams. They may already use coding-agent CLIs, but should not need to know Devflow's internal terms to understand the public site. Use of the product itself requires a GitHub repository and Project queue.

## Product Purpose

Devflow turns a GitHub Project queue into a repeatable path from issue to reviewed pull request. Success means a builder can initialize a repository, run a small ticket, inspect the plan and evidence, and merge the commit that passed review and CI. The user also wants an opt-in, OpenClaw-supervised queue that can carry a trusted repository through batches of three tickets before an owner checkpoint.

## Positioning

The product combines agent judgment with a deterministic engine: OpenClaw refines and triages work; coding-agent CLIs implement and review; Devflow persists state, runs configured gates in a separate checkout, checks approvals and CI, and pins merge to the reviewed commit. Local Graphify context and optional local Langfuse traces make the work inspectable across tickets.

## Operating Context

Users work in a GitHub repository with a Project queue and issues. They install the Python CLI with `uv`, connect GitHub CLI and OpenClaw, initialize the target repository, review detected test commands, and run a small ticket or watch the queue. The public site introduces the value to anyone building with AI; the README and `docs/getting-started.md` carry detailed setup.

## Capabilities and Constraints

- Open source under MIT; early-stage version 0.1.0. Supported targets are macOS and Linux with Python 3.11+.
- Plan and merge approvals are enabled by default. An opt-in autonomous mode uses OpenClaw supervision and pauses after three merged tickets for an owner checkpoint.
- The runner executes stages sequentially. STOP, time, and cost limits apply between stages; recorded cost is not a billing guarantee.
- The reviewer prefers a different coding tool when available, without guaranteeing a different underlying model.
- Graphify updates are local; model providers still receive context used for their calls. Langfuse is optional and can run locally in Docker.
- No model subscription, API credit, daemon, or hosted Devflow service is included in the install.

## Brand Commitments

Keep the name Devflow. Use direct, specific language and show the actual workflow. Avoid invented customers, testimonials, adoption numbers, benchmarks, or enterprise claims. The user wants the site clean and concise, with a visual demonstration and purposeful animation.

## Evidence on Hand

`README.md`, `DESIGN.md` (architecture), `docs/getting-started.md`, `docs/observability.md`, the actual CLI and tests, and public merged PRs. The site may use clearly labelled illustrative ticket data. There are no verified customer stories, adoption metrics, or product photos.

## Product Principles

1. Show what the engine checks and what agents decide.
2. Keep the route from first ticket to reviewed PR easy to understand.
3. Ground public claims in shipped code and documentation.
4. Make trust boundaries and early-stage limits visible without burying the main task.
