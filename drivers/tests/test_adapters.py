"""Hardware-free unit tests for the extracted transport/parser adapters.

No projector, no sockets, no TouchDesigner: parsers are pure string functions and
the transport registry is exercised only for its names/dispatch (the socket code
itself is verified live against the Epson). Loaded by path, the same way the TD
extension and the other suites load these modules.
"""

from __future__ import annotations
import os
import importlib.util
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_DRIVERS_DIR = os.path.dirname(_HERE)


def _load(name):
    path = os.path.join(_DRIVERS_DIR, name + '.py')
    spec = importlib.util.spec_from_file_location('adapters_%s_under_test' % name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


parsers = _load('parsers')
transports = _load('transports')
driver_manager = _load('driver_manager')


class ParserTest(unittest.TestCase):
    def test_pjlink_power(self):
        self.assertEqual(parsers.pjlink_power('%1POWR=1'), '1')
        self.assertEqual(parsers.pjlink_power('%1POWR=0'), '0')
        self.assertEqual(parsers.pjlink_power('%1POWR=OK'), 'OK')
        self.assertEqual(parsers.pjlink_power('%1POWR=4'), 'ERR')    # out of range
        self.assertEqual(parsers.pjlink_power('garbage'), 'ERR')
        self.assertEqual(parsers.pjlink_power(''), 'ERR')

    def test_pjlink_ack(self):
        self.assertEqual(parsers.pjlink_ack('%1POWR=OK'), 'OK')
        self.assertEqual(parsers.pjlink_ack('%1POWR=ERR2'), 'ERR')

    def test_pjlink_text(self):
        self.assertEqual(parsers.pjlink_text('%1INF2=EB-2250U'), 'EB-2250U')
        self.assertEqual(parsers.pjlink_text('%1INF1=ERR3'), '')
        self.assertEqual(parsers.pjlink_text('no-equals'), '')

    def test_pjlink_lamp(self):
        self.assertEqual(parsers.pjlink_lamp('%1LAMP=8262 1'), '8262')
        self.assertEqual(parsers.pjlink_lamp('%1LAMP=8262 1 13 0'), '8262/13')
        self.assertEqual(parsers.pjlink_lamp('%1LAMP=ERR1'), '')

    def test_escvp(self):
        self.assertEqual(parsers.escvp_val('SOURCE=14'), '14')
        self.assertEqual(parsers.escvp_val('MUTE=OFF'), 'OFF')
        self.assertEqual(parsers.escvp_val('ERR'), '')
        self.assertEqual(parsers.escvp_ack(''), 'OK')        # bare prompt -> ack
        self.assertEqual(parsers.escvp_ack('ERR'), 'ERR')

    def test_get_falls_back_to_text(self):
        # an unknown parser name resolves to pjlink_text, never None
        fn = parsers.get('no_such_parser')
        self.assertIs(fn, parsers.get('pjlink_text'))
        self.assertEqual(fn('%1INF2=X'), 'X')


class TransportRegistryTest(unittest.TestCase):
    def test_names_and_dispatch(self):
        self.assertEqual(transports.names(), {'pjlink', 'esc_vp21'})
        self.assertTrue(callable(transports.get('pjlink')))
        self.assertTrue(callable(transports.get('esc_vp21')))
        self.assertIsNone(transports.get('no_such_transport'))


class RegistryMatchesCoreBaseTest(unittest.TestCase):
    """The adapter registries must stay in lockstep with DriverManager's base
    name sets, so injecting names() is consistent and nothing silently drifts."""

    def test_parser_names_match_core_base(self):
        self.assertEqual(parsers.names(), set(driver_manager.KNOWN_PARSERS))

    def test_transport_names_match_core_base(self):
        self.assertEqual(transports.names(), set(driver_manager.KNOWN_TRANSPORTS))


if __name__ == '__main__':
    unittest.main(verbosity=2)
