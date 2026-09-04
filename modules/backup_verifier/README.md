# Smart Backup Integrity Verifier (Phase 2+)
#
# Implement ``module.py`` exporting ``MODULE`` that conforms to ModuleProtocol
# in ``app.core.registry``. Suggested hooks:
#   - get_router()           → /api/modules/backup_verifier/*
#   - on_topology_refresh()  → map backup targets to discovered hosts
#   - on_startup()           → schedule integrity checks
#
# Consume the unified topology; keep backup credentials in module-local config.
