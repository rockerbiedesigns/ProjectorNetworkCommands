"""Synthetic cross-vendor architecture tests (hardware-free).

These prove the device-agnostic core (DriverManager + schema) does NOT bake in
Epson/PJLink assumptions. They run against an explicitly FICTIONAL driver tree in
fixtures/synthetic_vendor/ (vendors 'ACME' and 'ZENITH' with invented transports
and parsers). None of these fixtures describe a real projector and none must ever
be shipped as a usable driver.

What they establish (the milestone's directive #3):
  * vendor matching is generic (works for invented manufacturers),
  * protocol -> vendor -> model inheritance works independently of Epson,
  * different transports implement the same normalized capability,
  * a vendor driver can override an inherited capability,
  * one vendor's parser/transport never leaks into another,
  * an unknown device falls back to generic PJLink,
plus the new machinery: parameterized 'set' resolution and the explicit 'kind'
field -- and that a brand-new vendor's parser/transport names are accepted purely
by INJECTION, with no edit to driver_manager.py.

Run headless:
    <venv-python> -m unittest discover -s drivers/tests
"""

from __future__ import annotations
import os
import json
import shutil
import tempfile
import importlib.util
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_DRIVERS_DIR = os.path.dirname(_HERE)
_DM_PATH = os.path.join(_DRIVERS_DIR, 'driver_manager.py')
_FIXTURE = os.path.join(_HERE, 'fixtures', 'synthetic_vendor')

_spec = importlib.util.spec_from_file_location('driver_manager_xv', _DM_PATH)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
DriverManager = _mod.DriverManager

# A fictional vendor's parser and transport NAMES. The composition root supplies
# these; the core knows nothing about them until they are injected here.
_XV_PARSERS = {'acme_val', 'acme_ack', 'zenith_txt'}
_XV_TRANSPORTS = {'acme_lan', 'zenith_tcp'}


def _dm():
    d = DriverManager(_FIXTURE, extra_parsers=_XV_PARSERS, extra_transports=_XV_TRANSPORTS)
    d.load()
    return d


class InjectionBoundaryTest(unittest.TestCase):
    """The core accepts new vendor names ONLY via injection -- proving they are
    not hardcoded, and that an un-injected name fails loudly rather than silently."""

    def test_loads_clean_with_injection(self):
        d = _dm()
        self.assertEqual(d.errors, [], d.errors)
        for did in ('protocol.pjlink', 'vendor.acme', 'vendor.zenith', 'model.acme.x1'):
            self.assertIn(did, d.driver_ids())

    def test_without_injection_unknown_transport_and_parser_rejected(self):
        d = DriverManager(_FIXTURE)                      # no extras
        d.load()
        joined = ' | '.join(d.errors)
        self.assertIn('acme_lan', joined)                # unknown transport named
        self.assertIn('acme_val', joined)                # unknown parser named
        # the generic PJLink baseline still loads despite the vendor failures
        self.assertIn('protocol.pjlink', d.driver_ids())


class MatchingTest(unittest.TestCase):
    def test_generic_vendor_matching(self):
        d = _dm()
        self.assertEqual(d.match_driver('ACME Corp', 'X1-900'), 'model.acme.x1')
        self.assertEqual(d.match_driver('ACME Corp', 'Z9'), 'vendor.acme')
        self.assertEqual(d.match_driver('Zenith Displays', 'ZZ-1'), 'vendor.zenith')

    def test_unknown_device_falls_back_to_pjlink(self):
        d = _dm()
        self.assertEqual(d.match_driver('Sony', 'VPL-FHZ'), 'protocol.pjlink')
        self.assertEqual(d.match_driver('', ''), 'protocol.pjlink')


class InheritanceTest(unittest.TestCase):
    def test_inheritance_independent_of_epson(self):
        d = _dm()
        caps = d.capabilities('model.acme.x1')
        # inherited from protocol.pjlink, inherited from vendor.acme, own override
        for a in ('power.on', 'power.get', 'input.get', 'input.set', 'av.mute.on'):
            self.assertIn(a, caps)
        self.assertEqual(
            d.resolve_chain('model.acme.x1'),
            ['protocol.pjlink', 'vendor.acme', 'model.acme.x1'])

    def test_vendor_overrides_inherited_capability(self):
        d = _dm()
        origin = d.capability_origin('model.acme.x1')
        self.assertEqual(origin['power.on'], 'protocol.pjlink')  # inherited
        self.assertEqual(origin['input.get'], 'vendor.acme')     # overridden at vendor
        self.assertEqual(origin['input.set'], 'model.acme.x1')   # overridden at model

    def test_different_transports_same_capability(self):
        d = _dm()
        # the SAME normalized action resolves to two different transports per vendor
        self.assertEqual(d.resolve_action('vendor.acme', 'input.get')['transport']['type'],
                         'acme_lan')
        self.assertEqual(d.resolve_action('vendor.zenith', 'input.get')['transport']['type'],
                         'zenith_tcp')
        # an inherited PJLink action keeps the PJLink default transport
        self.assertEqual(d.transport('vendor.acme').get('type'), 'pjlink')
        self.assertIsNone(d.resolve_action('vendor.acme', 'power.get').get('transport'))

    def test_no_cross_vendor_leakage(self):
        d = _dm()
        acme = d.explain('ACME', 'X1')['capabilities']
        zenith = d.explain('Zenith', 'ZZ')['capabilities']
        self.assertFalse(any('zenith' in c['transport'] for c in acme.values()))
        self.assertFalse(any('acme' in c['transport'] for c in zenith.values()))


