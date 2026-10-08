"""The career knowledge graph — addendum §14–19.

A projection over the relational tables, not a second store. Nodes and edges are derived on
read, so the graph cannot drift from the data and there is no sync step to forget.

    from careeragent.knowledge import candidate_graph, around, questions

    graph = await candidate_graph(session, candidate_id)   # the default view
    graph = await around(session, "company:swiggy", candidate_id, depth=2)
    rows  = await questions.untouched_companies(session, candidate_id)

`model` defines what a node and an edge are, `project` builds bounded views, and `questions`
holds the traversals that are worth asking by name — the ones a graph answers in one step
and SQL answers in five.
"""

from __future__ import annotations

from . import project, questions
from .model import MAX_NODES, Edge, EdgeKind, Graph, Node, NodeKind, company_node, skill_node, slug
from .project import around, candidate_graph, stats

__all__ = [
    "MAX_NODES",
    "Edge",
    "EdgeKind",
    "Graph",
    "Node",
    "NodeKind",
    "around",
    "candidate_graph",
    "company_node",
    "project",
    "questions",
    "skill_node",
    "slug",
    "stats",
]
