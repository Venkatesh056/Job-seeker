"""Resume archetypes.

The brief's §4 asks for a master resume plus role-specific variants. An archetype
is not a separate document — it is an *ordering and emphasis rule* applied to the
one Fact Bank. That distinction is the whole design:

  * A variant that is a separate document drifts from the master, and the drift
    is invisible until a recruiter reads two versions of the same career.
  * A variant that is an ordering over shared facts cannot contain anything the
    master does not, which makes fabrication structurally impossible rather than
    merely discouraged.

Archetype skill lists use the canonical names from `taxonomy.py`, so a resume
that wrote "sklearn" still matches the "scikit-learn" priority.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Archetype:
    id: str
    label: str
    # Titles this archetype is the right choice for, matched as substrings
    # against a normalized job title.
    title_markers: tuple[str, ...]
    # Skills to surface first, most important first. Presence in this list does
    # not create the skill — it only reorders what the Fact Bank already holds.
    priority_skills: tuple[str, ...]
    # A one-line framing for the tailored summary. The LLM may rephrase it; it
    # may not add claims to it.
    framing: str = ""
    de_emphasize: tuple[str, ...] = field(default_factory=tuple)


ARCHETYPES: tuple[Archetype, ...] = (
    Archetype(
        id="ai_engineer",
        label="AI Engineer",
        title_markers=(
            "ai engineer",
            "ai developer",
            "applied ai",
            "ai software",
            "genai engineer",
        ),
        priority_skills=(
            "pytorch",
            "transformers",
            "llm",
            "rag",
            "ai agents",
            "langchain",
            "langgraph",
            "fastapi",
            "docker",
            "pgvector",
            "faiss",
            "chromadb",
            "openai api",
            "fine-tuning",
            "python",
            "aws",
            "kubernetes",
        ),
        framing="builds and ships AI systems end to end",
        de_emphasize=("excel", "power bi", "tableau"),
    ),
    Archetype(
        id="ml_engineer",
        label="ML Engineer",
        title_markers=("ml engineer", "machine learning engineer", "mlops", "ml scientist"),
        priority_skills=(
            "pytorch",
            "tensorflow",
            "scikit-learn",
            "xgboost",
            "mlflow",
            "docker",
            "kubernetes",
            "aws",
            "airflow",
            "spark",
            "python",
            "sql",
            "onnx",
            "fastapi",
        ),
        framing="trains, evaluates and deploys machine learning models in production",
        de_emphasize=("excel", "power bi"),
    ),
    Archetype(
        id="data_scientist",
        label="Data Scientist",
        title_markers=("data scientist", "applied scientist", "research scientist", "quantitative"),
        priority_skills=(
            "python",
            "sql",
            "pandas",
            "numpy",
            "scikit-learn",
            "statistics",
            "r",
            "hypothesis testing",
            "a/b testing",
            "matplotlib",
            "seaborn",
            "pytorch",
            "feature engineering",
            "time series",
        ),
        framing="turns data into decisions with statistics and modelling",
    ),
    Archetype(
        id="data_analyst",
        label="Data Analyst",
        title_markers=("data analyst", "business analyst", "bi analyst", "analytics", "reporting"),
        priority_skills=(
            "sql",
            "excel",
            "power bi",
            "tableau",
            "pandas",
            "dax",
            "power query",
            "data visualization",
            "statistics",
            "python",
            "looker",
            "etl",
        ),
        framing="answers business questions with SQL, dashboards and clear analysis",
        de_emphasize=("kubernetes", "docker", "langgraph", "ai agents"),
    ),
    Archetype(
        id="nlp_engineer",
        label="NLP Engineer",
        title_markers=("nlp", "natural language", "speech", "conversational ai", "asr"),
        priority_skills=(
            "nlp",
            "transformers",
            "hugging face",
            "bert",
            "whisper",
            "speechbrain",
            "spacy",
            "nltk",
            "speech recognition",
            "text-to-speech",
            "pytorch",
            "llm",
            "fine-tuning",
            "python",
        ),
        framing="works on language and speech models",
        de_emphasize=("power bi", "excel", "tableau"),
    ),
    Archetype(
        id="genai_engineer",
        label="GenAI Engineer",
        title_markers=("generative ai", "genai", "llm engineer", "prompt engineer", "agent"),
        priority_skills=(
            "llm",
            "rag",
            "langchain",
            "langgraph",
            "llamaindex",
            "ai agents",
            "prompt engineering",
            "fine-tuning",
            "openai api",
            "ollama",
            "pgvector",
            "faiss",
            "chromadb",
            "qdrant",
            "fastapi",
            "python",
        ),
        framing="builds LLM and agent applications",
        de_emphasize=("excel", "power bi", "tableau"),
    ),
    Archetype(
        id="software_engineer",
        label="Software Engineer",
        title_markers=("software engineer", "backend", "full stack", "sde", "developer"),
        priority_skills=(
            "python",
            "java",
            "javascript",
            "typescript",
            "fastapi",
            "django",
            "flask",
            "react",
            "node.js",
            "postgresql",
            "docker",
            "kubernetes",
            "aws",
            "git",
            "redis",
            "sql",
        ),
        framing="writes and maintains production software",
        de_emphasize=("power bi", "excel"),
    ),
    Archetype(
        id="custom",
        label="Job-specific",
        title_markers=(),
        priority_skills=(),
        framing="",
    ),
)

BY_ID: dict[str, Archetype] = {a.id: a for a in ARCHETYPES}
CUSTOM = BY_ID["custom"]


def for_title(title: str) -> Archetype:
    """Pick the archetype whose markers best fit a job title.

    Longest marker wins, so "machine learning engineer" is not captured by a
    shorter "engineer"-style marker in another archetype. No match returns
    `custom`, which applies no reordering beyond what the job description itself
    justifies — the honest answer when the title is unfamiliar.
    """
    normalized = " ".join((title or "").lower().split())
    best: tuple[int, Archetype] = (0, CUSTOM)
    for archetype in ARCHETYPES:
        for marker in archetype.title_markers:
            if marker in normalized and len(marker) > best[0]:
                best = (len(marker), archetype)
    return best[1]


def get(archetype_id: str | None) -> Archetype:
    return BY_ID.get((archetype_id or "").strip().lower(), CUSTOM)


def rank_skills(
    available: list[str], archetype: Archetype, job_keywords: list[str] | None = None
) -> list[str]:
    """Order the candidate's own skills for this archetype and this job.

    Priority, in order: the job asked for it → the archetype prioritizes it →
    everything else, alphabetically for stability. De-emphasized skills sink but
    are never dropped: removing a true skill to fit a template is still editing
    the truth, and the user may want it there.
    """
    wanted = {k.strip().lower() for k in (job_keywords or []) if k.strip()}
    priority = {skill: i for i, skill in enumerate(archetype.priority_skills)}
    sunk = set(archetype.de_emphasize)

    def key(skill: str) -> tuple[int, int, str]:
        lowered = skill.lower()
        if lowered in sunk and lowered not in wanted:
            return (3, 0, lowered)
        if lowered in wanted:
            return (0, priority.get(lowered, 500), lowered)
        if lowered in priority:
            return (1, priority[lowered], lowered)
        return (2, 0, lowered)

    return sorted(dict.fromkeys(available), key=key)
