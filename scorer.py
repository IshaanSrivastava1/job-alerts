"""
Agent A -- job fit scoring.

The simplest useful shape an LLM feature can take: one request in, one
structured answer out. No tools, no loop. Everything here is a plain function
so it can be tested without a network call.

The interesting parts:

  * Structured outputs. `FIT_SCHEMA` is enforced by the API, so `score_job`
    either returns a dict with exactly those keys or raises -- there is no
    "the model wrapped it in ```json" failure mode to defend against.

  * Prompt caching. `resume.md` is identical on every call, so it goes in the
    cached system block and repeat calls bill it at ~10%.

  * Fail-open. `score_job` is wrapped in @safe(None): if the API key is
    missing, expired, or rate-limited, it returns None and the alert still
    goes out unscored. Scoring is an enhancement, never a dependency.

Calibrate the prompt with:  ./venv/bin/python3 scorer.py --sample
"""

import html
import json
import os
import re
import sys

import agent_kit
from agent_kit import ask_json, safe

HERE = os.path.dirname(os.path.abspath(__file__))
RESUME_PATH = os.path.join(HERE, "resume.md")

# Job descriptions run long and are mostly boilerplate past this point.
MAX_DESCRIPTION_CHARS = 12000

FIT_SCHEMA = {
    "type": "object",
    "properties": {
        "fit_score": {
            "type": "integer",
            "description": "1 = clearly wrong role, 10 = apply today.",
        },
        "reason": {
            "type": "string",
            "description": "One sentence, under 200 characters, explaining "
                           "the score. Concrete, not generic.",
        },
        "red_flags": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Short phrases for genuine blockers only: "
                           "seniority mismatch, missing hard requirement, "
                           "no visa sponsorship, wrong domain. Empty if none.",
        },
        "seniority_match": {
            "type": "string",
            "enum": ["under", "match", "over"],
            "description": "'under' = the role wants more experience than "
                           "Ishaan has; 'over' = the role is below his level.",
        },
    },
    "required": ["fit_score", "reason", "red_flags", "seniority_match"],
    "additionalProperties": False,
}

SYSTEM_TEMPLATE = """\
You screen job postings for one specific person. Below is everything you know \
about him. Score how well each posting fits, honestly -- an inflated score \
wastes his time more than a low one does.

Scoring guide:
  9-10  Strong match on role, seniority, and location. He should apply today.
  7-8   Good match with one soft gap he could stretch across.
  5-6   Plausible but with a real gap: wrong seniority, thin domain overlap,
        or a hard requirement he only partly meets.
  3-4   Weak. Shares a job title but little else.
  1-2   Wrong role for him entirely.

Rules:
  - Judge against the "What I'm looking for" section as much as the resume.
  - "Analyst" in a title means nothing on its own. A sales analyst or a
    marketing analyst role is a poor fit even though it matched his keyword.
  - Weigh required years of experience heavily. He has roughly one year
    full-time plus a 16-month co-op.
  - A US role that explicitly refuses visa sponsorship is a hard blocker;
    say so in red_flags and cap the score at 3.
  - If the posting text is truncated or thin, score what is there and say so
    in the reason rather than guessing.
  - reason must cite something specific from the posting, not a platitude.

--- CANDIDATE PROFILE ---
{resume}
"""

USER_TEMPLATE = """\
Score this posting.

Title: {title}
Company: {company}
Location: {location}
Source: {source}

--- POSTING TEXT ---
{description}
"""

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t\r\f\v]+")
_BLANK_RE = re.compile(r"\n{3,}")


def clean_text(raw):
    """HTML -> readable plain text. Descriptions arrive as HTML from
    Greenhouse and as plain text from Lever, so this has to handle both."""
    if not raw:
        return ""
    text = _TAG_RE.sub("\n", raw)
    text = html.unescape(text)
    text = _WS_RE.sub(" ", text)
    text = "\n".join(line.strip() for line in text.split("\n"))
    return _BLANK_RE.sub("\n\n", text).strip()


def load_resume(path=RESUME_PATH):
    """Load the candidate profile.

    Prefers the RESUME_MD environment variable so the file itself can stay out
    of this public repo -- it contains visa status and a candid self-assessment
    that don't belong on GitHub. In CI it arrives as a repo secret; locally it
    falls back to the gitignored resume.md.
    """
    from_env = os.environ.get("RESUME_MD")
    if from_env and from_env.strip():
        return from_env
    with open(path, encoding="utf-8") as f:
        return f.read()


