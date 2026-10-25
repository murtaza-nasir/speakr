"""Voyage wire-format regressions; no credentials or network required."""
import json
import unittest
from unittest.mock import patch

import httpx
from flask import Flask
from openai import OpenAI

from tests.test_embedding_api_mode import reload_with_env


class VoyageEmbeddingsTests(unittest.TestCase):
    def setUp(self):
        self.ctx = Flask(__name__).app_context()
        self.ctx.push()
        self.requests = []

    def tearDown(self):
        reload_with_env()
        self.ctx.pop()

    def client(self, base, fail=False):
        def handle(request):
            body = json.loads(request.content)
            self.requests.append(body)
            if fail:
                return httpx.Response(400, json={'detail': 'Invalid output_dimension'})
            return httpx.Response(200, json={
                'object': 'list',
                'data': [{'object': 'embedding', 'index': i, 'embedding': [0.1, 0.2]}
                         for i in range(len(body['input']))],
                'model': body['model'], 'usage': {'total_tokens': 4},
            })
        return OpenAI(api_key='test-only', base_url=base, max_retries=0,
                      http_client=httpx.Client(transport=httpx.MockTransport(handle)))

    def test_documents_use_voyage_wire_format(self):
        base = 'https://api.voyageai.com/v1'
        emb = reload_with_env(EMBEDDING_BASE_URL=base, EMBEDDING_MODEL='voyage-4-lite',
                              EMBEDDING_DIMENSIONS='256')
        with self.client(base) as client, patch.object(emb, 'get_embedding_api_client', return_value=client):
            vectors = emb.generate_embeddings(['hello', 'world'])
        self.assertEqual(len(vectors), 2)
        self.assertEqual(vectors[0].dtype.name, 'float32')
        body = self.requests[0]
        self.assertIsNone(body['encoding_format'])
        self.assertEqual(body['input_type'], 'document')
        self.assertEqual(body['output_dimension'], 256)
        self.assertNotIn('dimensions', body)

    def test_query_type_survives_batching(self):
        base = 'https://api.voyageai.com/v1/'
        emb = reload_with_env(EMBEDDING_BASE_URL=base, EMBEDDING_MODEL='voyage-4-lite')
        with self.client(base) as client, patch.object(emb, 'get_embedding_api_client', return_value=client), patch.dict('os.environ', {'EMBEDDING_API_BATCH_SIZE': '1'}):
            vectors = emb._api_embed(['one?', 'two?'], input_type='query')
        self.assertEqual(len(vectors), 2)
        self.assertEqual(len(self.requests), 2)
        for body in self.requests:
            self.assertEqual(body['input_type'], 'query')
            self.assertNotIn('output_dimension', body)

    def test_other_hosts_keep_openai_format(self):
        for base in ('https://api.openai.com/v1', 'https://api.voyageai.com.example.org/v1',
                     'https://proxy.example.org/api.voyageai.com/v1'):
            with self.subTest(base=base):
                emb = reload_with_env(EMBEDDING_BASE_URL=base, EMBEDDING_DIMENSIONS='256')
                with self.client(base) as client, patch.object(emb, 'get_embedding_api_client', return_value=client):
                    self.assertEqual(len(emb.generate_embeddings(['text'])), 1)
                body = self.requests[-1]
                self.assertEqual(body['encoding_format'], 'float')
                self.assertEqual(body['dimensions'], 256)
                self.assertNotIn('input_type', body)
                self.assertNotIn('output_dimension', body)

    def test_semantic_search_marks_request_as_query(self):
        base = 'https://api.voyageai.com/v1'
        emb = reload_with_env(EMBEDDING_BASE_URL=base, EMBEDDING_MODEL='voyage-4-lite')
        with self.client(base) as client, patch.object(emb, 'get_embedding_api_client', return_value=client), patch.object(emb, 'get_accessible_recording_ids', return_value=[]):
            self.assertEqual(emb.semantic_search_chunks(None, 'When is launch?'), [])
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.requests[0]['input_type'], 'query')

    def test_voyage_bad_dimension_does_not_silently_change_vector_space(self):
        base = 'https://api.voyageai.com/v1'
        emb = reload_with_env(EMBEDDING_BASE_URL=base, EMBEDDING_DIMENSIONS='123')
        with self.client(base, fail=True) as client, patch.object(emb, 'get_embedding_api_client', return_value=client):
            self.assertEqual(emb.generate_embeddings(['text']), [])
        self.assertEqual(len(self.requests), 1)


if __name__ == '__main__':
    unittest.main()
