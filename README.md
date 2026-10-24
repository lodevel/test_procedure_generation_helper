# Procedo Studio (`procedo.studio`)

A PySide6 Qt desktop application for authoring, editing, and reviewing structured
test procedures with LLM assistance. It is the **AI-assisted authoring/editing GUI**
that sits alongside Procedo Bench: where Procedo Bench authors and
*runs* procedures, this editor uses an LLM to transform plain-language intent into
the canonical `procedure.json` and generated `test.py`, keeping the natural-language
text, JSON, and code in sync.

It lives in `external/test_procedure_generation_helper/` as a sibling package of the
Procedo Bench (`src/procedo/bench/`). The host's `script.bat` installs it **editable**
into the host venv (`pip install -e`), so `procedo.studio` imports like any
dependency — no `sys.path` / `PYTHONPATH` glue. The legacy path bootstraps remain
only as logged deprecation shims. See the host repo's
`docs/adr/0003-editor-installed-package.md`.

**Non-editable installs** additionally require the vendored `external/opencode_client`
submodule to be checked out (`git submodule update --init`):
`procedo.studio.llm.backend_base` imports `opencode_client`, so a plain
`pip install .` from a clone without the submodule yields a package that cannot
import its LLM backends.

## What it does

- **Text → JSON → Code pipeline.** Three authoring tabs (`Text`, `Text-JSON`,
  `JSON-Code`) drive the LLM to turn a natural-language procedure into structured
  `procedure.json`, then into runnable `test.py`, plus a read-only `Traceability`
  view mapping JSON steps to code blocks.
- **Per-tab LLM conversations.** Each tab keeps its own conversation, context, and
  open-question state — messages in one tab don't bleed into another.
- **Diff-gated edits.** Every LLM-proposed change to an artifact is shown in a
  side-by-side diff (`dialogs/diff_viewer.py`) and applied only on explicit accept.
- **Pack-driven parsing/validation.** Parsing, codegen, and validation are dispatched
  to the *project's* venv via `procedo/studio/pack_ops/pack_parsers.py` (each op a
  frame to the project's persistent engine process, `procedo/engine_client/engine.py`, which
  runs `engine_client/_execute_op_subprocess.py --role tools` against the
  `rules_packager_base` wheel), so the rules/wheel used are the ones bundled with
  the active project — not the editor's own interpreter.
