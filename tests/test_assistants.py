from voice.assistants import pilot_assistant, unit_assistant
from voice.config import Settings


def test_function_tools_override_default_filler():
    s = Settings(public_base_url="https://example.invalid")
    for cfg in (pilot_assistant(s), unit_assistant(s)):
        for tool in cfg["model"]["tools"]:
            if tool["type"] != "function":
                continue
            starts = [m for m in tool["messages"] if m["type"] == "request-start"]
            assert starts and all(m["content"] for m in starts), tool["function"]["name"]
    report = next(t for t in pilot_assistant(s)["model"]["tools"] if t.get("function", {}).get("name") == "report_emergency")
    assert report["messages"][0]["content"] == "Copy."


def test_assistants_subscribe_to_plain_transcripts():
    s = Settings(public_base_url="https://example.invalid")
    for cfg in (pilot_assistant(s), unit_assistant(s)):
        assert "transcript" in cfg["serverMessages"]
        assert cfg["transcriber"]["keyterm"][0] == "SkyWatch"
