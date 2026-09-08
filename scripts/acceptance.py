#!/usr/bin/env python3
"""Exercise the real Pi CLI in an isolated home. No model prompts are sent."""
import json
import os
from pathlib import Path
import selectors
import shutil
import subprocess
import tempfile
import time

HUB = Path(__file__).resolve().parents[1] / 'hub.py'
PI = os.environ.get('PI_HUB_PI') or shutil.which('pi')
if not PI:
    raise SystemExit('Pi must be installed; set PI_HUB_PI to its actual executable.')

with tempfile.TemporaryDirectory(prefix='pi-hub-acceptance-') as directory:
    base = Path(directory)
    project = base / 'project'
    project.mkdir()
    package = base / 'package'
    (package / 'prompts').mkdir(parents=True)
    (package / 'package.json').write_text(json.dumps({
        'name': 'pi-hub-acceptance-fixture', 'version': '1.0.0',
        'pi': {'prompts': ['prompts/check-package.md']},
    }))
    (package / 'README.md').write_text('# Acceptance fixture\nA harmless local prompt package.\n')
    (package / 'prompts/check-package.md').write_text('---\ndescription: Acceptance fixture\n---\nDescribe $1.\n')
    env = dict(os.environ, HOME=str(base), XDG_STATE_HOME=str(base / 'state'),
               XDG_CONFIG_HOME=str(base / 'config'), XDG_DATA_HOME=str(base / 'data'),
               XDG_CACHE_HOME=str(base / 'cache'),
               PI_CODING_AGENT_DIR=str(base / 'agent'), PI_HUB_PI=PI,
               PI_OFFLINE='1', npm_config_ignore_scripts='true')

    def call(action, scope='project', expect=True, **fields):
        request = dict(action=action, scope=scope, project=str(project), **fields)
        result = subprocess.run(['python3', str(HUB), 'rpc'], input=json.dumps(request),
                                capture_output=True, text=True, env=env, timeout=200)
        payload = json.loads(result.stdout)
        assert payload['ok'] is expect, (action, payload)
        return payload

    def commands():
        # RPC queries enumerate resources without asking a model to do work.
        stderr_file = tempfile.TemporaryFile(mode='w+')
        process = subprocess.Popen([PI, '--mode', 'rpc', '--no-session', '--offline',
                                    '--no-context-files', '--approve'], cwd=project, env=env,
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=stderr_file,
                                   text=True)
        try:
            process.stdin.write('{"id":"acceptance","type":"get_commands"}\n')
            process.stdin.flush()
            selector = selectors.DefaultSelector()
            selector.register(process.stdout, selectors.EVENT_READ)
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                if not selector.select(timeout=1):
                    continue
                line = process.stdout.readline()
                if not line:
                    break
                try:
                    value = json.loads(line)
                except json.JSONDecodeError:
                    continue
                assert value.get('type') != 'extension_error', value
                if value.get('id') == 'acceptance':
                    assert value.get('success'), value
                    return {item['name'] for item in value['data']['commands']}
            raise AssertionError('Pi did not answer get_commands within 20 seconds')
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            stderr_file.seek(0)
            diagnostics = stderr_file.read()
            stderr_file.close()
            assert not diagnostics.strip(), diagnostics

    call('list')
    for kind, template, name in [('skills', 'skill', 'acceptance-skill'),
                                  ('prompts', 'prompt', 'acceptance-prompt'),
                                  ('extensions', 'command', 'acceptance-command'),
                                  ('extensions', 'tool', 'acceptance-tool'),
                                  ('extensions', 'guard', 'acceptance-guard')]:
        body = call('scaffold', kind=kind, template=template, name=name, description='Harmless acceptance fixture')['body']
        saved = call('save', kind=kind, name=name, path='', body=body)['resource']
        reread = call('read', path=saved['path'])['resource']
        assert reread['body'] == body
    available = commands()
    assert {'skill:acceptance-skill', 'acceptance-prompt', 'acceptance-command'} <= available, available
    print('PASS: generated resources load in real Pi RPC without a model call')

    prompt = next(r for r in call('list')['resources'] if r['name'] == 'acceptance-prompt')
    opened = call('read', path=prompt['path'])['resource']
    edited = call('save', kind='prompts', name=opened['name'], path=opened['path'],
                  revision=opened['revision'], body=opened['body'] + '\nExtra instruction.\n')['resource']
    call('save', expect=False, kind='prompts', name=opened['name'], path=opened['path'],
         revision=opened['revision'], body=opened['body'])
    token = call('delete', path=edited['path'], revision=edited['revision'], confirm=True)['undoToken']
    assert not Path(edited['path']).exists()
    call('restore', token=token)
    assert Path(edited['path']).read_text() == edited['body']
    print('PASS: editor save, stale revision rejection, delete and restore')

    call('toggleResource', path=prompt['path'], enabled=False)
    assert 'acceptance-prompt' not in commands(), (project / '.pi/settings.json').read_text()
    call('toggleResource', path=prompt['path'], enabled=True)
    assert 'acceptance-prompt' in commands()
    print('PASS: native resource toggles change actual Pi discovery')

    review = call('review', source=str(package))['review']
    assert 'prompts/check-package.md' in review['fileContents']
    call('install', expect=False, source=review['source'], token='invalid')
    call('install', scope='global', expect=False, source=review['source'], token=review['token'])
    call('install', source=review['source'], token=review['token'])
    assert 'check-package' in commands()
    listing = call('list')
    assert any(r['name'] == 'check-package' for r in listing['resources']), listing
    installed_source = listing['packages'][0]['source']
    call('toggle', source=installed_source, enabled=False)
    assert 'check-package' not in commands()
    call('toggle', source=installed_source, enabled=True)
    assert 'check-package' in commands()
    call('remove', source=installed_source, confirm=True)
    assert 'check-package' not in commands()
    assert not call('list')['packages']
    print('PASS: review, scope-bound install, package discovery, disable, enable and removal')

    review = call('review', source=str(package))['review']
    (package / 'prompts/check-package.md').write_text('Changed since review.\n')
    call('install', expect=False, source=review['source'], token=review['token'])
    assert not call('list')['packages']
    print('PASS: changed local package cannot reuse a review token')
