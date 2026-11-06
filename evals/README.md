# Evals

Structure only. There is no suite here that gates anything, and this file
exists so that `evals/code-review/` survives a clone at all: git does not
track empty directories.

Each `capabilities/*.yaml` points at a directory under here with its `evals:`
field. In a real platform that directory holds the cases a capability must
still pass before its model, prompt or context template is allowed to change,
and the decision worth writing down is who may approve a change that fails
one.

That decision is not demonstrated in this repo. Saying so is more useful than
a suite of two cases that pass and prove nothing.
