"""Post-hoc report-ID repair; never execute tests or alter solver artifacts."""
import re

VERSION = 'report-id-v1-20261013'


def canonical(task, name):
    if task == 'obsidian-linter-auto-table-of-contents':
        if name.startswith('Auto TOC '):
            return 'Auto Table of Contents ' + name[len('Auto TOC '):]
    if task == 'vulture-persistent-analysis-cache':
        return re.sub(
            r'^(tests\.test_config\.test_incompatible_option_type\[(?:paths|exclude|ignore_decorators|ignore_names)-value)\d+(\])$',
            r'\1#\2', name,
        )
    return name


def remap(task, seen, expected):
    """Only uniquely identified aliases; collisions fail closed, status unchanged."""
    targets = {}
    for name in expected:
        key = canonical(task, name)
        if key in targets and targets[key] != name:
            raise ValueError('ambiguous expected identity')
        targets[key] = name
    groups = {}
    for name, value in seen.items():
        key = canonical(task, name)
        groups.setdefault(key, []).append((name, value))
    out = dict(seen)
    aliases = []
    for key, matches in groups.items():
        target = targets.get(key)
        if target is None:
            continue
        if len(matches) > 1:
            raise ValueError('ambiguous observed identity')
        source, value = matches[0]
        if source != target:
            out[target] = value
            aliases.append({'source': source, 'target': target, 'status': value[0]})
    return out, aliases


def self_check():
    v = 'vulture-persistent-analysis-cache'
    old = 'tests.test_config.test_incompatible_option_type[paths-value2]'
    new = old.replace('value2', 'value5')
    out, aliases = remap(v, {new: ('failed', 'real failure')}, [old])
    assert out[old] == ('failed', 'real failure') and len(aliases) == 1
    assert remap(v, {}, [old]) == ({}, [])
    assert remap('unrelated', {new: ('passed', '')}, [old])[0].get(old) is None
    for seen, expected in [({old: ('failed', ''), new: ('passed', '')}, [old]), ({new: ('passed','')}, [old,new])]:
        try:
            remap(v, seen, expected)
        except ValueError:
            pass
        else:
            raise AssertionError('ambiguous IDs must not pass')
    o = 'obsidian-linter-auto-table-of-contents'
    for st in ('passed', 'failed', 'skipped'):
        out, _ = remap(o, {'Auto TOC case': (st, '')}, ['Auto Table of Contents case'])
        assert out['Auto Table of Contents case'][0] == st
    assert canonical(o, 'Unrelated Auto TOC case') == 'Unrelated Auto TOC case'
    print('report ID repair self-check passed')


if __name__ == '__main__':
    self_check()