@safe(None)
def score_job(job, resume):
    """Score one posting. Returns a dict matching FIT_SCHEMA, or None if the
    API is unavailable for any reason (the caller must handle None)."""
    description = clean_text(job.get("description"))
    if len(description) < 80:
        # Nothing meaningful to judge -- don't pay for a guess.
        return None
    if len(description) > MAX_DESCRIPTION_CHARS:
        description = description[:MAX_DESCRIPTION_CHARS] + "\n[...truncated]"

    result = ask_json(
        system=SYSTEM_TEMPLATE.format(resume=resume),
        user=USER_TEMPLATE.format(
            title=job.get("title", "?"),
            company=job.get("company", "?"),
            location="; ".join(job.get("locations") or []) or "?",
            source=job.get("source", "?"),
            description=description,
        ),
        schema=FIT_SCHEMA,
        max_tokens=1024,
        effort="low",
    )
    # Clamp defensively: the schema guarantees an integer, not a sane range.
    result["fit_score"] = max(1, min(10, int(result["fit_score"])))
    return result


# --- Presentation helpers (pure -- these are what the tests cover) ----------

DEFAULT_COLOR = 0x5865F2   # Discord blurple, the current unscored colour
GREEN = 0x2ECC71
AMBER = 0xF1C40F
RED = 0xE74C3C

SENIORITY_NOTE = {
    "under": " (wants more experience)",
    "over": " (below your level)",
    "match": "",
}


def embed_color(fit):
    """Score-driven embed colour. Unscored jobs keep today's blurple."""
    if not fit:
        return DEFAULT_COLOR
    score = fit.get("fit_score", 0)
    if score >= 8:
        return GREEN
    if score >= 5:
        return AMBER
    return RED


def fit_field_value(fit):
    """The 'Fit' field body. Discord caps field values at 1024 chars."""
    if not fit:
        return None
    note = SENIORITY_NOTE.get(fit.get("seniority_match", "match"), "")
    return ("**%d/10**%s — %s" % (
        fit["fit_score"], note, fit.get("reason", "")))[:1024]


def red_flag_text(fit):
    """Red flags go in the embed description, which is currently unused."""
    if not fit:
        return None
    flags = [f for f in (fit.get("red_flags") or []) if f]
    if not flags:
        return None
    return ("⚠️ " + " · ".join(flags))[:4096]


# --- Calibration CLI --------------------------------------------------------

SAMPLE = {
    "title": "Supply Chain Analyst",
    "company": "Example Foods Inc.",
    "locations": ["Toronto, ON"],
    "source": "Greenhouse",
    "description": """
        <p>We are seeking a Supply Chain Analyst to join our Toronto office.
        You will analyze procurement spend, build reporting in Excel and SQL,
        and partner with finance and operations to identify savings.</p>
        <ul>
          <li>1-3 years of experience in supply chain, procurement, or a
              related analytical role</li>
          <li>Strong Excel; SQL required; Python a plus</li>
          <li>Experience with SAP or Ariba preferred</li>
          <li>Bachelor's degree in engineering, business, or supply chain</li>
        </ul>
    """,
}


def main(argv):
    resume = load_resume()

    if "--sample" in argv:
        job = SAMPLE
    else:
        path = next((a for a in argv[1:] if not a.startswith("-")), None)
        if not path:
            print(__doc__)
            print("usage: python3 scorer.py --sample")
            print("       python3 scorer.py <file-with-job-description.txt>")
            return 2
        with open(path, encoding="utf-8") as f:
            job = {"title": os.path.basename(path), "company": "?",
                   "locations": ["?"], "source": "file",
                   "description": f.read()}

    print("Scoring: %s @ %s" % (job["title"], job["company"]))
    fit = score_job(job, resume)
    if fit is None:
        print("\nNo score returned. Check ANTHROPIC_API_KEY, or the "
              "description was too short.")
        return 1
    print("\n" + json.dumps(fit, indent=2))
    print("\n--- as it will look in Discord ---")
    print("colour:      #%06X" % embed_color(fit))
    print("Fit field:   %s" % fit_field_value(fit))
    print("description: %s" % (red_flag_text(fit) or "(none)"))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
