"""LangGraph multi-agent orchestration.

Three graphs with very different shapes and lifetimes:

  * `supervisor` — a goal in a sentence. The only one whose order is not written down:
    it decides each step from what the last one found, which is what separates a
    supervisor from a pipeline. It can queue and prepare work; it cannot submit any,
    because no step for that exists.

  * `campaign` — discovery. Five agents, two `Send()` fan-outs, runs for minutes.
    A graph earns its place here: sourcing is a map over (family × source) and
    enrichment is a map over companies, and both merge concurrent writes into one
    state through reducers rather than through hand-written merge code.

  * `apply` — one application. Linear, but interruptible at the approval, which can
    sit for days. The live browser page it holds is deliberately *not* graph state —
    a page cannot be checkpointed, and approving a form nobody saw is the failure the
    whole design exists to prevent.
"""

from .apply import (
    MODES,
    build_apply_graph,
    get_apply_graph,
    pending_approval,
    resume_application,
    start_application,
)
from .campaign import build_campaign_graph, get_campaign_graph, run_campaign
from .pool import ApplyPool, Batch, Item, get_pool
from .state import ApplyState, CampaignState, EnrichTask, SourcingTask, SupervisorState
from .supervisor import (
    STEPS,
    build_supervisor_graph,
    get_supervisor_graph,
    run_supervisor,
)

__all__ = [
    "MODES",
    "STEPS",
    "ApplyPool",
    "ApplyState",
    "Batch",
    "CampaignState",
    "EnrichTask",
    "Item",
    "SourcingTask",
    "SupervisorState",
    "build_apply_graph",
    "build_campaign_graph",
    "build_supervisor_graph",
    "get_apply_graph",
    "get_campaign_graph",
    "get_pool",
    "get_supervisor_graph",
    "pending_approval",
    "resume_application",
    "run_campaign",
    "run_supervisor",
    "start_application",
]
