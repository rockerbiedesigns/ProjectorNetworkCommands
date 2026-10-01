"""Hardware-free unit tests for DriverManager.

No projector and no TouchDesigner are required: DriverManager is pure Python, so
these import drivers/driver_manager.py directly and exercise it against both
synthetic temp driver trees (for isolation + malformed-input cases) and the real
drivers/ tree in this repo.

Run headless:
    <venv-python> drivers/tests/test_driver_manager.py
    python -m unittest discover -s drivers/tests
"""

from __future__ import annotations
import os
import json
import shutil
import tempfile
import importlib.util
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_DRIVERS_DIR = os.path.dirname(_HERE)                       # repo drivers/
_DM_PATH = os.path.join(_DRIVERS_DIR, 'driver_manager.py')

_spec = importlib.util.spec_from_file_location('driver_manager_under_test', _DM_PATH)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
DriverManager = _mod.DriverManager


def _write(root, layer, name, obj):
    folder = os.path.join(root, layer)
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, name)
    with open(path, 'w', encoding='utf-8') as f:
        if isinstance(obj, str):
            f.write(obj)                                   # raw text (for malformed cases)
        else:
            json.dump(obj, f)
    return path


def _base_tree(root):
    """A minimal protocol->vendor->model tree used by several tests."""
    _write(root, 'protocols', 'pjlink.json', {
        'driver_id': 'protocol.pjlink', 'label': 'Generic PJLink', 'protocol': 'pjlink',
        'tier': 'protocol', 'extends': None, 'match': {'always': True},
        'transport': {'type': 'pjlink', 'port': 4352},
        'capabilities': {
            'power.on':  {'command': '%1POWR 1', 'parse': 'pjlink_ack', 'confirm_with': 'power.get'},
            'power.get': {'command': '%1POWR ?', 'parse': 'pjlink_power'},
            'input.get': {'command': '%1INPT ?', 'parse': 'pjlink_text'},
            'info.model': {'command': '%1INF2 ?', 'parse': 'pjlink_text'},
        },
    })
    _write(root, 'vendors', 'epson.json', {
        'driver_id': 'vendor.epson', 'label': 'Epson', 'protocol': 'pjlink',
        'tier': 'vendor', 'extends': 'protocol.pjlink',
        'match': {'manufacturer_contains': 'epson'},
        'capabilities': {
            'input.get':   {'command': 'SOURCE?', 'parse': 'escvp_val',
                            'transport': {'type': 'esc_vp21', 'port': 3629}},
            'av.mute.get': {'command': 'MUTE?', 'parse': 'escvp_val',
                            'transport': {'type': 'esc_vp21', 'port': 3629}},
        },
    })
    _write(root, 'models', 'epson.eb410w.json', {
        'driver_id': 'model.epson.eb410w', 'label': 'Epson EB-410W', 'protocol': 'pjlink',
        'tier': 'model', 'extends': 'vendor.epson',
        'match': {'manufacturer_contains': 'epson', 'model_contains': '410w', 'pjlink_class': '1'},
        'capabilities': {
            'power.get': {'command': '%1POWR ?', 'parse': 'pjlink_power',
                          'meta': {'provenance': 'hardware_verified'}},
        },
    })


class TempTreeTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix='drvtest_')

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def dm(self):
        d = DriverManager(self.root)
        d.load()
        return d

    def test_loads_clean(self):
        _base_tree(self.root)
        d = self.dm()
        self.assertEqual(d.errors, [])
        self.assertEqual(d.driver_ids(),
                         ['model.epson.eb410w', 'protocol.pjlink', 'vendor.epson'])

    def test_generic_pjlink_fallback(self):
        _base_tree(self.root)
        d = self.dm()
        self.assertEqual(d.match_driver('Acme', 'ZX-1'), 'protocol.pjlink')

    def test_unknown_manufacturer_fallback(self):
        _base_tree(self.root)
        d = self.dm()
        self.assertEqual(d.match_driver('', ''), 'protocol.pjlink')
        self.assertEqual(d.match_driver('Panasonic', 'PT-XYZ'), 'protocol.pjlink')

    def test_epson_vendor_match(self):
        _base_tree(self.root)
        d = self.dm()
        self.assertEqual(d.match_driver('EPSON', 'EB-96W'), 'vendor.epson')

    def test_model_specific_match_beats_vendor(self):
        _base_tree(self.root)
        d = self.dm()
        self.assertEqual(
            d.match_driver('EPSON', 'EB-410W/PowerLite 410W', '1'),
            'model.epson.eb410w')

    def test_model_requires_its_model_token(self):
        # an Epson that is not a 410W must not match the model driver
        _base_tree(self.root)
        d = self.dm()
        self.assertEqual(d.match_driver('EPSON', 'EB-2250U', '1'), 'vendor.epson')

    def test_inheritance_merges_capabilities(self):
        _base_tree(self.root)
        d = self.dm()
        caps = d.capabilities('model.epson.eb410w')
        # power.on + info.model inherited from protocol, av.mute.get from vendor
        for a in ('power.on', 'power.get', 'input.get', 'info.model', 'av.mute.get'):
            self.assertIn(a, caps)

    def test_capability_override_changes_transport(self):
        _base_tree(self.root)
        d = self.dm()
        proto_in = d.resolve_action('protocol.pjlink', 'input.get')
        epson_in = d.resolve_action('vendor.epson', 'input.get')
        self.assertEqual(proto_in['command'], '%1INPT ?')
        self.assertEqual(epson_in['command'], 'SOURCE?')
        self.assertEqual(epson_in['transport']['type'], 'esc_vp21')
        # inherited core action keeps the protocol transport default
        self.assertEqual(d.transport('vendor.epson').get('type'), 'pjlink')

    def test_capability_origin(self):
        _base_tree(self.root)
        d = self.dm()
        origin = d.capability_origin('model.epson.eb410w')
        self.assertEqual(origin['power.on'], 'protocol.pjlink')   # inherited
        self.assertEqual(origin['av.mute.get'], 'vendor.epson')   # inherited from vendor
        self.assertEqual(origin['input.get'], 'vendor.epson')     # overridden at vendor
        self.assertEqual(origin['power.get'], 'model.epson.eb410w')  # overridden at model

    def test_resolve_chain(self):
        _base_tree(self.root)
        d = self.dm()
        self.assertEqual(
            d.resolve_chain('model.epson.eb410w'),
            ['protocol.pjlink', 'vendor.epson', 'model.epson.eb410w'])

    def test_malformed_json_rejected_others_survive(self):
        _base_tree(self.root)
        _write(self.root, 'models', 'broken.json', '{ this is not json ')
        d = self.dm()
        self.assertTrue(any('broken.json' in e and 'invalid JSON' in e for e in d.errors),
                        d.errors)
        # the good drivers still loaded
        self.assertIn('vendor.epson', d.driver_ids())

    def test_schema_missing_command(self):
        _base_tree(self.root)
        _write(self.root, 'vendors', 'bad.json', {
            'driver_id': 'vendor.bad', 'protocol': 'pjlink', 'tier': 'vendor',
            'extends': 'protocol.pjlink', 'match': {'manufacturer_contains': 'bad'},
            'capabilities': {'power.get': {'parse': 'pjlink_power'}},   # no command
        })
        d = self.dm()
        self.assertTrue(any('bad.json' in e and 'command' in e for e in d.errors), d.errors)
        self.assertNotIn('vendor.bad', d.driver_ids())

    def test_schema_unknown_parser(self):
        _base_tree(self.root)
        _write(self.root, 'vendors', 'bad2.json', {
            'driver_id': 'vendor.bad2', 'protocol': 'pjlink', 'tier': 'vendor',
            'extends': 'protocol.pjlink', 'match': {'manufacturer_contains': 'bad2'},
            'capabilities': {'power.get': {'command': '%1POWR ?', 'parse': 'do_evil'}},
        })
        d = self.dm()
        self.assertTrue(any('bad2.json' in e and 'unknown parser' in e for e in d.errors), d.errors)

    def test_schema_unknown_key_rejected(self):
        # additionalProperties:false is what guarantees no place for executable code
        _base_tree(self.root)
        _write(self.root, 'vendors', 'bad3.json', {
            'driver_id': 'vendor.bad3', 'protocol': 'pjlink', 'tier': 'vendor',
            'extends': 'protocol.pjlink', 'match': {'manufacturer_contains': 'bad3'},
            'capabilities': {'power.get': {'command': '%1POWR ?', 'parse': 'pjlink_power',
                                           'python': 'os.system("boom")'}},
        })
        d = self.dm()
        self.assertTrue(any('bad3.json' in e and 'unknown key' in e for e in d.errors), d.errors)

    def test_extends_must_resolve(self):
        _base_tree(self.root)
        _write(self.root, 'models', 'orphan.json', {
            'driver_id': 'model.orphan', 'protocol': 'pjlink', 'tier': 'model',
            'extends': 'vendor.nope', 'match': {'model_contains': 'orphan'},
            'capabilities': {},
        })
        d = self.dm()
        self.assertTrue(any('orphan' in e and 'extends unknown driver' in e for e in d.errors),
                        d.errors)

    def test_tier_folder_mismatch_rejected(self):
        _base_tree(self.root)
        _write(self.root, 'vendors', 'misfiled.json', {
            'driver_id': 'model.misfiled', 'protocol': 'pjlink',   # model id in vendors/
            'extends': 'protocol.pjlink', 'match': {'model_contains': 'misfiled'},
            'capabilities': {},
        })
        d = self.dm()
        self.assertTrue(any('misfiled' in e and 'belongs in models/' in e for e in d.errors),
                        d.errors)

    def test_prefix_and_class_matching(self):
        # family-prefix match and pjlink_class disqualification
        _write(self.root, 'protocols', 'pjlink.json', {
            'driver_id': 'protocol.pjlink', 'protocol': 'pjlink', 'tier': 'protocol',
            'extends': None, 'match': {'always': True},
            'transport': {'type': 'pjlink', 'port': 4352},
            'capabilities': {'power.get': {'command': '%1POWR ?', 'parse': 'pjlink_power'}},
        })
        _write(self.root, 'families', 'acme.fx.json', {
            'driver_id': 'family.acme.fx', 'protocol': 'pjlink', 'tier': 'family',
            'extends': 'protocol.pjlink',
            'match': {'manufacturer_contains': 'acme', 'model_prefix': 'fx-', 'pjlink_class': '2'},
            'capabilities': {},
        })
        d = self.dm()
        self.assertEqual(d.errors, [])
        # right prefix + right class -> family
        self.assertEqual(d.match_driver('ACME', 'FX-900', '2'), 'family.acme.fx')
        # right prefix but WRONG known class -> disqualified -> fallback
        self.assertEqual(d.match_driver('ACME', 'FX-900', '1'), 'protocol.pjlink')
        # unknown class does not disqualify
        self.assertEqual(d.match_driver('ACME', 'FX-900', ''), 'family.acme.fx')
        # wrong prefix -> fallback
        self.assertEqual(d.match_driver('ACME', 'GX-900', '2'), 'protocol.pjlink')

    def test_model_alias_matching(self):
        _base_tree(self.root)
        # add an alias-driven model
        _write(self.root, 'models', 'epson.c2010wn.json', {
            'driver_id': 'model.epson.c2010wn', 'protocol': 'pjlink', 'tier': 'model',
            'extends': 'vendor.epson',
            'match': {'manufacturer_contains': 'epson', 'model_aliases': ['c2010wn']},
            'capabilities': {},
        })
        d = self.dm()
        self.assertEqual(d.errors, [])
        self.assertEqual(d.match_driver('EPSON', 'C2010WN'), 'model.epson.c2010wn')

    def test_explain_structure(self):
        _base_tree(self.root)
        d = self.dm()
        e = d.explain('EPSON', 'EB-410W', '1')
        self.assertEqual(e['matched_driver'], 'model.epson.eb410w')
        self.assertEqual([c['driver_id'] for c in e['chain']],
                         ['protocol.pjlink', 'vendor.epson', 'model.epson.eb410w'])
        self.assertEqual(e['capabilities']['power.get']['origin'], 'own')
        self.assertEqual(e['capabilities']['power.get']['provenance'], 'hardware_verified')
        self.assertEqual(e['capabilities']['input.get']['supplied_by'], 'vendor.epson')
        self.assertEqual(e['capabilities']['input.get']['origin'], 'inherited')
        self.assertTrue(e['capabilities']['input.get']['transport'].startswith('esc_vp21:'))
        self.assertIn('EB-410W', d.explain_text('EPSON', 'EB-410W', '1'))


