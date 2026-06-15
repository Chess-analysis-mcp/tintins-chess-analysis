"""FastAPI web layer for the interactive board (Phases 4-5).

Runs in the same process as the MCP server, importing the same singleton modules
(`server.core.engine`, `server.core.session`) so the engine pool and ReviewSession are
shared automatically. Do not create a second engine pool or session here.
"""
