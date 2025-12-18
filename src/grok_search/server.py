from fastmcp import FastMCP, Context
from .providers.grok import GrokSearchProvider
from .utils import format_search_results
from .logger import log_info
from .config import config
import asyncio

mcp = FastMCP("grok-search")

@mcp.tool(
    name="web_search",
    description="""
    Performs a third-party web search based on the given query and returns the results
    as a JSON string.

    The `query` should be a clear, self-contained natural-language search query.
    When helpful, include constraints such as topic, time range, language, or domain.

    The `platform` should be the platforms which you should focus on searching, such as "Twitter", "GitHub", "Reddit", etc.

    The `min_results` and `max_results` should be the minimum and maximum number of results to return.

    Returns
    -------
    str
        A JSON-encoded string representing a list of search results. Each result
        includes at least:
        - `url`: the link to the result
        - `title`: a short title
        - `summary`: a brief description or snippet of the page content.
    """
)
async def web_search(query: str, platform: str = "", min_results: int = 3, max_results: int = 10, ctx: Context = None) -> str:
    try:
        api_url = config.grok_api_url
        api_key = config.grok_api_key
    except ValueError as e:
        error_msg = str(e)
        if ctx:
            await ctx.report_progress(error_msg)
        return f"配置错误: {error_msg}"
    
    grok_provider = GrokSearchProvider(api_url, api_key)
    
    await log_info(ctx, f"Begin Search: {query}", config.debug_enabled)
    results = await grok_provider.search(query, platform, min_results, max_results, ctx)
    await log_info(ctx, "Search Finished!", config.debug_enabled)
    return results


@mcp.tool(
    name="web_fetch",
    description="""
    Fetches and extracts the complete content from a specified URL and returns it
    as a structured Markdown document.
    The `url` should be a valid HTTP/HTTPS web address pointing to the target page.
    Ensure the URL is complete and accessible (not behind authentication or paywalls).
    The function will:
    - Retrieve the full HTML content from the URL
    - Parse and extract all meaningful content (text, images, links, tables, code blocks)
    - Convert the HTML structure to well-formatted Markdown
    - Preserve the original content hierarchy and formatting
    - Remove scripts, styles, and other non-content elements
    Returns
    -------
    str
        A Markdown-formatted string containing:
        - Metadata header (source URL, title, fetch timestamp)
        - Table of Contents (if applicable)
        - Complete page content with preserved structure
        - All text, links, images, tables, and code blocks from the original page
        
        The output maintains 100% content fidelity with the source page and is
        ready for documentation, analysis, or further processing.
    Notes
    -----
    - Does NOT summarize or modify content - returns complete original text
    - Handles special characters, encoding (UTF-8), and nested structures
    - May not capture dynamically loaded content requiring JavaScript execution
    - Respects the original language without translation
    """
)
async def web_fetch(url: str, ctx: Context = None) -> str:
    try:
        api_url = config.grok_api_url
        api_key = config.grok_api_key
    except ValueError as e:
        error_msg = str(e)
        if ctx:
            await ctx.report_progress(error_msg)
        return f"配置错误: {error_msg}"
    await log_info(ctx, f"Begin Fetch: {url}", config.debug_enabled)
    grok_provider = GrokSearchProvider(api_url, api_key)
    results = await grok_provider.fetch(url, ctx)
    await log_info(ctx, "Fetch Finished!", config.debug_enabled)
    return results



def main():
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
