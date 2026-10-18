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
- source yield = publishable stories / ingested candidate stories;
- source reliability = successful collection runs / total collection runs;
- auto-publish precision must be estimated from reviewed samples, not assumed.

## Daily decision levels

- AUTO: reversible low-risk config tuning inside explicit bounds.
- PR_REVIEW: prompt/config/code changes.
- OWNER_APPROVAL: spend, policy, new high-risk automation, major architecture changes.
