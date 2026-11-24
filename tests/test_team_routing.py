from services.team_routing import verification_only
from services.agent_team import run_team
from services.agent_tools import LocalToolbox
from test_three_stage_team import Script, finish


def test_small_verification_has_one_model_actor_without_mutations(tmp_path):
    (tmp_path / "a.py").write_text("x=1")

    def verify(route, tools):
        assert "write_file" not in [t["function"]["name"] for t in tools]
        route("read_file", {"path": "a.py"})
        route("finish_stage", finish("pass"))

    r = run_team(
        Script([verify]),
        LocalToolbox(str(tmp_path)),
        "Проведи небольшой тест",
        lambda *_: None,
        lambda: False,
    )
    assert r.complete and len(r.optimization["stages"]) == 1


def test_defect_found_during_check_does_not_authorize_implementation(tmp_path):
    def verify(route, _):
        route("finish_stage", finish("needs_changes", "found bug"))

    r = run_team(
        Script([verify]),
        LocalToolbox(str(tmp_path)),
        "Повтори проверку",
        lambda *_: None,
        lambda: False,
    )
    assert not r.complete and len(r.optimization["stages"]) == 1 and not r.changed_files


def test_mutation_and_explicit_team_keep_three_stages():
    assert not verification_only("Проверь и исправь ошибки")
    assert not verification_only("Проверь трёхэтапной командой")
    assert verification_only("Проверь; не исправляй код")


def test_archived_edits_keep_exact_payload_and_valid_message_groups():
    import json
    from services.agent_harness import AgentHarness
    from services.local_evidence import LocalEvidence

    e = LocalEvidence()
    messages = []
    for i in range(4):
        messages.extend(
            [
                {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "id": str(i),
                            "function": {
                                "name": "edit_file",
                                "arguments": json.dumps(
                                    {"path": "a.py", "old_text": "x" * 3000, "new_text": "y" * 3000}
                                ),
                            },
                        }
                    ],
                },
                {"role": "tool", "tool_call_id": str(i), "content": "Изменён файл a.py."},
            ]
        )
    compact = AgentHarness._archive_completed_edits(messages, e)
    assert len(str(compact)) < len(str(messages)) * 0.6
    assert len(messages) == 8 and len(e.values) == 2
    ids = {c["id"] for m in compact for c in m.get("tool_calls", [])}
    assert all(m["tool_call_id"] in ids for m in compact if m["role"] == "tool")
    assert all("old_text" in text for text in e.values.values())