- **Per-op execution daemon.** `procedo/engine_client/_execute_op_subprocess.py` is a
  persistent runner that executes individual procedure ops against live equipment
  (used by Procedo Bench's interactive/manual runner).

## Project layout

Top-level package directory:

```
test_procedure_generation_helper/
├── README.md
├── requirements.txt
├── config/
│   └── tab_contexts.json          # Fallback per-task rules/prompts
├── tests/                         # pytest suite
├── procedo/engine_client/         # The engine-process stack, shared with Procedo Bench (no LLM, no Qt)
│   ├── engine.py                  # One engine process per project and role
│   ├── engine_session.py          # RemoteSession: the channel to one engine process
│   ├── bench_client.py            # The RUN frames (bench session)
│   ├── engine_replies.py          # Engine replies as app-facing objects
│   ├── project_venv.py            # Where the project's venv is (asks the host)
│   └── _execute_op_subprocess.py  # Per-op execution daemon (imports drivers) + its _-prefixed siblings
└── procedo/studio/
    ├── __init__.py                # Package init, __version__ = "0.1.0"
    ├── __main__.py                # CLI entry point (python -m procedo.studio)
    ├── main_window.py             # Main application window
    ├── theme.py                   # Studio-only colours (chat, diff, syntax); the app theme is Bench's
    ├── logging_config.py
    ├── core/                      # Non-LLM domain logic
    │   ├── artifact_manager.py    # Shared artifact storage
    │   ├── project_manager.py     # Project/test/rules discovery
    │   ├── session_state.py       # Assumptions/decisions/questions
    │   ├── task_config.py         # Per-task config (config.json:workflows)
    │   ├── validators.py / validators_registry.py  # Artifact validators + registry
    │   ├── cheatsheet.py          # Syntax cheatsheet (extracted from bundle rule docs at runtime)
    │   ├── odb_inspect.py         # ODB++ board/netlist inspection
    │   └── ...
    ├── llm/                       # LLM integration + execution
    │   ├── backend_base.py        # Abstract LLMBackend + NoneBackend
    │   ├── backend_factory.py     # Per-tab backend instances (BackendConfig)
    │   ├── opencode_backend.py    # OpenCode backend (re-exports the opencode_client client)
    │   ├── prompt_builder.py / reconstruction.py / response_parser.py
    │   ├── section_ownership.py / output_contracts.py / validator_dispatch.py
    │   ├── server_manager.py      # OpenCode server lifecycle
    │   └── worker.py              # Async LLM worker thread
    ├── pack_ops/                  # The bridge into the project venv
    │   ├── pack_parsers.py        # Façade: parse/codegen/validate in project venv
    │   ├── engine_async.py        # Engine calls off the GUI thread
    │   └── bundle_capabilities.py # Bundle defaults readers + capability gating
    ├── tabs/                      # Tab widgets
    │   ├── text_only_tab.py       # "Text" tab
    │   ├── text_json_tab.py       # "Text-JSON" tab
    │   ├── json_code_tab.py       # "JSON-Code" tab
    │   ├── traceability_tab.py    # "Traceability" tab (read-only)
    │   └── workspace_tab.py       # Workspace (modern layout)
    ├── authoring/                 # Skill/wizard packages: tier discovery, skill chat, MCP tool servers
    ├── dock/                      # Dock panels (chat, session, findings, raw response)
    ├── dialogs/                   # settings, diff viewer, new-project, syntax reference
    └── widgets/                   # project bar, find/replace, netlist panel, rule selector
```

(The `__init__.py` reports `__version__ = "0.1.0"`. Authoring-skill packages are
discovered from four tiers — builtin / bundled / local / project, resolved in
`procedo/studio/authoring/locations.py` — see `docs/authoring-a-skill.md` in this
repo for writing one. The authoritative cross-package contracts live at the host
repo root, see below.)

## Requirements

From `requirements.txt`:

- **Python:** 3.10+
- **PySide6** ≥ 6.5.0 — Qt GUI framework
- **requests** ≥ 2.28.0 — HTTP client for the OpenCode server API

The editor process itself needs only these. Hardware drivers (labscpi / fncore,
VISA, BLE) and the rules/parser wheel are **not** dependencies of the editor — they
live in each *project's* venv and are invoked there by the engine process
(`_execute_op_subprocess.py`).

## Launching

### Standalone

```bash
python -m procedo.studio [options]
```

Studio needs the host app installed (it wears Bench's theme, `procedo.ui.theme`); there
is no degraded mode without it. The consumed surface is Contract C (`../../docs/contract_c_editor_host_services.md`).

Command-line options (see `procedo/studio/__main__.py`):

| Option | Meaning |
|--------|---------|
| `--project-root PATH` | Project root (contains `tests/` and/or `config/`) |
| `--test-name NAME`    | Open a test folder by name under `tests/` |
| `--test-dir PATH`     | Direct path to a test folder (overrides `--test-name`) |
| `--llm-backend {opencode,external,none}` | Backend hint (see note below) |
| `--llm-profile NAME`  | LLM profile name |
| `--debug`             | Enable debug logging |
| `--log-file PATH`     | Write logs to a file |

Examples:

```bash
# Open a project and a specific test
python -m procedo.studio --project-root /path/to/Project --test-name voltage_test

# Open a test folder directly
python -m procedo.studio --test-dir /path/to/Project/tests/voltage_test
```

Note: the active backend is resolved from the persisted `llm_backend` setting (see
Configuration). The `--llm-backend` and `--llm-profile` flags are parsed and stored
on the window (`_cli_llm_backend` / `_cli_llm_profile`) but are not currently
re-read — the saved setting is what selects the backend on startup.

### From Procedo Bench

Procedo Bench launches this editor for you — via the **Test** menu
("Open Procedo Studio…", "Edit…") or the test-list context
menu ("Edit in Procedo Studio"). `src/procedo/bench/main_window.py::_launch_workflow_editor`
formats a configured command template (its `{python}` placeholder defaults to the
GUI's `sys.executable`), launches it via `subprocess.Popen`, and appends
`--project-root` and `--test-name`; Studio reads the rules of each project it opens
from that project's active bundle (`bundle/rules/`), plus the project's own rule
documents (`config/project_rules/`), which win where they conflict.
No `PYTHONPATH` injection: `procedo.studio` is installed editable in the GUI venv
(ADR 0003). The editor path and command
template are set on Procedo Bench's **Settings → Procedo Studio** page.
Project-specific parsing/validation then runs in the project's own venv via
`pack_parsers.py`, so the editor does not itself need the project venv.

## LLM backend configuration

Settings are stored in `%APPDATA%\Procedo\studio\settings.json` (copied once from the old
`~/.workflow_editor` folder, which is kept) and edited in the
in-app **Settings** dialog (`procedo/studio/dialogs/settings_dialog.py`). The
`llm_backend` key selects `opencode` or `none` (the External API backend was removed;
OpenCode providers cover OpenAI-compatible endpoints).

### Model-string format — important

The OpenCode **Model** field is `providerID/modelID`, **split on the FIRST `/`**
(`_build_message_body` in `external/opencode_client/opencode_client/client.py`, shared by
the sync and streaming send paths). The text before the first `/` is the OpenCode
provider; everything after is the model id (which may itself contain `/`).

- `anthropic/claude-3-5-sonnet` → provider `anthropic`, model `claude-3-5-sonnet`
- `my_vllm/cyankiwi/gemma-3-27b` → provider `my_vllm`, model `cyankiwi/gemma-3-27b`
- Leave **blank** to use the OpenCode server default. A bare name with no `/` is
  ignored — the request omits the `model` field and the server default is used; it
  is **not** treated as a provider.
- The provider (e.g. a custom `@ai-sdk/openai-compatible` provider named `my_vllm`)
  must exist in your OpenCode config (`~/.config/opencode/` or a project
  `opencode.json`).

### OpenCode backend

Runs against an OpenCode server (typically in WSL) on a configurable port (default
4096). Example `settings.json` fragment:

```json
{
  "llm_backend": "opencode",
  "opencode": {
    "port": 4096,
    "host": "127.0.0.1",
    "model": "anthropic/claude-3-5-sonnet"
  }
}
```
`model` is `providerID/modelID`, split on the first `/`; leave it `""` for the server default.

### `none`

Disables the LLM. The editor still loads, parses, and lets you hand-edit artifacts;
LLM-driven tabs return a disabled-backend error.

## Per-op execution daemon

`procedo/engine_client/_execute_op_subprocess.py` is a persistent NDJSON-framed daemon
(one JSON object per line, both directions) that executes individual procedure ops
against live instruments. It is the only runner that imports the hardware drivers,
and it is driven by Procedo Bench's interactive/manual runner
(`guided_manual_execution_dialog.py`). Key properties:

- **Invoked by file path** in the *project* venv (not `python -m`): the project venv
  has no `procedo` package.
- **No-reset guarantee:** it only `connect()`/`open()` + `initialize()`, never
  `reset()`, so it never disturbs the bench state an operator set up.
- **Per-device session policy** (`per_step` vs `per_session`): per-session devices are
  opened once and closed (unlocked) only at shutdown — important for instruments whose
  `close()` releases the remote lock and drops the output.
- **One mapping seam:** it does not re-implement op→driver-call mapping; it drives each
  pack's `emit_python(op, ctx)` through a capture-context and execs the captured remote
  branch against the live device.
- **Raw terminal seam:** a `raw` command dispatches on driver attributes by presence —
  `query_raw`/`write_raw` (psu/eload/scope), then `raw_command` (fncore line protocol,
  multi-line drain) or legacy `_write_readline`, with defensive `.s`/`._session` and
  raw-resource fallbacks. This powers Procedo Bench's per-equipment raw-command
  terminal; a new pack's driver becomes terminal-addressable just by exposing those
  surfaces, with no per-driver branch (`_raw_send`, `_execute_op_subprocess.py:174-227`).

## How it ties into Procedo Bench

- Procedo Bench imports this package from its venv's editable install.
- Bench and its shared core reach the engine processes through `procedo.engine_client`
  (`engine`, `bench_client`, `engine_replies` — `ManualRunResult` of `build_manual_run`).
- Several main-GUI modules import from `procedo.studio` directly —
  `pack_ops.bundle_capabilities` (`bench_fields`, `bench_constant_names`), `llm.section_ownership`, and
  `core.task_config`.
- The interactive/manual runner (`guided_manual_execution_dialog.py`) uses
  `pack_parsers` and the per-op execution daemon to run ops against live equipment;
  its bench Console (per-op output, reconnects, safe-off, and terminal traffic) is
  mirrored into `_console_lines` (capped at `_CONSOLE_SAVE_CAP = 20000`) and persisted
  into `result.json` as `console_log`.
- The "Edit in Procedo Studio" / "Test → Edit…" actions launch this
  editor as a separate process for full LLM-assisted authoring of the selected test.

## Testing

The `tests/` directory contains a pytest suite:

```bash
pytest -q
```

## Related documentation

The authoritative cross-package contracts live at the repository root (not in this
package):

- `../../docs/contract_a_gui_wheel_api.md` — Contract A: the GUI ↔ wheel `pack_parsers` façade
- `../../docs/contract_b_bundle_engine_api.md` — Contract B: the bundle ↔ engine API
- `../../docs/contract_c_editor_host_services.md` — Contract C: what this editor consumes from the host (`procedo.core`, Bench)
- `../../docs/adr/0003-editor-installed-package.md` — why the editor is an installed (editable) package
- `../../TODO.md` — current status / task tracker
