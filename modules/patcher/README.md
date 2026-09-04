# AI-Driven Patch-Management (Phase 2+)
#
# Implement ``module.py`` exporting ``MODULE`` that conforms to ModuleProtocol
# in ``app.core.registry``. Suggested hooks:
#   - get_router()           → /api/modules/patcher/*
#   - on_topology_refresh()  → scan package versions on discovered guests
#   - on_startup()           → schedule patch evaluation jobs
#
# Do not modify core discovery — consume TopologySnapshot from the hook.
