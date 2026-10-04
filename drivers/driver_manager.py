"""DriverManager -- device-agnostic driver/capability resolver.

Loads DECLARATIVE JSON driver definitions from a drivers/ tree
(protocols, vendors, families, models), VALIDATES each against the driver
schema, matches a discovered projector to the best driver (most specific match
wins; generic PJLink is the fallback), merges the 'extends' inheritance chain
(protocol -> vendor -> family -> model, any intermediate level optional), and
resolves a normalized action (e.g. 'power.on') to a transport command plus a
PARSER NAME.

Declarative only: JSON never contains Python. The 'parse' field is a string key
into a fixed set of parser names (KNOWN_PARSERS); the parser FUNCTIONS live in
Python (ProjectorScannerExt registers them) and all networking/transport stays
in Python. additionalProperties are rejected at load, so there is nowhere in a
driver file to put executable code.

This module touches NO TouchDesigner objects, so it is safe to import from
anywhere -- including a headless unittest with no TD running. It is the single
source of truth: the TD extension imports THIS file by path, and so do the
tests.
"""

from __future__ import annotations
import os
import json
import glob
from typing import Optional, List, Dict, Any

# Parser names a capability's 'parse' field may reference. The functions live in
# ProjectorScannerExt; this set is the declarative contract the loader validates
# against, so a typo fails loudly at load instead of silently at runtime.
KNOWN_PARSERS = {
    'pjlink_power', 'pjlink_lamp', 'pjlink_ack', 'pjlink_text',
    'escvp_val', 'escvp_ack',
}

KNOWN_TRANSPORTS = {'pjlink', 'esc_vp21'}
TIERS = ('protocol', 'vendor', 'family', 'model')
_TIER_RANK = {'protocol': 0, 'vendor': 1, 'family': 2, 'model': 3}
PROVENANCE = {'documented', 'hardware_verified'}

# Allowed keys per object (loader rejects any key not listed -> no place for code).
_DRIVER_KEYS = {'driver_id', 'label', 'protocol', 'tier', 'extends',
                'match', 'transport', 'capabilities'}
_MATCH_KEYS = {
    'always',
    'manufacturer_equals', 'manufacturer_contains', 'manufacturer_aliases',
    'model_equals', 'model_contains', 'model_prefix', 'model_aliases',
    'pjlink_class', 'firmware_min', 'firmware_max', 'firmware_equals',
}
_CAP_KEYS = {'command', 'kind', 'value_map', 'parse', 'confirm_with', 'transport', 'meta'}
CAP_KINDS = {'query', 'command', 'set'}
_TRANSPORT_KEYS = {'type', 'port', 'terminator'}
_META_KEYS = {'provenance', 'decoded', 'source'}
_SOURCE_KEYS = {'title', 'version', 'page', 'url'}


