"""Skill taxonomy for evidence-based extraction.

Deliberately a curated list rather than an LLM call. Extraction runs over the
candidate's own resume, and the cost of a hallucinated skill here is a
fabricated claim on a real application — the exact failure the Fact Bank exists
to prevent. A fixed vocabulary can only ever find what is actually written.

Aliases matter more than the canonical names: a resume says "sklearn", "scikit",
"Scikit-Learn" and "SKLearn", and all four must resolve to one fact so that
matching, deduplication and gap analysis agree with each other.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from .taxonomy_domains import DOMAIN_AMBIGUOUS, DOMAIN_SKILLS

# canonical -> aliases (the canonical form is always matched too)
SKILLS: dict[str, list[str]] = {
    # Languages
    "python": ["py", "python3"],
    "sql": ["t-sql", "pl/sql", "plsql"],
    "java": [],
    "javascript": ["js", "es6"],
    "typescript": ["ts"],
    "c++": ["cpp", "c plus plus"],
    "c": [],
    "c#": ["csharp", "c sharp"],
    "go": ["golang"],
    "rust": [],
    "r": [],
    "scala": [],
    "matlab": [],
    # NOT "shell": a real resume said "identify shell entities" (shell companies)
    # and that became an evidenced Unix-shell skill.
    "bash": ["shell scripting", "zsh"],
    # ML / DL frameworks
    "pytorch": ["torch", "py-torch"],
    "tensorflow": ["tf", "tensor flow"],
    "keras": [],
    "scikit-learn": ["sklearn", "scikit", "scikit learn"],
    "xgboost": ["xgb"],
    "lightgbm": ["lgbm"],
    "jax": [],
    "onnx": ["onnxruntime"],
    "hugging face": ["huggingface", "transformers library", "hf"],
    # NLP / speech
    "nlp": ["natural language processing"],
    "transformers": ["transformer models"],
    "bert": [],
    "whisper": ["openai whisper"],
    "speechbrain": [],
    "spacy": [],
    "nltk": [],
    "speech recognition": ["asr", "automatic speech recognition", "speech-to-text"],
    "text-to-speech": ["tts"],
    # GenAI / agents
    "llm": ["large language model", "large language models", "llms"],
    "rag": ["retrieval augmented generation", "retrieval-augmented generation"],
    "langchain": [],
    "langgraph": [],
    "llamaindex": ["llama index"],
    "prompt engineering": ["prompting"],
    "ai agents": ["agentic ai", "agent framework", "autonomous agents"],
    "fine-tuning": ["finetuning", "fine tuning", "lora", "qlora", "peft"],
    "openai api": ["gpt api"],
    "ollama": [],
    # Vector / search
    "pinecone": [],
    "chromadb": ["chroma"],
    "faiss": [],
    "qdrant": [],
    "weaviate": [],
    "pgvector": [],
    "elasticsearch": ["elastic search", "opensearch"],
    # CV
    "computer vision": ["cv", "image processing"],
    "opencv": ["cv2"],
    "yolo": ["yolov5", "yolov8"],
    "cnn": ["convolutional neural network"],
    # Data
    "dax": [],
    "power query": ["powerquery"],
    "data modeling": ["data modelling", "dimensional modeling"],
    "eda": ["exploratory data analysis"],
    "feature engineering": [],
    "neo4j": ["graph database", "cypher"],
    "gnn": ["graph neural network", "graph neural networks"],
    "pandas": [],
    "numpy": ["np"],
    "scipy": [],
    "matplotlib": [],
    "seaborn": [],
    "plotly": [],
    "power bi": ["powerbi"],
    "tableau": [],
    "excel": ["ms excel", "microsoft excel"],
    "spark": ["pyspark", "apache spark"],
    "hadoop": [],
    "airflow": ["apache airflow"],
    "dbt": [],
    "kafka": ["apache kafka"],
    "etl": ["elt", "data pipeline", "data pipelines"],
    "statistics": ["statistical analysis", "stats"],
    "data visualization": ["dataviz", "data viz"],
    "a/b testing": ["ab testing", "experimentation"],
    # Databases
    "postgresql": ["postgres", "psql"],
    "mysql": [],
    "mongodb": ["mongo"],
    "redis": [],
    "sqlite": [],
    "snowflake": [],
    "bigquery": ["big query"],
    "dynamodb": [],
    # Cloud / infra
    "aws": ["amazon web services", "ec2", "s3", "lambda", "sagemaker"],
    "gcp": ["google cloud", "google cloud platform", "vertex ai"],
    "azure": ["microsoft azure", "azure ml"],
    "docker": ["containerization", "containers"],
    "kubernetes": ["k8s"],
    "terraform": [],
    "ci/cd": ["cicd", "continuous integration", "github actions", "jenkins"],
    "mlops": ["ml ops"],
    "mlflow": [],
    "linux": ["unix"],
    # Web / API
    "fastapi": [],
    "flask": [],
    "django": [],
    "react": ["react.js", "reactjs"],
    "next.js": ["nextjs"],
    "node.js": ["nodejs", "node"],
    "rest api": ["rest", "restful api", "restful"],
    "graphql": [],
    "streamlit": [],
    "gradio": [],
    # Tools
    "git": ["github", "gitlab", "version control"],
    "jupyter": ["jupyter notebook", "colab", "google colab"],
    "playwright": [],
    "selenium": [],
}

# Everything that is not software — civil, mechanical, pharma, finance, design, and
# the rest — merged in from its own module. Kept separate because the two vocabularies
# are reviewed by different eyes; merged here so every consumer keeps one lookup.
# Software wins a name collision, since its entry was written against this file's
# ambiguity rules.
SKILLS.update({k: v for k, v in DOMAIN_SKILLS.items() if k not in SKILLS})

# Alias → canonical, built once.
ALIAS_TO_CANONICAL: dict[str, str] = {}
for _canonical, _aliases in SKILLS.items():
    ALIAS_TO_CANONICAL[_canonical] = _canonical
    for _alias in _aliases:
        ALIAS_TO_CANONICAL[_alias] = _canonical

# Skills whose names are short, ordinary English words, or common in proper
# nouns. These only count inside a delimited list, where a resume actually puts
# skills. Matching them in prose fills the Fact Bank with nonsense that later
# becomes a resume claim — "CIT SPARK GRANT 2025" is an award, not Apache Spark.
AMBIGUOUS = frozenset(
    {
        "r",
        "c",
        "go",
        "java",
        "excel",
        "git",
        "np",
        "ts",
        "js",
        "py",
        "cv",
        "spark",
        "dax",
        "eda",
        "react",
        "flask",
        "scala",
        "rust",
        "keras",
    }
    | DOMAIN_AMBIGUOUS
)

# Sections whose text is proper nouns rather than technical description. Award
# and competition names collide constantly with product names, and a profile URL
# is not evidence of a skill.
SKILL_EXCLUDED_SECTIONS = frozenset({"achievements", "links", "header", "publications"})

SECTION_ALIASES: dict[str, list[str]] = {
    "experience": [
        "experience",
        "work experience",
        "professional experience",
        "employment",
        "employment history",
        "work history",
        "career history",
    ],
    "internships": ["internship", "internships", "internship experience"],
    "education": ["education", "academic background", "academics", "qualifications"],
    "projects": [
        "projects",
        "personal projects",
        "academic projects",
        "key projects",
        "selected projects",
    ],
    "skills": [
        "skills",
        "technical skills",
        "core skills",
        "technologies",
        "tech stack",
        "skills & tools",
        "technical proficiencies",
        "competencies",
    ],
    "certifications": [
        "certifications",
        "certificates",
        "licenses",
        "courses",
        "certifications & courses",
    ],
    "achievements": [
        "achievements",
        "awards",
        "honors",
        "honours",
        "accomplishments",
        "activities",
    ],
    "publications": ["publications", "papers", "research"],
    "summary": [
        "summary",
        "profile",
        "objective",
        "about",
        "professional summary",
        "career objective",
    ],
    "links": ["links", "contact", "profiles", "social"],
}

HEADING_TO_SECTION: dict[str, str] = {
    alias: section for section, aliases in SECTION_ALIASES.items() for alias in aliases
}


# ─── Matching a skill in free text ──────────────────────────────────────────
#
# `skill in text` is wrong, and wrong in a way that inflates rather than deflates — which is
# the worse direction. Measured on a real job description mentioning none of a candidate's
# skills:
#
#     "excellent communication, a digital-first mindset, strong organisational rigour"
#     matched: ['excel', 'git', 'go', 'r']   →  skill overlap 1.0, full marks
#
# "excellent" contains "excel", "digital" contains "git", "organisational" contains "go",
# and "r" is inside almost every word in English. Every job in the database was scored this
# way.
#
# Word boundaries fix it, but `\b` alone does not: real skill names end in symbols that are
# not word characters — c++, c#, .net, node.js, f#. `\b` after "c++" asserts a boundary
# after the final "+", which is a non-word char, so the assertion is about the wrong thing.
# Hence explicit guards on both sides against the characters that can *continue* a skill
# name.

_SKILL_EDGE_LEFT = r"(?<![a-z0-9+#.])"
_SKILL_EDGE_RIGHT = r"(?![a-z0-9+#])"

_skill_patterns: dict[str, re.Pattern[str]] = {}


def skill_pattern(skill: str) -> re.Pattern[str]:
    """A compiled matcher for one skill name. Cached — this runs per skill per job."""
    key = skill.strip().lower()
    if (cached := _skill_patterns.get(key)) is not None:
        return cached
    compiled = re.compile(
        _SKILL_EDGE_LEFT + re.escape(key) + _SKILL_EDGE_RIGHT, flags=re.IGNORECASE
    )
    _skill_patterns[key] = compiled
    return compiled


def mentions(text: str, skill: str) -> bool:
    """Does `text` actually name this skill, rather than merely contain its letters?

    The one place skill matching happens, so the scorer, the graph and the gap analysis all
    agree about what a job asks for. Two of them disagreeing would be worse than either
    being wrong, because the disagreement would be invisible.
    """
    if not text or not skill:
        return False
    return skill_pattern(skill).search(text) is not None


def mentioned_skills(text: str, skills: Iterable[str]) -> list[str]:
    """Every skill from `skills` that `text` actually names, in the order given."""
    if not text:
        return []
    low = text.lower()
    return [s for s in skills if s and mentions(low, s)]
