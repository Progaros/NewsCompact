"""HTTP routers, one per area. Handlers parse/validate input and map results to HTTP;
SQL and business rules live in the app modules they call. Authorization is the
middleware's job (app/api.py)."""
from . import admin, auth, chat, feeds, monitor, pages, profile, reports, share, stories

ROUTERS = [m.router for m in (pages, stories, monitor, chat, auth, profile, reports, share, admin, feeds)]
