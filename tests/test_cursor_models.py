import sys
from pathlib import Path
import unittest
from unittest.mock import patch
import json
import sqlite3
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))
from cursor_models import ModelError, resolve_model
from session import composer_model


class CursorModels(unittest.TestCase):
    models = {
        'auto': 'Auto (default)',
        'cursor-grok-4.6-low': 'Grok 4.6 Low',
        'cursor-grok-4.6-medium': 'Grok 4.6 Medium',
        'cursor-grok-4.6-high': 'Grok 4.6',
        'cursor-grok-4.6-high-fast': 'Grok 4.6 Fast',
        'cursor-grok-4.6-xhigh': 'Grok 4.6 Extra High',
        'claude-opus-4-8-high': 'Claude Opus 4.8 1M',
        'gpt-5.5-extra-high-fast': 'GPT-5.5 Extra High Fast',
    }

    def resolve(self, name, params=()):
        return resolve_model(name, params, models=self.models)

    def test_cursor_display_name_maps_to_catalog_default(self):
        self.assertEqual(self.resolve('Cursor Grok 4.6'), 'cursor-grok-4.6-high')

    def test_exact_slug_and_bracket_settings_are_preserved(self):
        self.assertEqual(self.resolve('cursor-grok-4.6-low'), 'cursor-grok-4.6-low')
        model='cursor-grok-4.6-high[effort=low,fast=false]'
        self.assertEqual(self.resolve(model),model)

    def test_composer_effort_and_fast_override_default_display_name(self):
        self.assertEqual(self.resolve('grok-4.6', [{'id':'effort','value':'low'}]), 'cursor-grok-4.6-low')
        self.assertEqual(self.resolve('Cursor Grok 4.6', [{'id':'fast','value':True}]), 'cursor-grok-4.6-high-fast')

    def test_family_identifiers_accept_explicit_composer_parameters(self):
        self.assertEqual(self.resolve('claude-opus-4-8',[{'id':'effort','value':'high'}]), 'claude-opus-4-8-high')
        self.assertEqual(self.resolve('gpt-5.5',[{'id':'effort','value':'xhigh'},{'id':'fast','value':True}]), 'gpt-5.5-extra-high-fast')

    def test_missing_variant_does_not_drop_effort_or_fast(self):
        with self.assertRaises(ModelError):
            self.resolve('Cursor Grok 4.6', [{'id':'effort','value':'low'},{'id':'fast','value':True}])

    def test_unknown_and_ambiguous_names_fail(self):
        with self.assertRaises(ModelError): self.resolve('Imaginary Model')
        with self.assertRaises(ModelError):
            resolve_model('Duplicated', models={'first':'Duplicated','second':'Duplicated'})

    def test_composer_display_names_and_parameters_use_same_resolver(self):
        with tempfile.TemporaryDirectory() as directory:
            db=Path(directory)/'state.vscdb'
            with sqlite3.connect(db) as connection:
                connection.execute('CREATE TABLE cursorDiskKV (key TEXT, value TEXT)')
                connection.execute('INSERT INTO cursorDiskKV VALUES (?,?)',
                    ('composerData:calling-thread',json.dumps({'modelConfig':{'selectedModels':[
                        {'modelId':'grok-4.6','parameters':[{'id':'effort','value':'low'}]}]}})))
            with patch('cursor_models.catalog',return_value=self.models):
                self.assertEqual(composer_model(str(db),'calling-thread'),'cursor-grok-4.6-low')