class ParameterizedSetTest(unittest.TestCase):
    def test_set_resolution_uses_value_map(self):
        d = _dm()
        self.assertEqual(
            d.resolve_set('model.acme.x1', 'input.set', 'hdmi1')['command'], 'SRC H1')
        # model-level override added 'sdi'
        self.assertEqual(
            d.resolve_set('model.acme.x1', 'input.set', 'sdi')['command'], 'SRC S1')

    def test_set_refuses_unknown_value(self):
        d = _dm()
        self.assertIsNone(d.resolve_set('model.acme.x1', 'input.set', 'bogus'))
        # vendor.acme has no 'sdi' (only the model added it)
        self.assertIsNone(d.resolve_set('vendor.acme', 'input.set', 'sdi'))

    def test_set_values_lists_accepted_values(self):
        d = _dm()
        self.assertEqual(d.set_values('model.acme.x1', 'input.set'),
                         ['hdmi1', 'hdmi2', 'sdi', 'vga1'])
        self.assertEqual(d.set_values('model.acme.x1', 'power.get'), [])   # not a set

    def test_set_on_non_set_action_returns_none(self):
        d = _dm()
        self.assertIsNone(d.resolve_set('model.acme.x1', 'power.get', 'x'))


class CapabilityKindTest(unittest.TestCase):
    def test_declared_kind_is_used(self):
        d = _dm()
        self.assertEqual(
            DriverManager.capability_kind('power.on', d.resolve_action('vendor.acme', 'power.on')),
            'command')
        self.assertEqual(
            DriverManager.capability_kind('power.get', d.resolve_action('vendor.acme', 'power.get')),
            'query')
        self.assertEqual(
            DriverManager.capability_kind('input.set', d.resolve_action('model.acme.x1', 'input.set')),
            'set')

    def test_kind_inference_without_declaration(self):
        # no spec: fall back to name-suffix inference (the one place it lives)
        self.assertEqual(DriverManager.capability_kind('lamp.hours', None), 'query')
        self.assertEqual(DriverManager.capability_kind('power.off', None), 'command')
        self.assertEqual(DriverManager.capability_kind('input.set', None), 'set')


class SetSchemaSafetyTest(unittest.TestCase):
    """A 'set' capability with neither a value_map nor a {value} placeholder is a
    latent bug (nothing to substitute); the loader must reject it."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix='xvset_')

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def _write(self, layer, name, obj):
        folder = os.path.join(self.root, layer)
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, name), 'w', encoding='utf-8') as f:
            json.dump(obj, f)

    def test_set_without_value_map_or_placeholder_rejected(self):
        self._write('protocols', 'pjlink.json', {
            'driver_id': 'protocol.pjlink', 'protocol': 'pjlink', 'tier': 'protocol',
            'extends': None, 'match': {'always': True},
            'capabilities': {
                'input.set': {'command': 'SRC', 'kind': 'set', 'parse': 'pjlink_text'},
            },
        })
        d = DriverManager(self.root)
        d.load()
        self.assertTrue(any('input.set' in e and 'value_map' in e for e in d.errors), d.errors)

    def test_value_map_on_non_set_rejected(self):
        self._write('protocols', 'pjlink.json', {
            'driver_id': 'protocol.pjlink', 'protocol': 'pjlink', 'tier': 'protocol',
            'extends': None, 'match': {'always': True},
            'capabilities': {
                'power.get': {'command': '%1POWR ?', 'kind': 'query', 'parse': 'pjlink_power',
                              'value_map': {'a': 'b'}},
            },
        })
        d = DriverManager(self.root)
        d.load()
        self.assertTrue(any('power.get' in e and 'value_map' in e for e in d.errors), d.errors)


if __name__ == '__main__':
    unittest.main(verbosity=2)
