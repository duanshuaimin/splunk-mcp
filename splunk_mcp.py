# Import packages
import json
import logging
import os
import ssl
import traceback
from datetime import datetime
from typing import Dict, List, Any, Optional, Union

import splunklib.client
import yaml
from mcp.server.fastmcp import FastMCP
from splunklib import results
import sys
import socket
from fastapi import FastAPI, APIRouter, Request
from fastapi.openapi.docs import get_swagger_ui_html, get_redoc_html
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse
from mcp.server.sse import SseServerTransport
from starlette.routing import Mount
import uvicorn
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("splunk_mcp.log")
    ]
)
logger = logging.getLogger(__name__)

# Global variable to hold server configurations
SPLUNK_SERVERS: List[Dict[str, Any]] = []

def load_config():
    """Loads server configurations from config.yaml."""
    global SPLUNK_SERVERS
    try:
        with open("config.yaml", "r") as f:
            config_data = yaml.safe_load(f)
            SPLUNK_SERVERS = config_data.get("servers", [])
            if not SPLUNK_SERVERS:
                logger.warning("⚠️ No Splunk servers configured in config.yaml. Most tools will not work.")
            else:
                logger.info(f"✅ Loaded {len(SPLUNK_SERVERS)} Splunk server configurations.")
    except FileNotFoundError:
        logger.error("❌ config.yaml not found. Please create it based on the example.")
        SPLUNK_SERVERS = []
    except Exception as e:
        logger.error(f"❌ Error loading config.yaml: {e}")
        SPLUNK_SERVERS = []

# Load configuration at startup
load_config()

# Environment variables
FASTMCP_PORT = int(os.environ.get("FASTMCP_PORT", "8000"))
os.environ["FASTMCP_PORT"] = str(FASTMCP_PORT)
VERSION = "0.3.1" # Updated version

# Create FastAPI application with metadata
app = FastAPI(
    title="Splunk MCP API",
    description="A FastMCP-based tool for interacting with Splunk Enterprise/Cloud through natural language",
    version=VERSION,
)

# Initialize the MCP server
mcp = FastMCP(
    "splunk",
    description="A FastMCP-based tool for interacting with Splunk Enterprise/Cloud through natural language",
    version=VERSION,
    host="0.0.0.0",  # Listen on all interfaces
    port=FASTMCP_PORT
)

# Create SSE transport instance for handling server-sent events
sse = SseServerTransport("/messages/")

# Mount the /messages path to handle SSE message posting
app.router.routes.append(Mount("/messages", app=sse.handle_post_message))

# Add documentation for the /messages endpoint
@app.get("/messages", tags=["MCP"], include_in_schema=True)
def messages_docs():
    """
    Messages endpoint for SSE communication

    This endpoint is used for posting messages to SSE clients.
    Note: This route is for documentation purposes only.
    The actual implementation is handled by the SSE transport.
    """
    pass

@app.get("/sse", tags=["MCP"])
async def handle_sse(request: Request):
    """
    SSE endpoint that connects to the MCP server

    This endpoint establishes a Server-Sent Events connection with the client
    and forwards communication to the Model Context Protocol server.
    """
    async with sse.connect_sse(request.scope, request.receive, request._send) as (
        read_stream,
        write_stream,
    ):
        await mcp._mcp_server.run(
            read_stream,
            write_stream,
            mcp._mcp_server.create_initialization_options(),
        )

@app.get("/docs", include_in_schema=False)
async def custom_swagger_ui_html():
    return get_swagger_ui_html(
        openapi_url="/openapi.json",
        title=f"{mcp.name} - Swagger UI"
    )

@app.get("/redoc", include_in_schema=False)
async def redoc_html():
    return get_redoc_html(
        openapi_url="/openapi.json",
        title=f"{mcp.name} - ReDoc"
    )

