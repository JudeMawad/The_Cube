"""Backend client and AI-node process compatibility over the HTTP contract."""
import json
import unittest
import httpx

from ai_node.app import create_app
from core.ai_client import AIClient
from core.config import AINodeConfig
from core.schemas import ProcessRequest, ConversationResult, ToolIntent
from tests.ai_node.test_process import BASE, CONFIG, INTENT, TOOL, envelope


class ProcessClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_backend_client_accepts_both_result_types(self):
        upstream = httpx.Response(200, json=envelope())
        app = create_app(llm_config=CONFIG, llm_transport=httpx.MockTransport(lambda request: upstream))
        client = AIClient(AINodeConfig("http://ai.invalid"), transport=httpx.ASGITransport(app=app))
        self.assertIsInstance(await client.process_remote(ProcessRequest(**BASE)), ConversationResult)
        upstream = httpx.Response(200, json=envelope(json.dumps(INTENT)))
        self.assertIsInstance(await client.process_remote(ProcessRequest(**BASE, tools=[TOOL])), ToolIntent)