class RealTreeTest(unittest.TestCase):
    """Validate the actual drivers/ tree shipped in this repo."""

    def dm(self):
        d = DriverManager(_DRIVERS_DIR)
        d.load()
        return d

    def test_real_tree_loads_without_errors(self):
        d = self.dm()
        self.assertEqual(d.errors, [], 'driver load errors: %s' % d.errors)
        for did in ('protocol.pjlink', 'vendor.epson', 'model.epson.eb410w'):
            self.assertIn(did, d.driver_ids())

    def test_real_epson_detected_unit_resolves_to_model(self):
        d = self.dm()
        self.assertEqual(
            d.match_driver('EPSON', 'EB-410W/PowerLite 410W', '1'),
            'model.epson.eb410w')

    def test_real_other_epson_resolves_to_vendor(self):
        d = self.dm()
        self.assertEqual(
            d.match_driver('EPSON', 'EB-96W/C2030WN/PowerLite 96W', '1'),
            'vendor.epson')

    def test_real_power_stays_pjlink_input_is_escvp(self):
        d = self.dm()
        drv = 'model.epson.eb410w'
        self.assertEqual(d.resolve_action(drv, 'power.on')['command'], '%1POWR 1')
        self.assertEqual(d.transport(drv).get('type'), 'pjlink')      # inherited default
        self.assertEqual(d.resolve_action(drv, 'input.get')['transport']['type'], 'esc_vp21')

    def test_real_no_guessed_source_set(self):
        # we dropped the guessed input.set.hdmi1 (SOURCE 30); it must be gone
        d = self.dm()
        self.assertNotIn('input.set.hdmi1', d.capabilities('model.epson.eb410w'))


if __name__ == '__main__':
    unittest.main(verbosity=2)
