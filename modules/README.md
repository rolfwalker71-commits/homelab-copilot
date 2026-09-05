# Module (Plugin-Slots)

Plugin-Slots für Phase 2+. Lege `modules/<name>/module.py` ab — kein Core-Rewrite nötig.

- Vorlage: [`example/module.py`](example/)
- Vertrag: `ModuleProtocol` in `app/core/registry.py`
- Überblick inkl. Codebeispiel: [README.md § Modul-Framework](../README.md#modul-framework)
- Aktiv: [`patcher/`](patcher/), [`backup_verifier/`](backup_verifier/), [`health/`](health/)

Router landen unter `/api/modules/<name>/…`.
