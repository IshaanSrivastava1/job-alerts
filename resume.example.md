# Candidate profile — template

The real file is `resume.md`, which is gitignored: it holds visa status and a
candid self-assessment that shouldn't be public. In GitHub Actions the same
content is supplied by the `RESUME_MD` repo secret.

To set it up:
    gh secret set RESUME_MD --repo IshaanSrivastava1/job-alerts < resume.md

Structure that works well:

## Snapshot
One paragraph: current role, background, location.

## Education / Work experience / Skills / Projects
Factual history. Quantified bullets score better than adjectives.

## What I'm looking for
The highest-leverage section — the scorer weighs it as heavily as the resume.
Cover: target roles, seniority band, locations and work authorization,
what genuinely appeals, and what a poor fit looks like.
