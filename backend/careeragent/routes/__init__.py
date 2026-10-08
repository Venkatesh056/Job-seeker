"""HTTP routers, one per domain.

`api.py` owns the app, the lifespan and the read endpoints that existed before this
package (status, candidate, jobs, events, discovery runs). Everything added since
lives here, one router per domain, so a domain's endpoints are in exactly one file.

The rule that shapes every router: **an endpoint that can cause an irreversible
external action takes an explicit action, never a side effect of a read.** Parsing a
command returns a preview. Preparing an application fills a form and stops.
Submitting requires a separate call to `/approve`. Sending outreach requires
`/approve` and then `/send`.
"""

from .analytics import router as analytics_router
from .applications import router as applications_router
from .auth import router as auth_router
from .campaigns import router as campaigns_router
from .companies import router as companies_router
from .decisions import router as decisions_router
from .integrations import router as integrations_router
from .knowledge import router as knowledge_router
from .learning import router as learning_router
from .monitoring import router as monitoring_router
from .outreach import router as outreach_router
from .people import router as people_router
from .preferences import router as preferences_router
from .resumes import router as resumes_router
from .settings import router as settings_router
from .supervisor import router as supervisor_router

ROUTERS = (
    # Before everything, because it is how a caller becomes somebody. Its own endpoints
    # are listed in auth.deps.PUBLIC_PATHS so registering and logging in do not require
    # the token they exist to hand out.
    auth_router,
    # First, because it is the entry point that decides which of the rest to use.
    supervisor_router,
    # Second, because nothing else ranks correctly until this is answered.
    preferences_router,
    campaigns_router,
    resumes_router,
    applications_router,
    companies_router,
    people_router,
    outreach_router,
    analytics_router,
    learning_router,
    knowledge_router,
    monitoring_router,
    integrations_router,
    decisions_router,
    settings_router,
)

__all__ = ["ROUTERS"]
