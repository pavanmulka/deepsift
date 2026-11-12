"""Every tool carries a title and the four hints AI apps and directories read."""
import asyncio

from deepsift.mcp_server import build_server

TITLES = {
    "web_search": "Web Search",
    "web_read": "Read Web Pages",
    "web_map": "Map a Website",
    "web_crawl": "Crawl a Website",
    "web_research": "Web Research Report",
    "web_research_result": "Get Research Report",
}


def _tools(**kwargs):
    server = build_server(lambda: None, **kwargs)
    return {t.name: t for t in asyncio.run(server.list_tools())}


def test_every_tool_has_a_title_and_all_four_hints():
    tools = _tools()
    assert {name: t.title for name, t in tools.items()} == TITLES
    for name, tool in tools.items():
        hints = tool.annotations.model_dump(by_alias=True)
        assert hints["title"] == TITLES[name]
        assert hints["readOnlyHint"] is True and hints["destructiveHint"] is False, name
        assert isinstance(hints["idempotentHint"], bool) and isinstance(hints["openWorldHint"], bool), name


def test_the_two_research_tools_are_labelled_differently():
    tools = _tools()
    # Each research call starts a new run; fetching its report never goes out to the web.
    assert tools["web_research"].annotations.idempotent_hint is False
    assert tools["web_research"].annotations.open_world_hint is True
    assert tools["web_research_result"].annotations.idempotent_hint is True
    assert tools["web_research_result"].annotations.open_world_hint is False


def test_hosted_copy_has_no_research_tools():
    assert sorted(_tools(research=False)) == ["web_crawl", "web_map", "web_read", "web_search"]
