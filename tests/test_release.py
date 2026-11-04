import contextlib
import io
import os
from pathlib import Path
import unittest
from unittest.mock import patch
from scripts import release

CHANGELOG = '''# Changes
## [Unreleased]
- Not ready.
## [1.010] — 2026-11-04
- Newest change.
## [1.009] — 2026-11-03
- Middle change.
## [1.008] — 2026-11-02
- Already released.
'''

def published(tag, **extra):
    return {'tag_name': tag, 'draft': False, 'prerelease': False, **extra}

class ReleaseTests(unittest.TestCase):
    def test_single_version(self):
        result = release.plan(CHANGELOG, [published('v1.009')])
        self.assertEqual(result['tag_name'], 'v1.010')
        self.assertEqual(result['name'], 'v1.010')
        self.assertIn('Newest change.', result['body'])
        self.assertNotIn('Middle change.', result['body'])
        self.assertNotIn('Not ready.', result['body'])

    def test_combined_release(self):
        result = release.plan(CHANGELOG, [published('v1.008')])
        self.assertEqual(result['name'], 'Сводный релиз: изменения с v1.009 по v1.010')
        self.assertIn('Newest change.', result['body'])
        self.assertIn('Middle change.', result['body'])
        self.assertNotIn('Already released.', result['body'])
        self.assertLess(result['body'].index('[1.010]'), result['body'].index('[1.009]'))

    def test_first_release_collects_every_version(self):
        result = release.plan(CHANGELOG, [])
        self.assertEqual(result['name'], 'Сводный релиз: изменения с v1.008 по v1.010')
        self.assertIn('Already released.', result['body'])

    def test_repeated_or_older_run_is_noop(self):
        for tag in ('v1.010', 'v2.000'):
            self.assertIsNone(release.plan(CHANGELOG, [published(tag)]))

    def test_numeric_baseline_ignores_release_order_and_foreign_tags(self):
        releases = [published('v1.008'), published('other'), published('v1.009'),
                    published('v9.999', draft=True), published('v8.999', prerelease=True)]
        self.assertEqual(release.plan(CHANGELOG, releases)['name'], 'v1.010')
        self.assertGreater(release.version('v10.000'), release.version('v9.999'))

    def test_draft_or_prerelease_collision_is_explicit(self):
        for flag in ('draft', 'prerelease'):
            with self.assertRaisesRegex(ValueError, 'already exists'):
                release.plan(CHANGELOG, [published('v1.010', **{flag: True})])

    def test_invalid_changelog_fails_closed(self):
        for text in ['# Empty', '## [1.01]\n- bad', '## [1.001]\n',
                     '## [1.001]\n- A\n## [1.001]\n- B',
                     '## [1.001]\n- A\n## [1.002]\n- B']:
            with self.subTest(text=text), self.assertRaises(ValueError):
                release.plan(text, [])

    def test_fenced_code_and_subheadings_are_preserved(self):
        text = '## [1.001]\n\n### Fixes\n\n```markdown\n## [9.999]\n```\n- Fixed.'
        result = release.plan(text, [])
        self.assertEqual(result['tag_name'], 'v1.001')
        self.assertIn('## [9.999]', result['body'])
        self.assertIn('### Fixes', result['body'])

    def test_cli_paginates_and_targets_exact_commit(self):
        env = {'GITHUB_REF': 'refs/heads/main', 'GITHUB_REPOSITORY': 'owner/shop', 'GITHUB_SHA': 'a'*40}
        with patch.dict(os.environ, env, clear=True), patch('sys.argv', ['release.py']), \
             patch.object(Path, 'read_text', return_value=CHANGELOG), \
             patch.object(release, 'github', side_effect=[[[published('v1.008')], [published('v1.009')]],
                 {'html_url': 'https://github.com/owner/shop/releases/tag/v1.010'}]) as api, \
             contextlib.redirect_stdout(io.StringIO()):
            release.main()
            self.assertIn('--paginate', api.call_args_list[0].args[0])
            payload = api.call_args_list[1].args[1]
            self.assertEqual(payload['target_commitish'], 'a'*40)
            self.assertEqual(payload['name'], 'v1.010')
            self.assertFalse(payload['draft'])

    def test_cli_noop_and_dry_run_do_not_write(self):
        env = {'GITHUB_REF': 'refs/heads/main', 'GITHUB_REPOSITORY': 'owner/shop', 'GITHUB_SHA': 'a'*40}
        for args, releases in [(['release.py'], [published('v1.010')]), (['release.py', '--dry-run'], [])]:
            with patch.dict(os.environ, env, clear=True), patch('sys.argv', args), \
                 patch.object(Path, 'read_text', return_value=CHANGELOG), \
                 patch.object(release, 'github', return_value=[releases]) as api, \
                 contextlib.redirect_stdout(io.StringIO()):
                release.main()
                self.assertEqual(api.call_count, 1)

    def test_manual_run_on_other_branch_cannot_publish(self):
        with patch.dict(os.environ, {'GITHUB_REF': 'refs/heads/feature'}, clear=True), \
             patch('sys.argv', ['release.py']), patch.object(release, 'github') as api:
            with self.assertRaisesRegex(ValueError, 'main'):
                release.main()
            api.assert_not_called()

if __name__ == '__main__':
    unittest.main()
