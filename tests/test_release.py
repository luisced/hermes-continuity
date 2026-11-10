import hashlib,json,tempfile,unittest,zipfile
from pathlib import Path
from scripts.build_release import build,ROOT

class ReleaseTests(unittest.TestCase):
    def test_reproducible_archives_contents_and_exclusions(self):
        with tempfile.TemporaryDirectory() as d:
            first=build(ROOT,Path(d)); second=build(ROOT,Path(d))
            self.assertEqual(first,second)
            archive=next(Path(d).glob('*.zip'))
            with zipfile.ZipFile(archive) as z:
                names=z.namelist(); prefix=names[0].split('/')[0]+'/'
                self.assertFalse(any(any(part in Path(n).parts for part in ('.git','.agents','.aws','.codex','evidence','dist')) for n in names))
                manifest=json.loads(z.read(prefix+'CONTENTS.sha256.json'))
                for name,digest in manifest.items(): self.assertEqual(hashlib.sha256(z.read(prefix+name)).hexdigest(),digest)
                self.assertIn('tests/test_hermes_contract.py',manifest)
                self.assertIn('docs/CHECKINS.md',manifest)
                self.assertIn('continuity/checkin.py',manifest)
