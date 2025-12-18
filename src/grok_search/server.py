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


@mcp.tool(
    name="get_config_info",
    description="""
    Returns the current Grok Search MCP server configuration information and tests the connection.

    This tool is useful for:
    - Verifying that environment variables are correctly configured
    - Testing API connectivity by sending a request to /models endpoint
    - Debugging configuration issues
    - Checking the current API endpoint and settings

    Returns
    -------
    str
        A JSON-encoded string containing configuration details:
        - `api_url`: The configured Grok API endpoint
        - `api_key`: The API key (masked for security, showing only first and last 4 characters)
        - `debug_enabled`: Whether debug mode is enabled
        - `log_level`: Current logging level
        - `log_dir`: Directory where logs are stored
        - `config_status`: Overall configuration status (✅ complete or ❌ error)
        - `connection_test`: Result of testing API connectivity to /models endpoint

    Notes
    -----
    - API keys are automatically masked for security
    - This tool does not require any parameters
    - Useful for troubleshooting before making actual search requests
    - Automatically tests API connectivity during execution
    """
)
async def get_config_info() -> str:
    import json
    import httpx

    config_info = config.get_config_info()

    # 添加连接测试
    test_result = {
        "status": "未测试",
        "message": "",
        "response_time_ms": 0
    }

    try:
        api_url = config.grok_api_url
        api_key = config.grok_api_key

        # 构建 /models 端点 URL
        models_url = f"{api_url.rstrip('/')}/models"

        # 发送测试请求
        import time
        start_time = time.time()

        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(
                models_url,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json"
                }
            )

            response_time = (time.time() - start_time) * 1000  # 转换为毫秒

            if response.status_code == 200:
                test_result["status"] = "✅ 连接成功"
                test_result["message"] = f"成功获取模型列表 (HTTP {response.status_code})"
                test_result["response_time_ms"] = round(response_time, 2)

                # 尝试解析返回的模型列表
                try:
                    models_data = response.json()
                    if "data" in models_data and isinstance(models_data["data"], list):
                        model_count = len(models_data["data"])
                        test_result["message"] += f"，共 {model_count} 个模型"
                except:
                    pass
            else:
                test_result["status"] = "⚠️ 连接异常"
                test_result["message"] = f"HTTP {response.status_code}: {response.text[:100]}"
                test_result["response_time_ms"] = round(response_time, 2)

    except httpx.TimeoutException:
        test_result["status"] = "❌ 连接超时"
        test_result["message"] = "请求超时（10秒），请检查网络连接或 API URL"
    except httpx.RequestError as e:
        test_result["status"] = "❌ 连接失败"
        test_result["message"] = f"网络错误: {str(e)}"
    except ValueError as e:
        test_result["status"] = "❌ 配置错误"
        test_result["message"] = str(e)
    except Exception as e:
        test_result["status"] = "❌ 测试失败"
        test_result["message"] = f"未知错误: {str(e)}"

    config_info["connection_test"] = test_result

    return json.dumps(config_info, ensure_ascii=False, indent=2)


def main():
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