def get_splunk_connection(server_name: Optional[str] = None) -> splunklib.client.Service:
    """
    Get a connection to a specific Splunk service from the configuration.
    If server_name is not provided, it connects to the first server in the list.
    
    Args:
        server_name: The name of the server to connect to, as defined in config.yaml.
        
    Returns:
        splunklib.client.Service: Connected Splunk service
        
    Raises:
        ConnectionError: If no servers are configured or authentication details are missing.
        ValueError: If the specified server_name is not found.
    """
    if not SPLUNK_SERVERS:
        raise ConnectionError("No Splunk servers are configured. Please check your config.yaml file.")

    server_config = None
    if server_name:
        server_config = next((s for s in SPLUNK_SERVERS if s.get("name") == server_name), None)
        if not server_config:
            raise ValueError(f"Splunk server '{server_name}' not found in config.yaml.")
    else:
        server_config = SPLUNK_SERVERS[0]
        logger.debug(f"No server name specified, connecting to the first server: {server_config.get('name')}")

    name = server_config.get("name", "Unknown")
    host = server_config.get("host", "localhost")
    port = server_config.get("port", 8089)
    scheme = server_config.get("scheme", "https")
    verify = server_config.get("verify", False)
    token = server_config.get("token")
    username = server_config.get("username")
    password = server_config.get("password")

    try:
        if token:
            logger.debug(f"🔌 Connecting to Splunk '{name}' at {scheme}://{host}:{port} using token authentication")
            service = splunklib.client.connect(
                host=host,
                port=port,
                scheme=scheme,
                verify=verify,
                token=f"Bearer {token}"
            )
        elif username and password:
            logger.debug(f"🔌 Connecting to Splunk '{name}' at {scheme}://{host}:{port} as {username}")
            service = splunklib.client.connect(
                host=host,
                port=port,
                username=username,
                password=password,
                scheme=scheme,
                verify=verify
            )
        else:
            raise ConnectionError(f"Authentication details (token or username/password) missing for server '{name}'.")
        
        logger.debug(f"✅ Connected to Splunk '{name}' successfully")
        return service
    except Exception as e:
        logger.error(f"❌ Failed to connect to Splunk '{name}': {str(e)}")
        raise

@mcp.tool()
async def list_splunk_servers() -> List[str]:
    """
    List all configured Splunk servers.
    
    Returns:
        List of Splunk server names.
    """
    if not SPLUNK_SERVERS:
        return []
    return [server.get("name", "unnamed_server") for server in SPLUNK_SERVERS]

@mcp.tool()
async def search_splunk(search_query: str, server_name: Optional[str] = None, earliest_time: str = "-24h", latest_time: str = "now", max_results: int = 100) -> List[Dict[str, Any]]:
    """
    Execute a Splunk search query and return the results.
    
    Args:
        search_query: The search query to execute
        server_name: The name of the Splunk server to use (from config.yaml). Defaults to the first server.
        earliest_time: Start time for the search (default: 24 hours ago)
        latest_time: End time for the search (default: now)
        max_results: Maximum number of results to return (default: 100)
        
    Returns:
        List of search results
    """
    if not search_query:
        raise ValueError("Search query cannot be empty")
    
    stripped_query = search_query.lstrip()
    if not (stripped_query.startswith('|') or stripped_query.lower().startswith('search')):
        search_query = f"search {search_query}"
    
    try:
        service = get_splunk_connection(server_name)
        logger.info(f"🔍 Executing search on server '{server_name}': {search_query}")
        
        kwargs_search = {"earliest_time": earliest_time, "latest_time": latest_time, "preview": False, "exec_mode": "blocking"}
        job = service.jobs.create(search_query, **kwargs_search)
        
        result_stream = job.results(output_mode='json', count=max_results)
        results_data = json.loads(result_stream.read().decode('utf-8'))
        
        return results_data.get("results", [])
        
    except Exception as e:
        logger.error(f"❌ Search failed: {str(e)}")
        raise

@mcp.tool()
async def list_indexes(server_name: Optional[str] = None) -> Dict[str, List[str]]:
    """
    Get a list of all available Splunk indexes on a specific server.
    
    Args:
        server_name: The name of the Splunk server to use. Defaults to the first server.
        
    Returns:
        Dictionary containing list of indexes
    """
    try:
        service = get_splunk_connection(server_name)
        indexes = [index.name for index in service.indexes]
        logger.info(f"📊 Found {len(indexes)} indexes on server '{server_name}'")
        return {"indexes": indexes}
    except Exception as e:
        logger.error(f"❌ Failed to list indexes: {str(e)}")
        raise

@mcp.tool()
async def get_index_info(index_name: str, server_name: Optional[str] = None) -> Dict[str, Any]:
    """
    Get metadata for a specific Splunk index.
    
    Args:
        index_name: Name of the index to get metadata for
        server_name: The name of the Splunk server to use. Defaults to the first server.
        
    Returns:
        Dictionary containing index metadata
    """
    try:
        service = get_splunk_connection(server_name)
        index = service.indexes[index_name]
        
        return {
            "name": index_name,
            "total_event_count": str(index["totalEventCount"]),
            "current_size": str(index["currentDBSizeMB"]),
            "max_size": str(index["maxTotalDataSizeMB"]),
            "min_time": str(index["minTime"]),
            "max_time": str(index["maxTime"])
        }
    except KeyError:
        logger.error(f"❌ Index not found: {index_name}")
        raise ValueError(f"Index not found: {index_name}")
    except Exception as e:
        logger.error(f"❌ Failed to get index info: {str(e)}")
        raise

