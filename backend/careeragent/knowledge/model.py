"""What a career graph is made of — addendum §14–19.

The value of a graph here is not storage. Every fact in it already lives in a relational
table and is correct there. The value is the questions a graph makes askable in one step
that SQL makes awkward in five:

    Which companies sit two hops from my strongest skills, have an open Bangalore role, and
    have never been contacted?

    What did I say to this company last time, and which resume did I send?

    Which skill, if I learned it, would connect me to the most currently-open jobs?

So this is a **projection, not a database**. Nodes and edges are derived from the existing
tables on read; nothing is written, nothing can drift, and there is no sync step to forget.
If a job's title changes in `job`, the graph's next read reflects it — because the graph is
just a way of looking at `job`.

**Node ids are typed and derived, never generated.** `job:7f3a…`, `skill:python`,
`company:acme`. Two consequences that matter: the same skill on a resume and in a job
description is the *same node* without a matching step, and a node id can be resolved back
to its row without a lookup table.

**Scope is mandatory.** 1,169 jobs against 50 skills is a hairball that renders as a grey
disc and answers nothing. Every projection here starts from a node and walks outward a
bounded number of hops, which is also how anyone actually reads a graph view.
"""

from __future__ import annotations

import enum
import re
from dataclasses import dataclass, field
from typing import Any

# A visualisation past this is a grey disc, and a traversal past it is a table.
MAX_NODES = 400


class NodeKind(enum.StrEnum):
    """What a node is. Each maps to exactly one table or one column value."""

    CANDIDATE = "candidate"
    SKILL = "skill"
    FACT = "fact"
    COMPANY = "company"
    JOB = "job"
    APPLICATION = "application"
    FAMILY = "family"
    ATS = "ats"
    LOCATION = "location"
    CONTACT = "contact"

    # People and what happens after you write to them. `PERSON` and `CONTACT` are
    # deliberately distinct: a person is somebody who works at a company, discovered from
    # what that company publishes; a contact is a channel we hold for reaching one. The
    # same human can be a person with no contact, which is the common case — 91 people
    # found so far, none with a published address.
    PERSON = "person"
    LEAD = "lead"
    OUTREACH = "outreach"
    INTERVIEW = "interview"
    OFFER = "offer"
    RESUME_VERSION = "resume_version"
    ROLE = "role"
    DEPARTMENT = "department"


class EdgeKind(enum.StrEnum):
    """How two nodes relate.

    Directed, and the direction is the one that reads naturally: a candidate HAS_SKILL, a
    job REQUIRES_SKILL. That matters for the questions — "what connects me to this job" is
    a different walk from "who else wants this skill".
    """

    HAS_SKILL = "has_skill"
    EVIDENCED_BY = "evidenced_by"
    REQUIRES_SKILL = "requires_skill"
    POSTED_BY = "posted_by"
    APPLIED_TO = "applied_to"
    SCORED = "scored"
    IN_FAMILY = "in_family"
    USES_ATS = "uses_ats"
    LOCATED_IN = "located_in"
    CONTACTED = "contacted"
    WORKS_AT = "works_at"

    # People and outreach. The directions are the ones that read naturally, because the
    # direction is what makes a walk answerable: "who have I not contacted at companies I
    # match" walks company → person and candidate → outreach → person, and needs the two
    # to point opposite ways to spot the gap.
    HAS_PERSON = "has_person"
    HAS_ROLE = "has_role"
    WORKS_IN = "works_in"
    RELEVANT_TO = "relevant_to"
    QUALIFIED_AS = "qualified_as"
    TARGETS = "targets"
    ABOUT_JOB = "about_job"
    REPLIED_TO = "replied_to"
    LED_TO = "led_to"
    USED_RESUME = "used_resume"


@dataclass(frozen=True)
class Node:
    """One thing. `id` is `kind:key` and is stable across reads."""

    kind: NodeKind
    key: str
    label: str
    # Anything a viewer needs that is not structural: a score, a status, a date. Kept small
    # — a node carrying a whole job description would make the graph the source of truth by
    # accident, and it is not.
    attrs: dict[str, Any] = field(default_factory=dict)

    @property
    def id(self) -> str:
        return f"{self.kind.value}:{self.key}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind.value,
            "key": self.key,
            "label": self.label,
            **({"attrs": self.attrs} if self.attrs else {}),
        }


@dataclass(frozen=True)
class Edge:
    """One relationship, from `source` to `target`, both node ids."""

    kind: EdgeKind
    source: str
    target: str
    weight: float = 1.0
    attrs: dict[str, Any] = field(default_factory=dict)

    @property
    def id(self) -> str:
        return f"{self.source}|{self.kind.value}|{self.target}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind.value,
            "source": self.source,
            "target": self.target,
            "weight": round(self.weight, 3),
            **({"attrs": self.attrs} if self.attrs else {}),
        }


