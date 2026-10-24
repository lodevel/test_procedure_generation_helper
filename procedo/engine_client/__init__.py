"""The engine-process stack, shared by Procedo Bench and Procedo Studio.

App side: the registry of a project's engine processes (:mod:`.engine`), the channel to
one (:mod:`.engine_session`), the RUN frames (:mod:`.bench_client`), the reply shapes
(:mod:`.engine_replies`) and where the project's venv is (:mod:`.project_venv`).

Engine side: the daemon ``_execute_op_subprocess.py`` and its siblings (``_engine_ops``,
``_engine_ops_manual``, ``_engine_ops_extract``, ``_instrument_ops``, ``_safe_off``,
``_bench_session_runner``, ``_guided_call``),
run by FILE PATH in the project venv, which has no ``procedo`` package.

No LLM code and no Qt. This package has no imports of its own.
"""