@mcp.tool()
async def list_saved_searches(server_name: Optional[str] = None) -> List[Dict[str, Any]]:
    """
    List all saved searches in Splunk.
    
    Args:
        server_name: The name of the Splunk server to use. Defaults to the first server.
    
    Returns:
        List of saved searches with their names, descriptions, and search queries
    """
    try:
        service = get_splunk_connection(server_name)
        saved_searches = []
        
        for saved_search in service.saved_searches:
            try:
                saved_searches.append({
                    "name": saved_search.name,
                    "description": saved_search.description or "",
                    "search": saved_search.search
                })
            except Exception as e:
                logger.warning(f"⚠️ Error processing saved search: {str(e)}")
                continue
            
        return saved_searches
        
    except Exception as e:
        logger.error(f"❌ Failed to list saved searches: {str(e)}")
        raise

@mcp.tool()
async def current_user(server_name: Optional[str] = None) -> Dict[str, Any]:
    """
    Get information about the currently authenticated user.
    
    Args:
        server_name: The name of the Splunk server to use. Defaults to the first server.
    
    Returns:
        Dict[str, Any]: Dictionary containing user information
    """
    try:
        service = get_splunk_connection(server_name)
        logger.info(f"👤 Fetching current user information from '{server_name}'...")
        
        current_context_resp = service.get("/services/authentication/current-context", **{"output_mode":"json"}).body.read()
        current_context_obj = json.loads(current_context_resp)
        current_username = current_context_obj["entry"][0]["content"]["username"]
        
        current_user_obj = service.users[current_username]
        
        user_info = {
            "username": current_user_obj.name,
            "real_name": current_user_obj.content.get('realname', "N/A"),
            "email": current_user_obj.content.get('email', "N/A"),
            "roles": current_user_obj.content.get('roles', []),
            "default_app": current_user_obj.content.get('defaultApp', "search"),
            "type": current_user_obj.content.get('type', "user")
        }
        
        logger.info(f"✅ Successfully retrieved current user information: {current_user_obj.name}")
        return user_info
            
    except Exception as e:
        logger.error(f"❌ Error getting current user: {str(e)}")
        raise

@mcp.tool()
async def list_users(server_name: Optional[str] = None) -> List[Dict[str, Any]]:
    """
    List all Splunk users (requires admin privileges).
    
    Args:
        server_name: The name of the Splunk server to use. Defaults to the first server.
    """
    try:
        service = get_splunk_connection(server_name)
        logger.info(f"👥 Fetching Splunk users from '{server_name}'...")
                
        users = []
        for user in service.users:
            try:
                user_info = {
                    "username": user.name,
                    "real_name": user.content.get('realname', "N/A"),
                    "email": user.content.get('email', "N/A"),
                    "roles": user.content.get('roles', []),
                    "default_app": user.content.get('defaultApp', "search"),
                    "type": user.content.get('type', "user")
                }
                users.append(user_info)
            except Exception as e:
                logger.warning(f"⚠️ Error processing user {user.name}: {str(e)}")
                continue
            
        logger.info(f"✅ Found {len(users)} users")
        return users
        
    except Exception as e:
        logger.error(f"❌ Error listing users: {str(e)}")
        raise

@mcp.tool()
async def list_kvstore_collections(server_name: Optional[str] = None) -> List[Dict[str, Any]]:
    """
    List all KV store collections across apps.
    
    Args:
        server_name: The name of the Splunk server to use. Defaults to the first server.
        
    Returns:
        List of KV store collections with metadata.
    """
    try:
        service = get_splunk_connection(server_name)
        logger.info(f"📚 Fetching KV store collections from '{server_name}'...")
        
        collections = []
        for entry in service.kvstore:
            try:
                collections.append({
                    "name": entry['name'],
                    "app": entry['access']['app'],
                    "fields": [f.replace('field.', '') for f in entry['content'] if f.startswith('field.')],
                    "accelerated_fields": [f.replace('accelerated_field.', '') for f in entry['content'] if f.startswith('accelerated_field.')]
                })
            except Exception as e:
                logger.warning(f"⚠️ Error processing collection entry: {str(e)}")
                continue
        
        logger.info(f"✅ Found {len(collections)} KV store collections")
        return collections
            
    except Exception as e:
        logger.error(f"❌ Error listing KV store collections: {str(e)}")
        raise

