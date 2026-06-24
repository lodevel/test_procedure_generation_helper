"""Tests for the skill-chat context layer (sources / assembler / netlist text).

Pure-Python: tmp_path + fakes only — no Qt, no managers, no ODB CLI.
"""
from workflow_editor.authoring import (
    ArtifactProvider,
    ArtifactsSource,
    ContextBundle,
    DocumentsSource,
    RulesSource,
    assemble,
    format_netlist,
)


def test_context_bundle_approx_tokens():
    assert ContextBundle("", 0).approx_tokens == 0
    assert ContextBundle("x" * 8, 8).approx_tokens == 2     # 8//4
    assert ContextBundle("x" * 10, 10).approx_tokens == 3   # ceil-ish (10+3)//4

# --------------------------------------------------------------------------- #
# format_netlist                                                              #
# --------------------------------------------------------------------------- #

_BOARD = {
    "components": [
        {"refdes": "U1", "side": "top", "pins": [
            {"name": "1", "net": "VIN"},
            {"name": "4", "net": "EN"},
            {"name": "5", "net": "VOUT"},
        ]},
        {"refdes": "TP4", "side": "top", "pins": [{"name": "1", "net": "VOUT"}]},
    ],
    "nets": [
        {"net": "VOUT", "nodes": [{"refdes": "U1", "pin": "5"}, {"refdes": "TP4", "pin": "1"}]},
    ],
    "error": "",
}


def test_format_netlist_full():
    text = format_netlist(_BOARD)
    assert "Components (2):" in text
    assert "- U1 [top]: 1=VIN, 4=EN, 5=VOUT" in text
    assert "Nets (1):" in text
    assert "- VOUT: U1.5, TP4.1" in text


def test_format_netlist_empty_with_error():
    text = format_netlist({"components": [], "nets": [], "error": "no tgz"})
    assert "no netlist available" in text and "no tgz" in text


def test_format_netlist_empty_no_error():
    assert format_netlist({"components": [], "nets": []}) == "(no netlist available)"


def test_format_netlist_pin_without_net_and_component_without_pins():
    board = {"components": [
        {"refdes": "R1", "pins": [{"name": "1"}]},   # pin has no net
        {"refdes": "MTG1", "pins": []},              # no pins at all
    ], "nets": []}
    text = format_netlist(board)
    assert "- R1: 1=-" in text          # missing net rendered as "-"
    assert "- MTG1" in text             # no-pin component still listed


def test_format_netlist_node_missing_pin_does_not_leak_none():
    board = {"components": [], "nets": [
        {"net": "GND", "nodes": [{"refdes": "R1", "pin": "0"}, {"refdes": "R2"}]},
    ]}
    text = format_netlist(board)
    assert "R1.0" in text               # pin "0" preserved (not falsy-dropped)
    assert "R2.?" in text               # missing pin → "?", never "None"
    assert ".None" not in text


def test_format_netlist_ignores_non_dict_entries():
    board = {
        "components": [None, "junk", {"refdes": "U1", "pins": [None, {"name": "1", "net": "V"}]}],
        "nets": ["junk", {"net": "V", "nodes": [None, {"refdes": "U1", "pin": "1"}]}],
    }
    text = format_netlist(board)
    assert "- U1: 1=V" in text          # junk pins/components/nodes dropped silently
    assert "- V: U1.1" in text
    assert "junk" not in text and "None" not in text


# --------------------------------------------------------------------------- #
# DocumentsSource                                                             #
# --------------------------------------------------------------------------- #

def test_documents_source_lists_sorted_skips_hidden(tmp_path):
    (tmp_path / "b.md").write_text("B", encoding="utf-8")
    (tmp_path / "a.txt").write_text("A", encoding="utf-8")
    (tmp_path / ".hidden").write_text("nope", encoding="utf-8")
    (tmp_path / "sub").mkdir()
    src = DocumentsSource(tmp_path)
    assert [i.key for i in src.list_items()] == ["a.txt", "b.md"]


