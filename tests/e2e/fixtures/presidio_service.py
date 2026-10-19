"""Disposable HTTP wrapper around the real Presidio analyzer, for acceptance only."""
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from presidio_analyzer import AnalyzerEngine
from presidio_analyzer.nlp_engine import NlpEngineProvider

nlp = NlpEngineProvider(nlp_configuration={
    'nlp_engine_name': 'spacy',
    'models': [{'lang_code': 'en', 'model_name': 'en_core_web_sm'}],
}).create_engine()
analyzer = AnalyzerEngine(nlp_engine=nlp, supported_languages=['en'])


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # Never log submitted text.

    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'ready')

    def do_POST(self):
        try:
            data = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            findings = analyzer.analyze(**data)
            response = json.dumps([dict(start=f.start, end=f.end, score=f.score,
                entity_type=f.entity_type) for f in findings]).encode()
            self.send_response(200)
        except Exception:
            response = b'{"error":"analysis failed"}'
            self.send_response(500)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(response)


ThreadingHTTPServer(('127.0.0.1', int(sys.argv[1])), Handler).serve_forever()
