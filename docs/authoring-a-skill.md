# Authoring a skill

A **skill** is a no-code, LLM chat assistant you add to the test-procedure
editor. It is just a folder holding a `SKILL.md` — an LLM system prompt plus a
little YAML frontmatter. The editor runs that prompt as a multi-turn chat over
your project data; the model produces a **rough draft** procedure — intent in
plain, operator-readable steps, *not* valid DSL grammar — which you insert into
the procedure editor, where the normal parse/codegen pipeline refines it.

There is nothing to compile, no Python to write, and no rebuild for the skill
itself. Drop a folder, open the **Skills** menu, and it is there.

## Quick start

1. Create a folder under the local drop-in dir (use **Skills → Open skills
   folder…** to open it, or see [Locations](#locations--precedence)):

   ```text
   local_packages/authoring_skills/my_first_skill/SKILL.md
   ```

2. Put a minimal `SKILL.md` in it:

   ```markdown
   ---
   name: My first skill
   when-to-use: Draft a rough functional test for one feature.
   version: 0.1.0
   ---
   You help the engineer draft a ROUGH test procedure. Express intent in plain,
   operator-readable steps — NOT final DSL grammar; the editor's pipeline refines
   it afterward. Use the attached context first; ask only what you can't infer.
   Output ONE coherent block of plain steps, one action per line.
   ```

3. In the editor, open the **Skills** menu and pick **My first skill**. (The menu
   re-scans every time it opens, so a freshly-dropped folder shows up without a
   restart.)

4. In the chat dialog, check the context you want to send (Rules / Documents /
   Artifacts — see [Context tabs](#context-tabs)), then chat. The skill may ask
   questions; refine over a few turns.

5. Click **Insert into procedure**. The latest draft is raw-appended to the
   procedure text editor. Edit and validate it there as usual.

## SKILL.md reference

`SKILL.md` is YAML frontmatter between `---` fences, followed by the prompt body.
All fields are optional **except the body**.

```markdown
---
kind: skill-chat        # informational only; default if omitted
name: Power rail check  # title in the Skills menu (falls back to folder name)
when-to-use: One-line description (shown as the menu tooltip)
version: 0.1.0
---
<the system prompt — the body IS the prompt>
```

| Field | Required | Role |
|---|---|---|
| (body) | yes | The LLM system prompt — write it as instructions to the model: what to detect, what to ask, what to output. |
| `name` | no | Human title shown in the Skills menu. Falls back to the folder name. |
| `when-to-use` | no | One-line description, shown as the menu-item tooltip. |
| `version` | no | Free-form version string. |
| `kind` | no | Informational; defaults to `skill-chat`. **Does not** decide the type — see below. |

**The type is decided by the folder, not the frontmatter.** Chat skills live in
`authoring_skills/`. Wizards (a separate, code-based feature) live in a parallel
`authoring_wizards/` folder that the chat scanner never reads. So the folder you
drop into determines what your skill is; the `kind` field is documentation only.

**Writing the body.** The body is the model's whole instruction set. Tell it to
lean on the attached context first, ask only what it genuinely cannot infer
(a couple of questions at a time), and emit one coherent block of plain steps —
one action per line. Where no instrument mapping exists, instruct it to write an
operator "probe / read / judge" step rather than inventing grammar; the editor's
pipeline and equipment editor handle binding later.

## Worked example — `rail_check`

The shipped example lives at
`local_packages/authoring_skills/rail_check/SKILL.md`:

```markdown
---
kind: skill-chat
name: Power rail quick-check
when-to-use: Draft a rough functional test for one power rail (set input, enable, measure output).
version: 0.1.0
---
You help the engineer draft a ROUGH test procedure for ONE power rail. The draft
expresses intent in plain, operator-readable steps — NOT final DSL grammar; the
editor's normal pipeline refines it afterward.

Use the attached context (netlist / rules / documents) first. Ask only what you
genuinely can't infer, and keep it to a couple of questions at a time.

For the chosen rail, draft steps covering:
1. **Wiring** — which supply connects where (use net / test-point names from the
   netlist when available).
2. **Enable** — if the rail has an enable line, assert it; otherwise note it is
   always-on.
3. **Set** the input voltage.
4. **Measure** the output rail voltage at its test point.
5. **Pass/fail** — output within nominal ± tolerance (ask for nominal and
   tolerance if they aren't given).

Output ONE coherent block of plain steps, one action per line, naming the rail
and its test point. Leave instrument binding to the equipment editor — where no
mapping exists, write an operator "probe / read / judge" step.
```

Note the pattern: a named role, a "use the context first" instruction, an
explicit checklist of what each draft must cover, and an output-shape rule.

## Context tabs

The chat dialog has a three-tab context picker. Each item is checkable and shows
a live size readout, so you can see how much you are sending.

| Tab | What it offers |
|---|---|
| **Rules** | The same rule docs the text tab sends. |
| **Documents** | Files in `<project>/documents/`. Drop reference docs (datasheets, spec extracts, notes) there to make them selectable. |
| **Artifacts** | **Procedure text**, **Procedure JSON**, **Test code**, and **Netlist** (the ODB++ board rendered to text). |

Only the items you check are sent with your message.

## Locations & precedence

Skills are discovered by scanning for subfolders that contain a `SKILL.md`,
across three tiers:

| Tier | Path | Scope |
|---|---|---|
| **Local drop-in** | `<repo>/local_packages/authoring_skills/<skill>/SKILL.md` | Hand-authored; available in every project on this install. This is the existing `local_packages` drop-in (same place local pack sources go), under an identifying `authoring_skills/` subfolder. **Skills → Open skills folder…** opens this. |
| **Project drop-in** | `<project>/authoring_skills/<skill>/` | One project only. |
| **Bundled** | `<project>/bundle/authoring_skills/<skill>/` | Ships with a pack, versioned. Populated by the bundle build — not by hand. |

When two skills share the same folder name, **project > local > bundled**.

## Bundling for distribution

A skill is plain files, so "bundling" is a folder copy — not a wheel. A pack
ships an `authoring_skills/` subdir; when a bundle is built, each skill folder is
copied into `<project>/bundle/authoring_skills/`. The mechanism is
`project_services.bundle_generator.copy_pack_skills` /
`bundle_skills_for_registry`, invoked by the Bundle Generator.

Hand drop-in (the local and project tiers) needs no build at all — just place the
folder.

## Caveats

- **Skill vs. feature.** Dropping a skill folder is enough for the *running*
  editor to discover it: the Skills menu re-scans each time it opens, so no
  restart is needed for the skill itself. But the skill-chat **feature** (the
  editor code) must be present in the running build. If you run the editor from a
  bundled or installed copy, getting the feature requires a rebundle/reinstall of
  the editor.
- **Web search is a placeholder.** The 🌐 **Web search** toggle in the chat
  dialog is cosmetic and disabled — not wired yet.
- **Custom tools are planned, not yet available.** A future skill may carry a
  `tools.py` exposing custom Python tools to the model. This will be gated behind
  a trust step (curated / trusted skills only) and is not usable today.