def test_documents_source_materialize_selected_only(tmp_path):
    (tmp_path / "a.txt").write_text("alpha", encoding="utf-8")
    (tmp_path / "b.txt").write_text("beta", encoding="utf-8")
    out = DocumentsSource(tmp_path).materialize(["a.txt"])
    assert "## a.txt" in out and "alpha" in out
    assert "beta" not in out


def test_documents_source_skips_binary(tmp_path):
    (tmp_path / "bin.dat").write_bytes(b"\xff\xfe\x00\x01ELF")
    out = DocumentsSource(tmp_path).materialize(["bin.dat"])
    assert out == ""


def test_documents_source_missing_dir_is_empty(tmp_path):
    src = DocumentsSource(tmp_path / "nope")
    assert src.list_items() == []
    assert src.materialize(["x"]) == ""


# --------------------------------------------------------------------------- #
# RulesSource                                                                 #
# --------------------------------------------------------------------------- #

def test_rules_source_lists_and_applies_transform(tmp_path):
    r1 = tmp_path / "r1.md"
    r1.write_text("rule one body", encoding="utf-8")
    r2 = tmp_path / "r2.md"
    r2.write_text("rule two body", encoding="utf-8")
    src = RulesSource(lambda: [r2, r1], transform=str.upper)
    assert [i.key for i in src.list_items()] == ["r1.md", "r2.md"]   # sorted
    out = src.materialize(["r1.md"])
    assert "## r1.md" in out and "RULE ONE BODY" in out
    assert "r2" not in out.lower()


def test_rules_source_ignores_unknown_key(tmp_path):
    r1 = tmp_path / "r1.md"
    r1.write_text("body", encoding="utf-8")
    src = RulesSource(lambda: [r1])
    assert src.materialize(["ghost.md"]) == ""


# --------------------------------------------------------------------------- #
# ArtifactsSource                                                             #
# --------------------------------------------------------------------------- #

def _providers():
    return [
        ArtifactProvider("text", "Procedure text", lambda: "the procedure"),
        ArtifactProvider("empty", "Empty", lambda: "   "),
        ArtifactProvider("boom", "Boom", lambda: (_ for _ in ()).throw(RuntimeError("x"))),
    ]


def test_artifacts_source_materialize_and_resilience():
    src = ArtifactsSource(_providers())
    assert {i.key for i in src.list_items()} == {"text", "empty", "boom"}
    # selected: a good one, an empty one (skipped), a throwing one (skipped).
    out = src.materialize(["text", "empty", "boom"])
    assert "## Procedure text" in out and "the procedure" in out
    assert "Empty" not in out and "Boom" not in out


# --------------------------------------------------------------------------- #
# assemble                                                                    #
# --------------------------------------------------------------------------- #

def test_assemble_concatenates_with_headers_and_counts(tmp_path):
    (tmp_path / "a.txt").write_text("alpha", encoding="utf-8")
    docs = DocumentsSource(tmp_path)
    arts = ArtifactsSource([ArtifactProvider("text", "Procedure text", lambda: "proc")])
    bundle = assemble([(docs, ["a.txt"]), (arts, ["text"])])
    assert "# Documents" in bundle.text and "## a.txt" in bundle.text
    assert "# Artifacts" in bundle.text and "proc" in bundle.text
    assert bundle.char_count == len(bundle.text)


def test_assemble_skips_empty_selection_and_content(tmp_path):
    docs = DocumentsSource(tmp_path)            # empty dir
    arts = ArtifactsSource([ArtifactProvider("text", "T", lambda: "x")])
    bundle = assemble([(docs, []), (docs, ["nothing"]), (arts, ["text"])])
    assert bundle.text == "# Artifacts\n\n## T\n\nx"


def test_assemble_empty_when_nothing_selected():
    arts = ArtifactsSource([ArtifactProvider("text", "T", lambda: "x")])
    bundle = assemble([(arts, [])])
    assert bundle.text == "" and bundle.char_count == 0
