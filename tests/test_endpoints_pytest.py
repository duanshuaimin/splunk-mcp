#!/usr/bin/env python3
"""
Test module for Splunk MCP endpoints using pytest.
"""

import json
import pytest
import splunk_mcp
from unittest.mock import patch, MagicMock

# Most tests will use the first server defined in the test config
TEST_SERVER_1 = "test_server_1"
TEST_SERVER_2 = "test_server_2"

@pytest.fixture
def mock_splunk_service():
    """Create a mock Splunk service for testing to avoid real connections."""
    mock_service = MagicMock()
    
    # Mock indexes
    mock_index = MagicMock()
    mock_index.name = "_internal"
    mock_service.indexes = [mock_index]
    
    # Mock search job
    mock_job = MagicMock()
    search_results = {"results": [{"_raw": "test event"}]}
    mock_job.results.return_value.read.return_value = json.dumps(search_results).encode('utf-8')
    mock_service.jobs.create.return_value = mock_job
    
    # Mock users
    mock_user = MagicMock()
    mock_user.name = "admin"
    mock_user.content = {'realname': 'Admin', 'email': 'admin@example.com', 'roles': ['admin'], 'defaultApp': 'search', 'type': 'user'}
    mock_service.users = [mock_user]

    # Mock saved searches
    mock_saved_search = MagicMock()
    mock_saved_search.name = "Test Saved Search"
    mock_saved_search.description = "A test search"
    mock_saved_search.search = "index=_internal | head 1"
    mock_service.saved_searches = [mock_saved_search]

    # Mock KV store
    mock_kv_collection = MagicMock()
    mock_kv_collection.name = "test_collection"
    mock_kv_collection.access = {"app": "search"}
    mock_kv_collection.content = {"field.my_field": "string"}
    mock_service.kvstore = [mock_kv_collection]
    
    # Mock apps
    mock_app = MagicMock()
    mock_app.name = "search"
    mock_app.label = "Search & Reporting"
    mock_app.version = "1.0"
    mock_service.apps = [mock_app]

    return mock_service

@pytest.mark.asyncio
@patch('splunk_mcp.get_splunk_connection')
async def test_list_splunk_servers(mock_get_conn):
    """Test the list_splunk_servers tool."""
    # Reload config to ensure it's picked up by the running module
    splunk_mcp.load_config()
    result = await splunk_mcp.list_splunk_servers()
    assert isinstance(result, list)
    assert len(result) == 2
    assert TEST_SERVER_1 in result
    assert TEST_SERVER_2 in result

@pytest.mark.asyncio
@patch('splunk_mcp.get_splunk_connection')
async def test_search_splunk(mock_get_conn, mock_splunk_service):
    """Test the search_splunk tool."""
    mock_get_conn.return_value = mock_splunk_service
    
    # Test with default server
    result = await splunk_mcp.search_splunk(search_query="index=_internal | head 1")
    assert result is not None
    assert len(result) > 0
    mock_get_conn.assert_called_with(None)

    # Test with a specific server
    result = await splunk_mcp.search_splunk(search_query="index=_internal | head 1", server_name=TEST_SERVER_2)
    assert result is not None
    mock_get_conn.assert_called_with(TEST_SERVER_2)

@pytest.mark.asyncio
@patch('splunk_mcp.get_splunk_connection')
async def test_list_indexes(mock_get_conn, mock_splunk_service):
    """Test the list_indexes tool."""
    mock_get_conn.return_value = mock_splunk_service
    result = await splunk_mcp.list_indexes(server_name=TEST_SERVER_1)
    assert "indexes" in result
    assert "_internal" in result["indexes"]
    mock_get_conn.assert_called_with(TEST_SERVER_1)

@pytest.mark.asyncio
@patch('splunk_mcp.get_splunk_connection')
async def test_health_check(mock_get_conn, mock_splunk_service):
    """Test the health_check tool."""
    mock_get_conn.return_value = mock_splunk_service
    result = await splunk_mcp.health_check(server_name=TEST_SERVER_1)
    assert result["status"] == "healthy"
    assert result["server_name"] == TEST_SERVER_1
    assert len(result["apps"]) > 0
    mock_get_conn.assert_called_with(TEST_SERVER_1)

@pytest.mark.asyncio
@patch('splunk_mcp.get_splunk_connection')
async def test_connection_error_handling(mock_get_conn):
    """Test that connection errors are handled gracefully."""
    mock_get_conn.side_effect = Exception("Splunk connection failed")
    with pytest.raises(Exception, match="Splunk connection failed"):
        await splunk_mcp.list_indexes()

@pytest.mark.asyncio
async def test_invalid_server_name():
    """Test that a ValueError is raised for an invalid server name."""
    with pytest.raises(ValueError, match="Splunk server 'invalid_server' not found in config.yaml."):
        await splunk_mcp.list_indexes(server_name="invalid_server")

@pytest.mark.asyncio
@patch('splunk_mcp.get_splunk_connection')
async def test_all_tools_accept_server_name(mock_get_conn, mock_splunk_service):
    """
    Verify that all relevant tools accept the server_name parameter
    and pass it to get_splunk_connection.
    """
    mock_get_conn.return_value = mock_splunk_service
    
    tools_to_test = {
        splunk_mcp.search_splunk: {"search_query": "test"},
        splunk_mcp.list_indexes: {},
        splunk_mcp.get_index_info: {"index_name": "_internal"},
        splunk_mcp.list_saved_searches: {},
        splunk_mcp.current_user: {},
        splunk_mcp.list_users: {},
        splunk_mcp.list_kvstore_collections: {},
        splunk_mcp.health_check: {},
        splunk_mcp.get_indexes_and_sourcetypes: {},
    }
    
    mock_get_response = MagicMock()
    mock_get_response.body.read.return_value = json.dumps({
        "entry": [{"content": {"username": "admin"}}]
    }).encode('utf-8')
    mock_splunk_service.get.return_value = mock_get_response

    # Mock the getitem of the users collection
    mock_user = MagicMock()
    mock_user.name = "admin"
    mock_user.content = {
        'realname': 'Admin', 'email': 'admin@example.com', 'roles': ['admin'], 
        'defaultApp': 'search', 'type': 'user'
    }
    mock_users = MagicMock()
    mock_users.__getitem__.return_value = mock_user
    mock_splunk_service.users = mock_users
    
    # Mock the getitem of the indexes collection to avoid KeyError
    mock_indexes = MagicMock()
    mock_indexes.__getitem__.return_value = MagicMock()
    mock_splunk_service.indexes = mock_indexes

    for tool, params in tools_to_test.items():
        # Test with default server
        await tool(**params)
        mock_get_conn.assert_called_with(None)
        
        # Test with specific server
        await tool(**params, server_name=TEST_SERVER_2)
        mock_get_conn.assert_called_with(TEST_SERVER_2)