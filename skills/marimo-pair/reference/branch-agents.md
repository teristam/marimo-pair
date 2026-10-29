# Branch Agents

Use this reference when a coordinator hands branch workers to parallel agents,
for example one agent per hypothesis, or when branching is combined with
another skill that dispatches agents or writes analysis files.

## Precedence when combining skills

Inside a marimo pairing session, the branch rules win over a generic skill's
file and notebook conventions:

- **The live runtime is the source of truth.** Agents work in their worker
  through `run_command`. They do not create Jupyter notebooks, `runs/` folders
  or project-relative files unless the user or coordinator approves.
- **No new packages.** Workers share the parent's Python environment. Check
  the `environment` that `start` printed, and tell agents what they can import.
- **Results come back as reports, not files.** Each agent ends with a short,
  structured report (the template below). The coordinator writes the synthesis
  into the parent notebook through `cm`, e.g. a markdown summary cell, so the
  notebook stays the artifact. If the user asked not to change the notebook,
  report the synthesis in the reply instead and offer to add it.
- **Persistent work stays in the worker.** If an agent builds something worth
  keeping, it puts it in named cells with `--cell NAME`. The coordinator can
  read those cells' code, e.g. by asking the agent to print it, and recreate
  them in the parent through `cm` after review. Nothing merges automatically.

If another skill's instructions conflict with these, follow these and tell the
user which instructions you set aside.

## Coordinator checklist

1. Run the required parent `help(cm)` inspection.
2. Run `start --count N --names ...`, with one worker per agent and only the
   names agents need.
3. Read `environment`. If an analysis needs a missing package, ask the user
   before installing anything into the parent's environment.
4. Hand each agent its own `run_command` and the prompt below. Never hand over
   `branch.py`, the parent token or token file, or another worker's launcher.
5. Answer names-only requests with `add`.
6. Collect reports, write the synthesis into the parent (or into the reply
   when the user asked for no notebook changes), then `stop` every worker.

## Agent prompt template

Fill in the bracketed parts. Keep the "how to run code" and "rules" sections
verbatim.

````text
You are testing one hypothesis in an isolated Python kernel (a marimo branch
worker).

HYPOTHESIS: [one testable claim]

DATA: these variables are already loaded in the worker: [name: short
description of type, shape and columns].

AVAILABLE PACKAGES: [from start's environment, e.g. numpy 2.1, pandas 2.2; no
scipy or statsmodels]

HOW TO RUN CODE — your only execution tool:
  bash [RUN_COMMAND_PATH] - <<'PY'
  print(df.head())
  PY
A plain run is a scratchpad: new top-level variables are discarded afterwards.
To keep a result for later runs, put it in a named cell:
  bash [RUN_COMMAND_PATH] --cell clean_data - <<'PY'
  clean = df.dropna()
  PY
Re-running the same --cell name replaces that cell. Each variable can be
defined by only one cell. If a new --cell fails, it is removed and keeps
nothing; if a replaced one fails, re-run it with fixed code. Only printed
output comes back, so print() what you want to see.

RULES:
- Do not install packages, write project files, or use any other script, URL,
  or token.
- If you need another variable from the parent notebook, stop and ask the
  coordinator for it by name only.
- Pilot first: a targeted analysis, with no exhaustive searches.

FINAL REPLY (the coordinator sees only this), under ~350 words:
- What you tested and how
- Key numbers: effect sizes, uncertainty, cross-validated or held-out fit, p-values
- Verdict: Supported / Not supported / Inconclusive
- Caveats, including circularity or confounding you noticed
- Names of any --cell cells worth keeping
````
