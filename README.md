# LLM Workflow Editor (`workflow_editor`)

A PySide6 Qt desktop application for authoring, editing, and reviewing structured
test procedures with LLM assistance. It is the **AI-assisted authoring/editing GUI**
that sits alongside the main Test Procedure GUI: where the main GUI authors and
*runs* procedures, this editor uses an LLM to transform plain-language intent into
the canonical `procedure.json` and generated `test.py`, keeping the natural-language
text, JSON, and code in sync.

It lives in `external/test_procedure_generation_helper/` as a sibling package of the
main GUI (`src/test_procedure_gui/`). The host's `script.bat` installs it **editable**
into the host venv (`pip install -e`), so `workflow_editor` imports like any
dependency — no `sys.path` / `PYTHONPATH` glue. The legacy path bootstraps remain
only as logged deprecation shims. See the host repo's
`docs/adr/0003-editor-installed-package.md`.

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
  to the *project's* venv via `workflow_editor/llm/pack_parsers.py` (which spawns
  `_pack_parsers_subprocess.py` against the `rules_packager_base` wheel), so the
  rules/wheel used are the ones bundled with the active project — not the editor's
  own interpreter.
- **Per-op execution daemon.** `workflow_editor/llm/_execute_op_subprocess.py` is a
  persistent runner that executes individual procedure ops against live equipment
  (used by the main GUI's interactive/manual runner).

## Project layout

Top-level package directory:

```
test_procedure_generation_helper/
├── README.md
├── requirements.txt
├── config/
│   └── tab_contexts.json          # Fallback per-task rules/prompts
├── tests/                         # pytest suite
└── workflow_editor/
    ├── __init__.py                # Package init, __version__ = "0.1.0"
    ├── __main__.py                # CLI entry point (python -m workflow_editor)
    ├── main_window.py             # Main application window
    ├── theme.py                   # App theming (light/dark, modern workspace)
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
    │   ├── opencode_backend.py    # OpenCode (WSL server) backend
    │   ├── external_api_backend.py# OpenAI-compatible HTTP backend
    │   ├── pack_parsers.py        # Façade: parse/codegen/validate in project venv
    │   ├── _pack_parsers_subprocess.py   # Parser runner (imports the wheel only)
    │   ├── _execute_op_subprocess.py     # Per-op execution daemon (imports drivers)
    │   ├── prompt_builder.py / reconstruction.py / response_parser.py
    │   ├── section_ownership.py / output_contracts.py / validator_dispatch.py
    │   ├── server_manager.py      # OpenCode server lifecycle
    │   └── worker.py              # Async LLM worker thread
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
`workflow_editor/authoring/locations.py` — see `docs/authoring-a-skill.md` in this
repo for writing one. The authoritative cross-package contracts live at the host
repo root, see below.)

## Requirements

From `requirements.txt`:

- **Python:** 3.10+
- **PySide6** ≥ 6.5.0 — Qt GUI framework
- **requests** ≥ 2.28.0 — HTTP client for the External API backend

The editor process itself needs only these. Hardware drivers (labscpi / fncore,
VISA, BLE) and the rules/parser wheel are **not** dependencies of the editor — they
live in each *project's* venv and are invoked there by the subprocess runners
(`_pack_parsers_subprocess.py`, `_execute_op_subprocess.py`).

## Launching

### Standalone

```bash
python -m workflow_editor [options]
```

Run from a venv **without** the host app, the editor starts in **degraded mode**:
`project_services`-backed features (package-library tiers, full .docx report, the
shared project/bundle/scenario/config dialogs) disable or report unavailable —
nothing crashes. The exact consumed surface and guard rule are Contract C
(`../../docs/contract_c_editor_host_services.md`).

Command-line options (see `workflow_editor/__main__.py`):

| Option | Meaning |
|--------|---------|
| `--project-root PATH` | Project root (contains `tests/` and/or `config/`) |
| `--rules-root PATH`   | Folder of rule `*.md` files (usually the active bundle's `rules/`) |
| `--test-name NAME`    | Open a test folder by name under `tests/` |
| `--test-dir PATH`     | Direct path to a test folder (overrides `--test-name`) |
| `--llm-backend {opencode,external,none}` | Backend hint (see note below) |
| `--llm-profile NAME`  | LLM profile name |
| `--debug`             | Enable debug logging |
| `--log-file PATH`     | Write logs to a file |

Examples:

```bash
# Open a project and a specific test
python -m workflow_editor --project-root /path/to/Project --test-name voltage_test

# Open a test folder directly
python -m workflow_editor --test-dir /path/to/Project/tests/voltage_test
```

Note: the active backend is resolved from the persisted `llm_backend` setting (see
Configuration). The `--llm-backend` and `--llm-profile` flags are parsed and stored
on the window (`_cli_llm_backend` / `_cli_llm_profile`) but are not currently
re-read — the saved setting is what selects the backend on startup.

### From the main GUI

The main Test Procedure GUI launches this editor for you — via the **Workflow** menu
("Open Workflow Editor", "Edit Test in Workflow Editor") or the test-list context
menu ("Edit in Workflow Editor"). `src/test_procedure_gui/main_window.py::_launch_workflow_editor`
formats a configured command template (its `{python}` placeholder defaults to the
GUI's `sys.executable`), launches it via `subprocess.Popen`, and appends
`--project-root`, `--rules-root` (the active bundle's `rules/`), and `--test-name`.
No `PYTHONPATH` injection: `workflow_editor` is installed editable in the GUI venv
(ADR 0003). The editor path and command
template are set in the main GUI's **Workflow Editor Settings** dialog.
Project-specific parsing/validation then runs in the project's own venv via
`pack_parsers.py`, so the editor does not itself need the project venv.

## LLM backend configuration

Settings are stored in `~/.workflow_editor/settings.json` and edited in the
in-app **Settings** dialog (`workflow_editor/dialogs/settings_dialog.py`). The
`llm_backend` key selects one of `opencode`, `external_api`, or `none`.

### Model-string format — important

The two backends interpret the **Model** field differently:

- **OpenCode** (`opencode_backend.py`): the model is `providerID/modelID`, **split on
  the FIRST `/`** (`opencode_backend.py:362-367`, `:722-727`). The text before the
  first `/` is the OpenCode provider; everything after is the model id (which may
  itself contain `/`).
  - `anthropic/claude-3-5-sonnet` → provider `anthropic`, model `claude-3-5-sonnet`
  - `my_vllm/cyankiwi/gemma-3-27b` → provider `my_vllm`, model `cyankiwi/gemma-3-27b`
  - Leave **blank** to use the OpenCode server default. A bare name with no `/` is
    ignored — the request omits the `model` field and the server default is used; it
    is **not** treated as a provider.
  - The provider (e.g. a custom `@ai-sdk/openai-compatible` provider named `my_vllm`)
    must exist in your OpenCode config (`~/.config/opencode/` or a project
    `opencode.json`).
- **External API** (`external_api_backend.py:116`): the model is sent **verbatim** as
  the `model` field — **no split, no provider prefix**. Use the plain model name your
  endpoint expects (e.g. `gpt-4`, `qwen3:8b-16k`). Do **not** slash-prefix it.

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

### External API backend (OpenAI-compatible)

```json
{
  "llm_backend": "external_api",
  "external_api": {
    "url": "https://api.openai.com/v1",
    "key": "sk-...",
    "model": "gpt-4"
  }
}
```
`model` is sent verbatim — no provider prefix. Point `url` at any OpenAI-compatible
endpoint (e.g. `http://127.0.0.1:11434/v1` for Ollama).

### `none`

Disables the LLM. The editor still loads, parses, and lets you hand-edit artifacts;
LLM-driven tabs return a disabled-backend error.

## Per-op execution daemon

`workflow_editor/llm/_execute_op_subprocess.py` is a persistent NDJSON-framed daemon
(one JSON object per line, both directions) that executes individual procedure ops
against live instruments. It is the only runner that imports the hardware drivers,
and it is driven by the main GUI's interactive/manual runner
(`guided_manual_execution_dialog.py`). Key properties:

- **Invoked by file path** in the *project* venv (not `python -m`), so it skips the
  PySide6 package init that the project venv lacks.
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
  raw-resource fallbacks. This powers the main GUI's per-equipment raw-command
  terminal; a new pack's driver becomes terminal-addressable just by exposing those
  surfaces, with no per-driver branch (`_raw_send`, `_execute_op_subprocess.py:174-227`).

## How it ties into the main GUI

- The main GUI imports this package from its venv's editable install; the old
  `app.py::_bootstrap_editor_on_path` sys.path hack survives only as a logged
  deprecation shim.
- Several main-GUI modules import from `workflow_editor` directly —
  `llm.pack_parsers` (e.g. `bench_fields`, `bench_constant_names`,
  `build_manual_run`/`build_manual_result`), `llm.section_ownership`, and
  `core.task_config`.
- The interactive/manual runner (`guided_manual_execution_dialog.py`) uses
  `pack_parsers` and the per-op execution daemon to run ops against live equipment;
  its bench Console (per-op output, reconnects, safe-off, and terminal traffic) is
  mirrored into `_console_lines` (capped at `_CONSOLE_SAVE_CAP = 20000`) and persisted
  into `result.json` as `console_log`.
- The "Edit in Workflow Editor" / "Edit Test in Workflow Editor" actions launch this
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
- `../../docs/contract_c_editor_host_services.md` — Contract C: what this editor consumes from `project_services` + degraded mode
- `../../docs/adr/0003-editor-installed-package.md` — why the editor is an installed (editable) package
- `../../TODO.md` — current status / task tracker