class DriverManager:
    def __init__(self, drivers_dir: str,
                 extra_parsers: Optional[set] = None,
                 extra_transports: Optional[set] = None) -> None:
        """extra_parsers / extra_transports let the composition root register a new
        vendor's parser and transport NAMES without editing this file -- the base
        sets cover the protocol baseline (PJLink) and Epson's ESC/VP21; a new
        vendor adapter passes its names in. This is why adding a manufacturer never
        touches the device-agnostic core."""
        self.drivers_dir = drivers_dir
        self._parsers = set(KNOWN_PARSERS) | set(extra_parsers or ())
        self._transports = set(KNOWN_TRANSPORTS) | set(extra_transports or ())
        self._drivers: Dict[str, Dict[str, Any]] = {}   # driver_id -> validated definition
        self._errors: List[str] = []

    # ---- loading ------------------------------------------------------------

    def load(self) -> None:
        """(Re)load and VALIDATE every *.json driver from the four tier folders.
        A malformed driver is skipped with a clear message in .errors; the rest
        still load."""
        self._drivers = {}
        self._errors = []
        seen_source: Dict[str, str] = {}
        for layer in ('protocols', 'vendors', 'families', 'models'):
            folder = os.path.join(self.drivers_dir, layer)
            if not os.path.isdir(folder):
                continue
            for path in sorted(glob.glob(os.path.join(folder, '*.json'))):
                disp = path.replace('\\', '/')
                try:
                    with open(path, 'r', encoding='utf-8') as f:
                        data = json.load(f)
                except Exception as e:
                    self._errors.append('%s: invalid JSON (%s)' % (disp, e))
                    continue
                errs = self._validate(data, layer)
                if errs:
                    for e in errs:
                        self._errors.append('%s: %s' % (disp, e))
                    continue
                did = data['driver_id']
                if did in seen_source:
                    self._errors.append('%s: duplicate driver_id %r (already defined in %s)'
                                        % (disp, did, seen_source[did]))
                    continue
                data['_source'] = disp
                data['_layer'] = layer
                seen_source[did] = disp
                self._drivers[did] = data
        # second pass: every 'extends' must resolve to a loaded driver.
        for did, d in list(self._drivers.items()):
            parent = d.get('extends')
            if parent and parent not in self._drivers:
                self._errors.append('%s: extends unknown driver %r'
                                    % (d.get('_source', did), parent))

    # ---- validation (hand-rolled; mirrors drivers/driver.schema.json) --------

    def _validate(self, data: Any, layer: str) -> List[str]:
        errs: List[str] = []
        if not isinstance(data, dict):
            return ['top level must be a JSON object']
        did = data.get('driver_id')
        if not did or not isinstance(did, str):
            errs.append('missing or non-string driver_id')
            did = ''
        for k in data:
            if k not in _DRIVER_KEYS and not k.startswith('_'):
                errs.append('unknown top-level key %r' % k)
        prefix = did.split('.', 1)[0] if '.' in did else ''
        if did and prefix not in TIERS:
            errs.append("driver_id must start with one of %s (got %r)" % (list(TIERS), did))
        tier = data.get('tier')
        if tier is not None:
            if tier not in TIERS:
                errs.append('tier %r not in %s' % (tier, list(TIERS)))
            elif prefix and tier != prefix:
                errs.append('tier %r disagrees with driver_id prefix %r' % (tier, prefix))
        exp_layer = {'protocol': 'protocols', 'vendor': 'vendors',
                     'family': 'families', 'model': 'models'}.get(prefix)
        if exp_layer and exp_layer != layer:
            errs.append('driver_id prefix %r belongs in %s/ but was found in %s/'
                        % (prefix, exp_layer, layer))
        if not isinstance(data.get('protocol', ''), str) or not data.get('protocol'):
            errs.append('missing or non-string protocol')
        if 'extends' in data and data['extends'] is not None \
                and not isinstance(data['extends'], str):
            errs.append('extends must be a string (a driver_id) or null')
        if 'label' in data and not isinstance(data['label'], str):
            errs.append('label must be a string')
        errs += self._validate_match(data.get('match', {}))
        if 'transport' in data:
            errs += self._validate_transport(data['transport'], 'transport')
        caps = data.get('capabilities', {})
        if not isinstance(caps, dict):
            errs.append('capabilities must be an object')
        else:
            for action, spec in caps.items():
                errs += self._validate_cap(action, spec)
        return errs

    def _validate_match(self, m: Any) -> List[str]:
        errs: List[str] = []
        if not isinstance(m, dict):
            return ['match must be an object']
        for k in m:
            if k not in _MATCH_KEYS:
                errs.append('match: unknown key %r' % k)
        if 'always' in m and not isinstance(m['always'], bool):
            errs.append('match.always must be boolean')
        for k in ('manufacturer_equals', 'manufacturer_contains',
                  'model_equals', 'model_contains', 'model_prefix',
                  'firmware_min', 'firmware_max', 'firmware_equals'):
            if k in m and m[k] is not None and not isinstance(m[k], str):
                errs.append('match.%s must be a string' % k)
        for k in ('manufacturer_aliases', 'model_aliases'):
            if k in m and not (isinstance(m[k], list) and all(isinstance(x, str) for x in m[k])):
                errs.append('match.%s must be a list of strings' % k)
        pc = m.get('pjlink_class')
        if pc is not None and not (isinstance(pc, str)
                                   or (isinstance(pc, list) and all(isinstance(x, str) for x in pc))):
            errs.append('match.pjlink_class must be a string or list of strings')
        return errs

    def _validate_transport(self, t: Any, where: str) -> List[str]:
        errs: List[str] = []
        if not isinstance(t, dict):
            return ['%s must be an object' % where]
        for k in t:
            if k not in _TRANSPORT_KEYS:
                errs.append('%s: unknown key %r' % (where, k))
        typ = t.get('type')
        if typ is not None and typ not in self._transports:
            errs.append('%s.type %r not in %s' % (where, typ, sorted(self._transports)))
        port = t.get('port')
        if port is not None and not (isinstance(port, int) and not isinstance(port, bool)
                                     and 1 <= port <= 65535):
            errs.append('%s.port must be an int 1..65535' % where)
        if 'terminator' in t and not isinstance(t['terminator'], str):
            errs.append('%s.terminator must be a string' % where)
        return errs

    def _validate_cap(self, action: Any, spec: Any) -> List[str]:
        errs: List[str] = []
        if not isinstance(action, str) or not action:
            errs.append('capability name must be a non-empty string')
        if not isinstance(spec, dict):
            return errs + ['capability %r must be an object' % action]
        for k in spec:
            if k not in _CAP_KEYS:
                errs.append('capability %r: unknown key %r' % (action, k))
        command = spec.get('command')
        if not isinstance(command, str) or not command:
            errs.append('capability %r: missing or non-string command' % action)
            command = ''
        parse = spec.get('parse')
        if not isinstance(parse, str) or not parse:
            errs.append('capability %r: missing or non-string parse' % action)
        elif parse not in self._parsers:
            errs.append('capability %r: unknown parser %r (known: %s)'
                        % (action, parse, sorted(self._parsers)))
        kind = spec.get('kind')
        if kind is not None and kind not in CAP_KINDS:
            errs.append('capability %r: kind %r not in %s' % (action, kind, sorted(CAP_KINDS)))
        vm = spec.get('value_map')
        if vm is not None and not (isinstance(vm, dict)
                                   and all(isinstance(k, str) and isinstance(v, str)
                                           for k, v in vm.items())):
            errs.append('capability %r: value_map must be an object of string->string' % action)
        if kind == 'set' and vm is None and '{value}' not in command:
            errs.append("capability %r: a 'set' capability needs a value_map or a "
                        "'{value}' placeholder in its command" % action)
        if vm is not None and kind not in (None, 'set'):
            errs.append("capability %r: value_map is only valid for kind 'set'" % action)
        cw = spec.get('confirm_with')
        if 'confirm_with' in spec and cw is not None and not isinstance(cw, str):
            errs.append('capability %r: confirm_with must be a string or null' % action)
        if 'transport' in spec:
            errs += self._validate_transport(spec['transport'],
                                             'capability %r transport' % action)
        if 'meta' in spec:
            errs += self._validate_meta(action, spec['meta'])
        return errs

    def _validate_meta(self, action: str, meta: Any) -> List[str]:
        errs: List[str] = []
        if not isinstance(meta, dict):
            return ['capability %r: meta must be an object' % action]
        for k in meta:
            if k not in _META_KEYS:
                errs.append('capability %r: meta unknown key %r' % (action, k))
        prov = meta.get('provenance')
        if prov is not None and prov not in PROVENANCE:
            errs.append('capability %r: meta.provenance %r not in %s'
                        % (action, prov, sorted(PROVENANCE)))
        if 'decoded' in meta and not isinstance(meta['decoded'], str):
            errs.append('capability %r: meta.decoded must be a string' % action)
        src = meta.get('source')
        if src is not None:
            if not isinstance(src, dict):
                errs.append('capability %r: meta.source must be an object' % action)
            else:
                for k in src:
                    if k not in _SOURCE_KEYS:
                        errs.append('capability %r: meta.source unknown key %r' % (action, k))
        return errs

    @property
    def errors(self) -> List[str]:
        return list(self._errors)

    def driver_ids(self) -> List[str]:
        return sorted(self._drivers.keys())

    # ---- normalization ------------------------------------------------------

    @staticmethod
    def normalize(s: Any) -> str:
        """Lowercase, keep alphanumerics + space/-/ for loose contains-matching."""
        return ''.join(c for c in str(s or '').lower()
                       if c.isalnum() or c in ' -/').strip()

    # ---- matching -----------------------------------------------------------

    def match_driver(self, manufacturer: str = '', model: str = '',
                     pjlink_class: str = '', firmware: str = '') -> Optional[str]:
        """Return the driver_id best matching the device. More specific matches
        score higher; the generic ('always':true) driver is the lowest-priority
        fallback. Ties break toward the deeper tier (model > family > vendor >
        protocol). Returns None only when no driver is loaded."""
        nm = self.normalize(manufacturer)
        nmodel = self.normalize(model)
        ncls = str(pjlink_class or '').strip()
        best_id: Optional[str] = None
        best_score = -1
        for did, d in self._drivers.items():
            score = self._match_score(d.get('match', {}), nm, nmodel, ncls, firmware)
            if score < 0:
                continue
            if score > best_score or (score == best_score
                                      and _TIER_RANK.get(did.split('.', 1)[0], 0)
                                      > _TIER_RANK.get((best_id or '').split('.', 1)[0], -1)):
                best_id, best_score = did, score
        return best_id

    def _match_score(self, match: Dict[str, Any], nm: str, nmodel: str,
                     ncls: str, firmware: str) -> int:
        """-1 = disqualified; higher = more specific. 'always' is the 0 floor."""
        if not isinstance(match, dict) or not match:
            return -1
        score = 0 if match.get('always') else -1

        # manufacturer
        if match.get('manufacturer_equals'):
            if self.normalize(match['manufacturer_equals']) == nm:
                score = max(score, 0) + 10
            else:
                return -1
        mc = self.normalize(match.get('manufacturer_contains', ''))
        if mc:
            if mc in nm:
                score = max(score, 0) + 10
            else:
                return -1
        for a in (match.get('manufacturer_aliases') or []):
            na = self.normalize(a)
            if na and na in nm:
                score = max(score, 0) + 10
                break

        # model -- exact/alias strongest, then family prefix, then contains
        if match.get('model_equals'):
            if self.normalize(match['model_equals']) == nmodel:
                score = max(score, 0) + 200
            else:
                return -1
        for a in (match.get('model_aliases') or []):
            na = self.normalize(a)
            if na and (na == nmodel or na in nmodel):
                score = max(score, 0) + 200
                break
        pfx = self.normalize(match.get('model_prefix', ''))
        if pfx:
            if nmodel.startswith(pfx) or pfx in nmodel:
                score = max(score, 0) + 100
            else:
                return -1
        mdc = self.normalize(match.get('model_contains', ''))
        if mdc:
            if mdc in nmodel:
                score = max(score, 0) + 50
            else:
                return -1

        # PJLink class: bonus when it matches, disqualify when known-and-wrong,
        # neither when the device class is unknown.
        pc = match.get('pjlink_class')
        if pc is not None:
            allowed = [pc] if isinstance(pc, str) else list(pc)
            allowed = [str(x).strip() for x in allowed]
            if ncls and ncls in allowed:
                score = max(score, 0) + 5
            elif ncls:
                return -1

        # firmware: only applied when the device firmware is actually known.
        if firmware:
            fw = str(firmware).strip()
            if match.get('firmware_equals') and str(match['firmware_equals']).strip() != fw:
                return -1
            # firmware_min/_max are reserved: string ordering is unreliable, so
            # they are validated but not enforced until a version parser exists.
        return score

    def match_reason(self, driver_id: Optional[str]) -> str:
        """A short human string describing why this driver's match block fires."""
        m = (self._drivers.get(driver_id or '', {}) or {}).get('match', {}) or {}
        bits: List[str] = []
        if m.get('always'):
            bits.append('always (fallback)')
        for k in ('manufacturer_equals', 'manufacturer_contains',
                  'model_equals', 'model_contains', 'model_prefix'):
            if m.get(k):
                bits.append("%s '%s'" % (k, m[k]))
        if m.get('model_aliases'):
            bits.append('model_aliases %s' % (m['model_aliases'],))
        if m.get('pjlink_class') is not None:
            bits.append('pjlink_class %s' % (m['pjlink_class'],))
        return ', '.join(bits) if bits else '(no criteria)'

    # ---- inheritance merge --------------------------------------------------

    def resolve_chain(self, driver_id: Optional[str]) -> List[str]:
        """Ordered root->leaf extends chain for a driver (protocol first, the
        matched driver last). Cycle-safe."""
        chain: List[str] = []
        seen = set()
        cur = driver_id
        while cur and cur in self._drivers and cur not in seen:
            seen.add(cur)
            chain.append(cur)
            cur = self._drivers[cur].get('extends')
        chain.reverse()
        return chain

    def _resolved(self, driver_id: Optional[str], _seen=None) -> Dict[str, Any]:
        """A driver with its 'extends' chain merged in (child overrides parent;
        capabilities are deep-merged per action)."""
        _seen = _seen or set()
        if not driver_id or driver_id in _seen or driver_id not in self._drivers:
            return {}
        _seen.add(driver_id)
        d = self._drivers[driver_id]
        base = self._resolved(d.get('extends'), _seen)
        merged = dict(base)
        for k, v in d.items():
            if k == 'capabilities':
                caps = dict(base.get('capabilities', {}))
                caps.update(v or {})
                merged['capabilities'] = caps
            elif k.startswith('_'):
                continue
            else:
                merged[k] = v
        return merged

    def capability_origin(self, driver_id: Optional[str]) -> Dict[str, str]:
        """action -> the driver_id that supplied the WINNING definition (the
        deepest driver in the chain that defines it)."""
        origin: Dict[str, str] = {}
        for did in self.resolve_chain(driver_id):      # root->leaf; leaf overrides
            for action in (self._drivers[did].get('capabilities') or {}):
                origin[action] = did
        return origin

    # ---- public resolver API ------------------------------------------------

    def capabilities(self, driver_id: Optional[str]) -> List[str]:
        """Sorted list of normalized actions the resolved driver supports."""
        return sorted(self._resolved(driver_id).get('capabilities', {}).keys())

    def resolve_action(self, driver_id: Optional[str], action: str) -> Optional[Dict[str, Any]]:
        """The merged capability spec ({command, kind?, value_map?, parse,
        confirm_with?, transport?, meta?}) for an action, or None if the driver
        does not support it."""
        return self._resolved(driver_id).get('capabilities', {}).get(action)

    @staticmethod
    def capability_kind(action: str, spec: Optional[Dict[str, Any]]) -> str:
        """'query' | 'command' | 'set'. Prefer the declared 'kind'; otherwise infer
        it from the action name. This is the ONE place the old name-suffix heuristic
        lives, so callers (and the TD extension, once it is wired here) stop guessing
        it themselves."""
        if spec and spec.get('kind') in CAP_KINDS:
            return str(spec['kind'])
        if spec and ('value_map' in spec or '{value}' in str(spec.get('command', ''))):
            return 'set'
        if action.endswith(('.get', '.hours', '.query', '.list', '.status')):
            return 'query'
        if action.endswith('.set'):
            return 'set'
        return 'command'

    def resolve_set(self, driver_id: Optional[str], action: str,
                    value: str) -> Optional[Dict[str, Any]]:
        """Resolve a parameterized 'set' action for a concrete value into a ready-to-
        send spec: the {value} placeholder is substituted with the value_map token
        (or the raw value when no value_map is defined). Returns None when the action
        is absent, is not a 'set', or the value is not in a defined value_map (an
        unknown value is refused, never sent blindly). The returned spec is a copy;
        the stored definition is never mutated."""
        spec = self.resolve_action(driver_id, action)
        if not spec or self.capability_kind(action, spec) != 'set':
            return None
        vm = spec.get('value_map')
        if vm is not None:
            if value not in vm:
                return None
            token = vm[value]
        else:
            token = str(value)
        out = dict(spec)
        out['command'] = str(spec.get('command', '')).replace('{value}', token)
        out['resolved_value'] = value
        return out

    def set_values(self, driver_id: Optional[str], action: str) -> List[str]:
        """The accepted normalized values for a 'set' action (its value_map keys),
        or [] when the action takes a free/raw value or is not a 'set'."""
        spec = self.resolve_action(driver_id, action)
        if not spec or self.capability_kind(action, spec) != 'set':
            return []
        return sorted((spec.get('value_map') or {}).keys())

    def transport(self, driver_id: Optional[str]) -> Dict[str, Any]:
        return self._resolved(driver_id).get('transport', {})

    def label(self, driver_id: Optional[str]) -> str:
        return str(self._resolved(driver_id).get('label', driver_id or ''))

    # ---- explain ------------------------------------------------------------

    def explain(self, manufacturer: str = '', model: str = '',
                pjlink_class: str = '', firmware: str = '') -> Dict[str, Any]:
        """Structured report of how the final driver was constructed: the matched
        driver, its root->leaf chain with each level's match reason, the default
        transport, and per final capability which driver supplied it (own vs
        inherited), its transport, and its provenance."""
        matched = self.match_driver(manufacturer, model, pjlink_class, firmware)
        chain = self.resolve_chain(matched)
        origin = self.capability_origin(matched)
        res = self._resolved(matched)
        default_tp = res.get('transport', {}) or {}
        caps: Dict[str, Any] = {}
        for action in sorted(res.get('capabilities', {}).keys()):
            spec = res['capabilities'][action]
            src = origin.get(action)
            tp = spec.get('transport') or default_tp
            meta = spec.get('meta') or {}
            own = (src == matched)
            caps[action] = {
                'supplied_by': src,
                'origin': 'own' if own else 'inherited',
                'transport': '%s:%s' % (tp.get('type', 'pjlink'), tp.get('port', '')),
                'command': spec.get('command'),
                'provenance': meta.get('provenance') or ('inherited' if not own else 'unspecified'),
                'decoded': meta.get('decoded'),
                'confirm_with': spec.get('confirm_with'),
            }
        return {
            'device': {'manufacturer': manufacturer, 'model': model,
                       'pjlink_class': pjlink_class, 'firmware': firmware or None},
            'matched_driver': matched,
            'chain': [{'driver_id': did, 'label': self.label(did),
                       'match': self.match_reason(did)} for did in chain],
            'transport_default': '%s:%s' % (default_tp.get('type', 'pjlink'),
                                            default_tp.get('port', '')),
            'capabilities': caps,
        }

    def explain_text(self, manufacturer: str = '', model: str = '',
                     pjlink_class: str = '', firmware: str = '') -> str:
        """Printable version of explain(): the inheritance chain followed by a
        per-capability table (supplier, own/inherited, transport, provenance)."""
        e = self.explain(manufacturer, model, pjlink_class, firmware)
        lines: List[str] = []
        if not e['matched_driver']:
            return 'No driver matched (none loaded?).'
        for i, c in enumerate(e['chain']):
            arrow = '' if i == 0 else ('  ' * i) + '-> '
            lines.append('%s%s  %s  [match: %s]'
                         % (arrow, c['driver_id'], c['label'], c['match']))
        lines.append('transport default: %s' % e['transport_default'])
        lines.append('%-18s %-20s %-10s %-16s %s'
                     % ('capability', 'supplied by', 'origin', 'transport', 'provenance'))
        for action in sorted(e['capabilities']):
            c = e['capabilities'][action]
            extra = ''
            if c.get('confirm_with'):
                extra += ' (confirm_with %s)' % c['confirm_with']
            if c.get('decoded') == 'unknown':
                extra += ' (decoded: unknown)'
            lines.append('%-18s %-20s %-10s %-16s %s%s'
                         % (action, c['supplied_by'], c['origin'],
                            c['transport'], c['provenance'], extra))
        return '\n'.join(lines)
