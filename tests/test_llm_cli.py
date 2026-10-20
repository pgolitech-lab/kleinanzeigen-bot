# Тесты обёртки CLI (modules/llm.py): лимит подписки → LLMLimitError с временем сброса.
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from modules import llm


def _proc(result: dict, rc: int = 1, stderr: str = ""):
    return SimpleNamespace(stdout=json.dumps({"type": "result", **result}) + "\n", stderr=stderr, returncode=rc)


def _run():
    return llm.run(model="claude-haiku-4-5", messages=[{"role": "user", "content": "hi"}])


def test_limit_error_with_reset_time():
    out = _proc({"is_error": True, "subtype": "success", "api_error_status": 429,
                 "result": "Claude AI usage limit reached|1760990400"})
    with patch("subprocess.run", return_value=out):
        with pytest.raises(llm.LLMLimitError) as ei:
            _run()
    assert ei.value.reset_at is not None
    assert "LLM недоступен до" in str(ei.value)


def test_limit_error_from_stderr_without_result():
    out = SimpleNamespace(stdout="", stderr="Error: 5-hour limit reached ∙ resets 3pm", returncode=1)
    with patch("subprocess.run", return_value=out):
        with pytest.raises(llm.LLMLimitError) as ei:
            _run()
    assert ei.value.reset_at.hour == 15


def test_other_errors_stay_llm_error():
    out = _proc({"is_error": True, "subtype": "error_during_execution", "result": "boom"})
    with patch("subprocess.run", return_value=out):
        with pytest.raises(llm.LLMError) as ei:
            _run()
    assert not isinstance(ei.value, llm.LLMLimitError)
