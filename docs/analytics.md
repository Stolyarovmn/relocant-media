# Analytics and feedback loop

## Post snapshots

Capture at approximately:

- 10 min
- 30 min
- 1 h
- 3 h
- 6 h
- 12 h
- 24 h
- 48 h
- 7 d

Store views, reactions, comments, forwards and attributable subscriber delta when available.

## Core derived metrics

- interaction rate = (reactions + comments + forwards) / views;
- correction rate = factual corrections / published posts;
- retained CPL D1/D7/D30 = acquisition spend / subscribers retained at horizon;
- source yield = deterministic anchor-source funnel `found → editorial_run → post_created → ready → public_sent`; see [SOURCE_YIELD.md](SOURCE_YIELD.md);
- source reliability = successful collection runs / total collection runs;
- auto-publish precision must be estimated from reviewed samples, not assumed.

`relocant-source-yield` keeps staging sends separate from public sends and includes zero-yield
registered sources. The metric is observational and may inform source review, but must not
automatically disable sources or weaken editorial/evidence policy.

## Daily decision levels

- AUTO: reversible low-risk config tuning inside explicit bounds.
- PR_REVIEW: prompt/config/code changes.
- OWNER_APPROVAL: spend, policy, new high-risk automation, major architecture changes.