class Graph:
    """A bounded set of nodes and edges, assembled for one question.

    Deduplicating on id is the reason this is a class rather than two lists: the same skill
    node is reached from a dozen jobs, and a projection that appended it a dozen times would
    render twelve overlapping circles.
    """

    def __init__(self, max_nodes: int = MAX_NODES) -> None:
        self._nodes: dict[str, Node] = {}
        self._edges: dict[str, Edge] = {}
        self.max_nodes = max_nodes
        self.truncated = False

    def add(self, node: Node) -> str:
        """Add a node, returning its id. Refuses silently once full — see `truncated`."""
        if node.id in self._nodes:
            # Merge attributes rather than replacing: a job reached first as a neighbour and
            # later as a subject should keep whichever pass knew more about it.
            existing = self._nodes[node.id]
            if node.attrs and node.attrs != existing.attrs:
                merged = {**existing.attrs, **node.attrs}
                self._nodes[node.id] = Node(
                    node.kind, node.key, existing.label or node.label, merged
                )
            return node.id
        if len(self._nodes) >= self.max_nodes:
            self.truncated = True
            return node.id
        self._nodes[node.id] = node
        return node.id

    def link(self, edge: Edge) -> None:
        """Add an edge. Dropped if either end was truncated away — an edge to a node that is
        not in the graph draws as a line into empty space."""
        if edge.source not in self._nodes or edge.target not in self._nodes:
            return
        self._edges.setdefault(edge.id, edge)

    def has(self, node_id: str) -> bool:
        return node_id in self._nodes

    def get(self, node_id: str) -> Node | None:
        return self._nodes.get(node_id)

    def neighbours(self, node_id: str) -> list[str]:
        out = []
        for edge in self._edges.values():
            if edge.source == node_id:
                out.append(edge.target)
            elif edge.target == node_id:
                out.append(edge.source)
        return out

    @property
    def nodes(self) -> list[Node]:
        return list(self._nodes.values())

    @property
    def edges(self) -> list[Edge]:
        return list(self._edges.values())

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for node in self._nodes.values():
            out[node.kind.value] = out.get(node.kind.value, 0) + 1
        return out

    def to_dict(self) -> dict[str, Any]:
        return {
            "nodes": [n.to_dict() for n in self._nodes.values()],
            "edges": [e.to_dict() for e in self._edges.values()],
            "counts": self.counts(),
            "node_count": len(self._nodes),
            "edge_count": len(self._edges),
            # Stated rather than implied. A viewer looking at a truncated graph and
            # concluding "that is all of it" would draw the wrong conclusion from it.
            "truncated": self.truncated,
            "max_nodes": self.max_nodes,
        }

    def __len__(self) -> int:
        return len(self._nodes)


# ─── Key derivation ─────────────────────────────────────────────────────────
#
# Ids are derived from the data so that the same thing found twice is one node. Getting this
# wrong is the classic graph bug: "Python" from a resume and "python" from a job description
# become two nodes, the candidate appears unconnected to the job, and the graph quietly
# answers the opposite of the truth.

_SLUG = re.compile(r"[^a-z0-9]+")


def slug(text: str) -> str:
    """Lowercase, punctuation collapsed. The key for anything matched by name."""
    return _SLUG.sub("-", (text or "").strip().lower()).strip("-")


def skill_node(label: str) -> Node:
    """A skill, keyed canonically.

    Routed through the taxonomy so an alias and its canonical name are the same node —
    "tf" and "tensorflow" must not sit in the graph as two unconnected circles, for the
    same reason they must not be counted as two matched skills.
    """
    from ..resume.taxonomy import ALIAS_TO_CANONICAL

    text = (label or "").strip().lower()
    canonical = ALIAS_TO_CANONICAL.get(text, text)
    return Node(NodeKind.SKILL, slug(canonical), canonical)


def company_node(name: str, **attrs: Any) -> Node:
    return Node(NodeKind.COMPANY, slug(name), name.strip(), attrs)


def person_node(name: str, person_id: str = "", **attrs: Any) -> Node:
    """One human being at one company.

    Keyed on the stored id when there is one, because two people can share a name — the
    same reason `same_person` never merges on a name alone. Falls back to a slug for a
    person who has been found but not yet stored.
    """
    key = person_id or slug(name)
    return Node(NodeKind.PERSON, key, (name or "").strip() or "unnamed", attrs)


def role_node(title: str) -> Node:
    """A job title as a shared thing, so two engineering managers at different companies
    sit on the same node and the graph can answer "who else does this job"."""
    return Node(NodeKind.ROLE, slug(title), (title or "").strip() or "unknown role")


def department_node(function: str) -> Node:
    """The function somebody works in — ai_ml, data, recruiting. Already the vocabulary the
    relevance scorer uses, so the graph and the ranking cannot disagree about it."""
    label = (function or "").replace("_", "/") or "unknown"
    return Node(NodeKind.DEPARTMENT, slug(function or "unknown"), label)


def location_node(text: str) -> Node:
    """A location, reduced to its city.

    "Bangalore, India", "Bengaluru, KA" and "Bangalore" are one place. The reduction is
    crude — first component, lowercased — and crude is the right level here: a graph that
    splits one city into four nodes is worse than one that occasionally merges two.
    """
    head = (text or "").split(",")[0].strip()
    normalized = {"bengaluru": "bangalore", "gurugram": "gurgaon", "mumbai": "mumbai"}.get(
        head.lower(), head.lower()
    )
    return Node(NodeKind.LOCATION, slug(normalized), head or "unknown")
