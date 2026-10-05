import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
PATH_LINE = 'export PATH="$HOME/.local/bin:$PATH"'


class Installer(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='boris-install-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / 'home with spaces'
        self.home.mkdir()
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        self.downloads = self.root / 'downloads'
        self.downloads.mkdir()
        self.archive = self.root / 'source.tar.gz'
        self.make_archive()
        # Stub only the network; extraction, installation, startup files, and
        # the installed CLI run as real subprocesses in an isolated home.
        curl = self.bin / 'curl'
        curl.write_text(f'#!{sys.executable}\n' + '''
import os, pathlib, shutil, sys
args = sys.argv[1:]
pathlib.Path(os.environ['URL_LOG']).write_text(args[-1])
if os.environ.get('DOWNLOAD_FAIL'):
    sys.exit(22)
shutil.copyfile(os.environ['ARCHIVE'], args[args.index('-o') + 1])
''')
        curl.chmod(0o755)
        self.env = dict(os.environ, HOME=str(self.home), SHELL='/bin/zsh',
                        PATH=str(self.bin) + os.pathsep + os.environ['PATH'],
                        TMPDIR=str(self.downloads), ARCHIVE=str(self.archive),
                        URL_LOG=str(self.root / 'url'))
        for name in ['BORIS_REF', 'ZDOTDIR', 'XDG_CONFIG_HOME', 'DOWNLOAD_FAIL']:
            self.env.pop(name, None)

    def make_archive(self, complete=True):
        with tarfile.open(self.archive, 'w:gz') as archive:
            archive.add(ROOT / 'boris', arcname='boris-main/boris')
            if complete:
                archive.add(ROOT / 'lib', arcname='boris-main/lib',
                            filter=lambda entry: None if '__pycache__' in entry.name else entry)
                archive.add(ROOT / 'skills', arcname='boris-main/skills')
                extra = self.root / 'extra-skill'
                extra.mkdir(exist_ok=True)
                (extra / 'SKILL.md').write_text('Another bundled skill\n')
                (extra / 'reference.md').write_text('Supporting material\n')
                archive.add(extra, arcname='boris-main/skills/extra')

    def install(self, check=True, **env):
        result = subprocess.run(['sh'], input=(ROOT / 'install.sh').read_text(),
                                env=dict(self.env, **env), cwd=self.root,
                                capture_output=True, text=True, timeout=15)
        if check:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(list(self.downloads.iterdir()), [], 'temporary download left behind')
        return result

    def test_pipe_installs_working_cli_and_all_skill_assets(self):
        self.install()
        command = self.home / '.local/bin/boris'
        self.assertTrue(command.is_symlink())
        self.assertEqual(command.resolve(), (self.home / '.boris/boris').resolve())
        self.assertTrue((self.home / '.boris/lib/boris.py').is_file())
        result = subprocess.run([str(command), '--help'], env=self.env,
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('usage:', result.stdout)
        skills = self.home / '.agents/skills'
        self.assertEqual((skills / 'boris/SKILL.md').read_bytes(),
                         (ROOT / 'skills/boris/SKILL.md').read_bytes())
        self.assertTrue((skills / 'boris/agents/openai.yaml').is_file())
        self.assertEqual((skills / 'extra/reference.md').read_text(), 'Supporting material\n')
        for profile in ['.zshrc', '.zprofile']:
            self.assertIn(PATH_LINE, (self.home / profile).read_text())

    def test_reinstall_updates_bundled_files_preserves_user_files_and_path(self):
        self.install()
        skills = self.home / '.agents/skills'
        (skills / 'boris/SKILL.md').write_text('Outdated\n')
        (skills / 'boris/custom.md').write_text('Keep me\n')
        (skills / 'personal').mkdir()
        (skills / 'personal/SKILL.md').write_text('Personal skill\n')
        self.install()
        self.assertEqual((skills / 'boris/SKILL.md').read_bytes(),
                         (ROOT / 'skills/boris/SKILL.md').read_bytes())
        self.assertEqual((skills / 'boris/custom.md').read_text(), 'Keep me\n')
        self.assertEqual((skills / 'personal/SKILL.md').read_text(), 'Personal skill\n')
        self.assertEqual((self.home / '.zshrc').read_text().count(PATH_LINE), 1)

    def test_bash_preserves_existing_login_profile_and_makes_command_available(self):
        profile = self.home / '.bash_profile'
        profile.write_text('# Existing settings\nexport EXISTING_SETTING=kept\n')
        self.install(SHELL='/bin/bash')
        self.assertTrue(profile.read_text().startswith('# Existing settings\n'))
        self.assertFalse((self.home / '.profile').exists())
        result = subprocess.run(['bash', '-c', '. "$HOME/.bash_profile"; command -v boris; boris --help'],
                                env=self.env, capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(str(self.home / '.local/bin/boris'), result.stdout)
        self.assertIn(PATH_LINE, (self.home / '.bashrc').read_text())

    def test_custom_shell_config_locations(self):
        zsh_dir = self.home / 'zsh config'
        self.install(ZDOTDIR=str(zsh_dir))
        self.assertIn(PATH_LINE, (zsh_dir / '.zshrc').read_text())
        self.assertFalse((self.home / '.zshrc').exists())
        fish_config = self.home / 'config dir'
        self.install(SHELL='/usr/bin/fish', XDG_CONFIG_HOME=str(fish_config))
        self.assertIn('set -gx PATH "$HOME/.local/bin" $PATH',
                      (fish_config / 'fish/config.fish').read_text())

    def test_existing_path_needs_no_startup_changes(self):
        self.install(PATH=str(self.home / '.local/bin') + os.pathsep + self.env['PATH'])
        self.assertFalse((self.home / '.zshrc').exists())
        self.assertFalse((self.home / '.zprofile').exists())

    def test_custom_ref_selects_one_archive(self):
        self.install(BORIS_REF='v1.2.3')
        self.assertEqual((self.root / 'url').read_text(),
                         'https://codeload.github.com/tyler-schwartz/boris/tar.gz/v1.2.3')

    def test_failed_download_and_invalid_archive_do_not_install(self):
        result = self.install(check=False, DOWNLOAD_FAIL='1')
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.home / '.local').exists())
        self.assertFalse((self.home / '.boris').exists())
        self.make_archive(complete=False)
        result = self.install(check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('missing the Boris runtime or skill', result.stderr)
        self.assertFalse((self.home / '.local').exists())
        self.assertFalse((self.home / '.boris').exists())
        self.assertFalse((self.home / '.agents').exists())

    def test_existing_unrelated_executable_is_not_overwritten(self):
        command = self.home / '.local/bin/boris'
        command.parent.mkdir(parents=True)
        command.write_text('An unrelated command\n')
        result = self.install(check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Refusing to overwrite', result.stderr)
        self.assertEqual(command.read_text(), 'An unrelated command\n')
        self.assertFalse((self.home / '.agents').exists())


if __name__ == '__main__':
    unittest.main()