@mcp.tool()
async def health_check(server_name: Optional[str] = None) -> Dict[str, Any]:
    """
    Get basic Splunk connection information and list available apps.
    
    Args:
        server_name: The name of the Splunk server to check. Defaults to the first server.
    """
    try:
        service = get_splunk_connection(server_name)
        logger.info(f"🏥 Performing health check on '{server_name}'...")
        
        apps = [{"name": app.name, "label": app.label, "version": app.version} for app in service.apps]
        
        server_config = next((s for s in SPLUNK_SERVERS if s.get("name") == server_name), SPLUNK_SERVERS[0])

        response = {
            "status": "healthy",
            "server_name": server_config.get("name"),
            "connection": {
                "host": server_config.get("host"),
                "port": server_config.get("port"),
                "scheme": server_config.get("scheme"),
                "ssl_verify": server_config.get("verify")
            },
            "apps_count": len(apps),
            "apps": apps
        }
        
        logger.info(f"✅ Health check successful for '{server_name}'. Found {len(apps)} apps")
        return response
        
    except Exception as e:
        logger.error(f"❌ Health check failed for '{server_name}': {str(e)}")
        raise

@mcp.tool()
async def get_indexes_and_sourcetypes(server_name: Optional[str] = None) -> Dict[str, Any]:
    """
    Get a list of all indexes and their sourcetypes.
    
    Args:
        server_name: The name of the Splunk server to use. Defaults to the first server.
        
    Returns:
        Dictionary with indexes and their sourcetypes.
    """
    try:
        service = get_splunk_connection(server_name)
        logger.info(f"📊 Fetching indexes and sourcetypes from '{server_name}'...")
        
        indexes = [index.name for index in service.indexes]
        
        search_query = "| tstats count WHERE index=* BY index, sourcetype | stats count BY index, sourcetype | sort - count"
        
        job = service.jobs.create(search_query, exec_mode="blocking")
        result_stream = job.results(output_mode='json')
        results_data = json.loads(result_stream.read().decode('utf-8'))
        
        sourcetypes_by_index = {}
        for result in results_data.get('results', []):
            index = result.get('index', '')
            sourcetype = result.get('sourcetype', '')
            count = result.get('count', '0')
            
            if index not in sourcetypes_by_index:
                sourcetypes_by_index[index] = []
            
            sourcetypes_by_index[index].append({'sourcetype': sourcetype, 'count': count})
        
        response = {
            'indexes': indexes,
            'sourcetypes': sourcetypes_by_index,
        }
        
        logger.info(f"✅ Successfully retrieved indexes and sourcetypes from '{server_name}'")
        return response
        
    except Exception as e:
        logger.error(f"❌ Error getting indexes and sourcetypes: {str(e)}")
        raise

@mcp.tool()
async def list_tools() -> List[Dict[str, Any]]:
    """
    List all available MCP tools.
    
    Returns:
        List of all available tools with their name, description, and parameters.
    """
    try:
        logger.info("🧰 Listing available MCP tools...")
        tools_list = []
        
        for name, tool_info in mcp.registered_tools.items():
            try:
                tool_data = {
                    "name": name,
                    "description": tool_info.get("description", "No description available"),
                    "parameters": tool_info.get("parameters", {})
                }
                tools_list.append(tool_data)
            except Exception as e:
                logger.warning(f"⚠️ Error processing tool {name}: {str(e)}")
                continue
        
        tools_list.sort(key=lambda x: x["name"])
        logger.info(f"✅ Found {len(tools_list)} tools")
        return tools_list
        
    except Exception as e:
        logger.error(f"❌ Error listing tools: {str(e)}")
        raise

@mcp.tool()
async def ping() -> Dict[str, Any]:
    """
    Simple ping endpoint to check server availability and get basic server information.
    """
    return {
        "status": "ok",
        "server": "splunk-mcp",
        "version": VERSION,
        "timestamp": datetime.now().isoformat(),
        "protocol": "mcp",
        "capabilities": ["splunk"]
    }

if __name__ == "__main__":
    import sys
    
    mode = sys.argv[1] if len(sys.argv) > 1 else "sse"
    
    if mode not in ["stdio", "sse"]:
        logger.error(f"❌ Invalid mode: {mode}. Must be one of: stdio, sse")
        sys.exit(1)
    
    if os.environ.get("DEBUG", "false").lower() == "true":
        logger.setLevel(logging.DEBUG)
    
    logger.info(f"🚀 Starting Splunk MCP server in {mode.upper()} mode on port {FASTMCP_PORT}")
    
    if mode == "stdio":
        mcp.run(transport=mode)
    else:
        uvicorn.run(app, host="0.0.0.0", port=FASTMCP_PORT)