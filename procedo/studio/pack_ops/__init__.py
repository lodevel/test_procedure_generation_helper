"""Deterministic pack-operation bridges — GUI-side clients of the active
bundle's ``rules_packager_base`` wheel (parse/render/validate/codegen via
:mod:`pack_parsers`).

Wheel code runs in the *project's* venv, in its persistent engine process
(:mod:`.engine`), so the bundle that ships with the project — not the GUI's
environment — defines procedure.json. No LLM involvement; the LLM backend
lives in ``procedo.studio.llm``.

This package intentionally has no imports of its own so that importing a
bridge module does not drag in the LLM backend stack the way the old
``procedo.studio.llm`` location did.
"""
