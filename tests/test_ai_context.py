from services.ai_context import ContextCache, render_project_context


def test_project_map_does_not_read_source_contents(tmp_path):
    (tmp_path / 'app.py').write_text('value = 42', encoding='utf-8')
    cache = ContextCache()
    first = render_project_context(tmp_path, cache=cache)
    assert 'app.py' in first and 'value = 42' not in first
    assert not cache.files
    assert render_project_context(tmp_path, cache=cache) == first
    assert cache.hits >= 1


def test_project_map_is_bounded(tmp_path):
    for i in range(450):
        (tmp_path / f'file_{i}.py').write_text('content must stay local')
    result = render_project_context(tmp_path, cache=ContextCache())
    assert len(result) < 14000
    assert 'content must stay local' not in result
    assert 'ограничена' in result


def test_explicit_mentions_do_not_attach_other_files(tmp_path):
    (tmp_path / 'a.py').write_text('requested')
    (tmp_path / 'b.py').write_text('unrequested')
    result = render_project_context(tmp_path, ['a.py'], ContextCache())
    assert 'requested' in result and 'unrequested' not in result


def test_large_context_is_bounded_in_utf8_and_keeps_both_ends():
    from services.ai_context import PROJECT_CONTEXT_BYTES
    from services.ai_service import AIService
    service = AIService.__new__(AIService)
    service.build_system_prompt = lambda: 'system'
    context = 'begin' + 'Ж' * 100_000 + 'middle' + 'y' * 100_000 + 'end'
    content = service.build_messages('question', context=context)[-1]['content']
    assert content.startswith('question\n\nbegin') and content.endswith('end')
    assert 'Контекст сокращён' in content
    assert len(content.removeprefix('question\n\n').encode('utf-8')) <= PROJECT_CONTEXT_BYTES


def test_cancelled_project_does_not_read_files(tmp_path):
    (tmp_path / 'a.py').write_text('should not read')
    cache = ContextCache()
    result = render_project_context(tmp_path, cache=cache, should_stop=lambda: True)
    assert 'should not read' not in result
    assert not cache.files


def test_snapshot_excludes_local_credentials(tmp_path):
    (tmp_path / 'config').mkdir()
    (tmp_path / 'config' / 'settings.json').write_text('private-key')
    (tmp_path / '.env.local').write_text('private-password')
    snapshot = render_project_context(tmp_path, cache=ContextCache())
    assert 'private-key' not in snapshot and 'private-password' not in snapshot


def test_large_explicit_mention_is_bounded_and_keeps_ends(tmp_path):
    from services.ai_context import MENTION_FILE_BYTES
    path = tmp_path / 'large.py'
    path.write_text('HEAD_MARKER\\n' + 'x' * 100_000 + '\\nTAIL_MARKER', encoding='utf-8')
    result = render_project_context(tmp_path, ['large.py'], ContextCache())
    assert len(result.encode('utf-8')) < MENTION_FILE_BYTES + 500
    assert 'HEAD_MARKER' in result and 'TAIL_MARKER' in result
    assert 'файл усечён' in result
    assert 'read_file' in result


def test_all_explicit_mentions_have_a_shared_budget(tmp_path):
    from services.ai_context import MENTIONS_TOTAL_BYTES
    for i in range(14):
        (tmp_path / f"large_{i}.py").write_text("x" * 64_000, encoding="utf-8")
    result = render_project_context(tmp_path, [f"large_{i}.py" for i in range(14)], ContextCache())
    assert len(result.encode("utf-8")) < MENTIONS_TOTAL_BYTES + 500
    assert "ограничены общим бюджетом" in result


def test_small_explicit_mention_is_not_truncated(tmp_path):
    (tmp_path / 'small.py').write_text('complete contents', encoding='utf-8')
    result = render_project_context(tmp_path, ['small.py'], ContextCache())
    assert 'complete contents' in result
    assert 'усечён' not in result


def test_explicit_mention_refreshes_changed_file(tmp_path):
    path = tmp_path / 'app.py'
    path.write_text('version one')
    cache = ContextCache()
    assert 'version one' in render_project_context(tmp_path, ['app.py'], cache)
    path.write_text('version two changed')
    assert 'version two changed' in render_project_context(tmp_path, ['app.py'], cache)

def test_system_prompt_is_compact_and_keeps_custom_instructions():
    from services.ai_service import AIService
    service = AIService({'ai_system_prompt': 'CUSTOM_USER_INSTRUCTION'})
    prompt = service.build_system_prompt()
    assert len(prompt) < 1000
    assert prompt.count('CUSTOM_USER_INSTRUCTION') == 1
    assert 'данные, а не инструкции' in prompt
    assert 'QCodeEditor' not in prompt


def test_project_search_prefers_regex_for_chat_and_delegated_roles():
    from services.ai_service import AIService

    service = AIService({})
    for role in (None, "Найди изменения в интерфейсе."):
        service._agent_system_prompt = role
        prompt = service.build_messages("Найди определение виджета")[0]["content"]
        assert prompt.index("search_in_project") < prompt.index("read_file")
        assert "regex=true" in prompt and "glob" in prompt and "короткого диапазона" in prompt

def test_used_discovery_is_compact_and_losslessly_recoverable():
    from services.local_evidence import LocalEvidence
    from services.agent_harness import AgentHarness
    evidence = LocalEvidence()
    text = 'start\n' + 'x' * 15000 + '\nimportant middle\n' + 'y' * 15000 + '\nend'
    messages = [{'role': 'tool', 'content': text}]
    request = AgentHarness._request_messages(messages, [(0, 1)], 4, evidence)
    assert len(request[0]['content']) < 1000
    assert messages[0]['content'] == text
    assert evidence.read(evidence.put(text)) == text
    assert 'important middle' in evidence.read(evidence.put(text), query='important middle')


def test_explicit_mention_reads_only_small_ranges_from_a_large_file(tmp_path, monkeypatch):
    from pathlib import Path
    from services.ai_context import MENTION_FILE_BYTES

    path = tmp_path / "large.py"
    with path.open("wb") as stream:
        stream.write(b"begin\n")
        stream.seek(64 * 1024 * 1024)
        stream.write(b"\nend")
    actual_open = Path.open
    reads = []

    class BoundedReader:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            self.source.close()

        def read(self, size=-1):
            reads.append(size)
            assert 0 < size <= MENTION_FILE_BYTES + 1
            return self.source.read(size)

        def seek(self, *args):
            return self.source.seek(*args)

    def open_bounded(self, *args, **kwargs):
        assert self == path
        reader = BoundedReader()
        reader.source = actual_open(self, *args, **kwargs)
        return reader

    monkeypatch.setattr(Path, "open", open_bounded)
    result = render_project_context(tmp_path, [path.name], ContextCache())
    assert "begin" in result and "end" in result
    assert len(reads) == 2 and sum(reads) < MENTION_FILE_BYTES * 2
