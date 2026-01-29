from __future__ import annotations

from src.context_compression import (
    build_anthropic_transcript,
    build_checkpoint_contents,
    parse_gemini_text_response,
    select_tail_contents,
)


def test_select_tail_contents_shifts_to_include_tool_call_pair():
    contents = [
        {
            "role": "model",
            "parts": [{"text": "hello"}],
        },
        {
            "role": "model",
            "parts": [{"functionCall": {"id": "call_1", "name": "t", "args": {}}}],
        },
        {
            "role": "user",
            "parts": [
                {
                    "functionResponse": {
                        "id": "call_1",
                        "name": "t",
                        "response": {"output": "ok"},
                    }
                }
            ],
        },
    ]

    # keep_last_messages=1 would start on the functionResponse; we should include the preceding call.
    tail = select_tail_contents(contents, keep_last_messages=1)
    assert len(tail) == 2
    assert "functionCall" in (tail[0]["parts"][0])
    assert "functionResponse" in (tail[1]["parts"][0])


def test_build_checkpoint_contents_has_prefix_and_keeps_tail():
    tail = [{"role": "user", "parts": [{"text": "latest"}]}]
    out = build_checkpoint_contents("<state_snapshot><goal>x</goal></state_snapshot>", tail)
    assert out[0]["role"] == "user"
    assert "Context checkpoint" in out[0]["parts"][0]["text"]
    assert out[-1]["parts"][0]["text"] == "latest"


def test_build_anthropic_transcript_compacts_tool_results():
    messages = [
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "call_1", "name": "webfetch", "input": {"url": "x"}},
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "call_1",
                    "name": "webfetch",
                    "content": [{"type": "text", "text": "a" * 50000}],
                },
            ],
        },
    ]

    transcript = build_anthropic_transcript(
        system="sys",
        messages=messages,
        tool_result_max_chars_for_summary=1000,
    )
    assert "[TOOL_RESULT" in transcript
    assert "...[truncated" in transcript


def test_parse_gemini_text_response_handles_wrapped_response():
    data = {
        "response": {
            "candidates": [
                {"content": {"parts": [{"text": "hello"}, {"text": " world"}]}}
            ]
        }
    }
    assert parse_gemini_text_response(data) == "hello world"
